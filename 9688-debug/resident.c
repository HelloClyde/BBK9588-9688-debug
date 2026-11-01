/* SPDX-License-Identifier: GPL-3.0-only
 * Heap resident lifecycle and bounded CDC pump based on H1Debug resident.c. */
#include "debug.h"
#include "firmware.h"
#include "usb.h"
#include "protocol.h"
#include "screen.h"
#include "exec.h"
#include "management.h"
static int resident_init(rd_header *);
static void resident_worker(void *);
static void resident_shutdown(rd_header *);
static uint8_t task_stack[RD_STACK_BYTES] __attribute__((aligned(32)));
rd_header rd_control __attribute__((section(".resident_header")))={
    .magic=RD_MAGIC,.version=RD_VERSION,.priority=RD_PRIORITY,
    .init=resident_init,.worker=resident_worker,.shutdown=resident_shutdown,
    .stack_top=(uint32_t)(uintptr_t)(task_stack+RD_STACK_BYTES-4),
    .stack_bottom=(uint32_t)(uintptr_t)task_stack,.image_id=RD_BUILD_ID
};
static debug_usb usb;
static debug_protocol protocol;
static uint8_t screen[RD_SCREEN_BYTES] __attribute__((aligned(32)));
static uint32_t screen_meta[6],screen_token,screen_valid;
static uint32_t commands,polls,last_poll,reset_seen,session_seen,configured_seen,zlp_pending,stop_tick;
static int in_poll;
/* Retained across CDC reopen; query has no effect on task ownership. */
static uint32_t event_sequence,event_log[64][4];
static void record_event(uint32_t kind,uint32_t detail)
{
    if(++event_sequence==0){event_sequence=1;memset(event_log,0,sizeof event_log);}
    uint32_t *e=event_log[(event_sequence-1u)%64u];
    e[0]=event_sequence;e[1]=fw_tick();e[2]=kind;e[3]=detail;
}
void *memset(void *dst,int value,size_t n)
{uint8_t *p=dst;while(n--)*p++=(uint8_t)value;return dst;}
void *memcpy(void *dst,const void *src,size_t n)
{uint8_t *d=dst;const uint8_t *s=src;while(n--)*d++=*s++;return dst;}
void rd_cache_sync(void *start,void *end)
{
    uintptr_t a=(uintptr_t)start&~31u,b=((uintptr_t)end+31u)&~31u;
    __asm__ volatile("sync":::"memory");
    for(uintptr_t p=a;p<b;p+=16u)__asm__ volatile("cache 0x15,0(%0)"::"r"(p):"memory");
    __asm__ volatile("sync":::"memory");
    for(uintptr_t p=a;p<b;p+=16u)__asm__ volatile("cache 0x10,0(%0)"::"r"(p):"memory");
    __asm__ volatile("sync\n\tnop\n\tnop\n\tnop":::"memory");
}
static uint32_t get32(const uint8_t *p)
{return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static void put32(uint8_t *p,uint32_t v){for(unsigned i=0;i<4;i++)p[i]=(uint8_t)(v>>(8*i));}
static unsigned words(uint8_t *p,const uint32_t *v,unsigned n)
{for(unsigned i=0;i<n;i++)put32(p+4*i,v[i]);return 4*n;}
static int ram_range(uint32_t a,uint32_t n)
{return n && a>=0x80000000u && a<FW_SDRAM_END && n<=FW_SDRAM_END-a;}
static unsigned dispatch_inner(unsigned op,const uint8_t *p,unsigned n,uint8_t *out,unsigned *status)
{
    *status=1;commands++;
    if((exec_state==EXEC_RUNNING || exec_state==EXEC_PENDING) && op!=1 && op!=2 && op!=9 && op!=10 && op!=31 && op!=32 && op!=40 && op!=42 && op!=43 && op!=47 && op!=48){*status=2;return 0;}
    if(op>=26 && op<=34){
        if(rd_management_busy() && op!=26 && op!=31 && op!=32){*status=2;return 0;}
        return rd_exec_command(op,p,n,out,status);
    }
    if(op>=40 && op<=44)return rd_management_command(op,p,n,out,status);
    switch(op){
    case 1:{
        static const char identity[]=RD_IDENTITY;
        if(n)break;memcpy(out,identity,sizeof identity-1);*status=0;return sizeof identity-1;
    }
    case 2:{
        if(n)break;
        uint32_t v[]={1,rd_control.state,usb.configured,usb.ownership_fault,usb.resets,commands,
            protocol.bad_frames,fw_tick(),polls,last_poll,rd_control.base,rd_control.size,
            rd_control.image_id,exec_state,rd_control.last_error,screen_valid};
        *status=0;return words(out,v,16);
    }
    case 3:{
        if(n!=8)break;uint32_t a=get32(p),count=get32(p+4);
        if(count>512 || !ram_range(a,count))break;
        const volatile uint8_t *src=(const volatile uint8_t *)(uintptr_t)a;
        for(unsigned i=0;i<count;i++)out[i]=src[i];*status=0;return count;
    }
    case 4:{
        if(n<5)break;uint32_t a=get32(p),count=n-4,b=rd_control.allocation,m=rd_control.size+31u;
        if(!ram_range(a,count) || (a<b+m && b<a+count))break;
        volatile uint8_t *dst=(volatile uint8_t *)(uintptr_t)a;
        for(unsigned i=0;i<count;i++)dst[i]=p[4+i];
        rd_cache_sync((void *)(uintptr_t)a,(void *)(uintptr_t)(a+count));*status=0;return 0;
    }
    case 6:{
        if(n && n!=16)break;screen_valid=0;
        uint32_t rect[4];if(n)for(unsigned i=0;i<4;i++)rect[i]=get32(p+4*i);
        *status=(unsigned)rd_screen_capture_area(screen,sizeof screen,screen_meta,n?rect:0);if(*status)return 0;
        if(++screen_token==0)++screen_token;screen_valid=1;
        put32(out,screen_token);words(out+4,screen_meta,6);put32(out+28,fw_tick());return 32;
    }
    case 7:{
        if(n!=12)break;uint32_t token=get32(p),off=get32(p+4),wanted=get32(p+8);
        if(!screen_valid || token!=screen_token){*status=2;return 0;}
        if(!wanted || wanted>500 || off>=screen_meta[4])break;
        unsigned count=screen_meta[4]-off;if(count>wanted)count=wanted;
        put32(out,token);put32(out+4,off);put32(out+8,screen_meta[4]);memcpy(out+12,screen+off,count);
        *status=0;return 12+count;
    }
    case 8:
        if(n!=4 || !screen_valid || get32(p)!=screen_token)break;
        screen_valid=0;*status=0;return 0;
    case 9:{
        if(n)break;
        /* Independent 9688 CAPS: RAM read/write, screen, EXEC, table call. */
        uint32_t v[]={1,511,512,0x80000000u,FW_SDRAM_END-0x80000000u,FW_SCREEN_WIDTH,FW_SCREEN_HEIGHT,RD_BUILD_ID};
        *status=0;return words(out,v,8);
    }
    case 10:
        if(n)break;rd_control.stop_request=1;stop_tick=fw_tick();rd_exec_cancel(1);rd_management_disconnect();*status=0;return 0;
    case 14:{
        if(n)break;uint32_t v[DEBUG_USB_DIAG_WORDS];debug_usb_diagnostics(&usb,v);
        *status=0;return words(out,v,DEBUG_USB_DIAG_WORDS);
    }
    case 48:{
        if(n && n!=4)break;
        uint32_t cursor=n?get32(p):0;
        if(cursor>event_sequence)break;
        uint32_t first=event_sequence>64u?event_sequence-63u:1u;
        uint32_t from=cursor+1u;if(from<first)from=first;
        unsigned count=from<=event_sequence?event_sequence-from+1u:0u;
        if(count>31u)count=31u;
        uint32_t header[]={1,count?from+count-1u:cursor,first,count};words(out,header,4);
        for(unsigned i=0;i<count;i++)words(out+16u+i*16u,event_log[(from+i-1u)%64u],4);
        *status=0;return 16u+count*16u;
    }
    case 47:{
        if(n)break;uint32_t v[128];unsigned used=fw_discovery_info(v,128);
        if(used>128)break;*status=0;return words(out,v,used);
    }
    case 20:{
        if(n!=24)break;uint32_t idx=get32(p),offset=get32(p+4);
        static const uint32_t bounds[]={FW_GUI_CALL_LIMIT,FW_FS_CALL_LIMIT,FW_SYS_CALL_LIMIT,FW_MEM_CALL_LIMIT,FW_RES_CALL_LIMIT};
        if(idx>=5 || (offset&3u) || offset>=bounds[idx])break;
        uint32_t table=rd_control.runtime_tables[idx];
        if(!ram_range(table,offset+4))break;
        uint32_t fn=*(const uint32_t *)(uintptr_t)(table+offset);
        if((fn&3u) || fn<FW_SDRAM_START || fn>=FW_RUNTIME_BASE)break;
        uint32_t v=((uint32_t (*)(uint32_t,uint32_t,uint32_t,uint32_t))(uintptr_t)fn)
            (get32(p+8),get32(p+12),get32(p+16),get32(p+20));
        put32(out,v);*status=0;return 4;
    }
    default:break;
    }
    return 0;
}
static unsigned dispatch(unsigned op,const uint8_t *p,unsigned n,uint8_t *out,unsigned *status)
{
    unsigned used=dispatch_inner(op,p,n,out,status);
    if(*status || op==4u || op==10u || op==20u || op==27u || op==28u || op==29u || op==30u || op==32u || op==34u || op==41u || op==44u){
        uint32_t detail=(op<<24)|((*status&255u)<<16)|(n&65535u);
        if(op==41u && n>=4u)detail=(detail&0xffff0000u)|(get32(p)&65535u);
        record_event(2u,detail);
    }
    return used;
}
static unsigned transport_poll(void)
{
    if(in_poll)return 0;in_poll=1;polls++;last_poll=fw_tick();debug_usb_poll(&usb);
    if(usb.resets!=reset_seen || usb.session_epoch!=session_seen || usb.configured!=configured_seen){
        protocol=(debug_protocol){0};zlp_pending=0;screen_valid=0;rd_management_disconnect();
        record_event(3u,usb.session_epoch);
        /* DTR merely changes the host connection. A read-only observer must
         * not cancel an active EXEC; reset/deconfiguration still cancels it. */
        if(usb.resets!=reset_seen || (configured_seen && !usb.configured))rd_exec_cancel(4);
        reset_seen=usb.resets;session_seen=usb.session_epoch;configured_seen=usb.configured;
    }
    if(usb.ownership_fault){rd_control.last_error=0x100u|usb.fault_reason;rd_control.stop_request=1;rd_exec_cancel(8);rd_management_disconnect();}
    unsigned progress=0;
    if(usb.configured && !usb.ownership_fault){
        if(protocol.tx_size){
            unsigned count=protocol.tx_size-protocol.tx_offset;if(count>64)count=64;
            if(count && debug_usb_write(&usb,protocol.tx+protocol.tx_offset,count)){
                progress=1;protocol.tx_offset+=count;
                if(protocol.tx_offset==protocol.tx_size)zlp_pending=(protocol.tx_size%64u)==0;
            }
            if(protocol.tx_offset==protocol.tx_size){
                if(zlp_pending){if(debug_usb_write(&usb,protocol.tx,0)){zlp_pending=0;progress=1;}}
                else if(debug_usb_tx_idle(&usb)){protocol.tx_size=protocol.tx_offset=0;progress=1;}
            }
        }else if(!rd_control.stop_request){
            uint8_t packet[64];int count=debug_usb_read(&usb,packet,sizeof packet);
            if(count>0){progress=1;debug_protocol_feed(&protocol,packet,(unsigned)count,dispatch);}
            else if(count<0){rd_control.last_error=0x200;rd_control.stop_request=1;}
        }
    }
    in_poll=0;return progress;
}
static void transport_service(void)
{
    uint32_t frame=debug_usb_frame(),tick=fw_tick();
    if(!transport_poll())return;
    for(unsigned i=1;i<4096u;i++){
        if(rd_control.stop_request || ((debug_usb_frame()-frame)&0x7ffu)>=2u || fw_tick()!=tick)break;
        (void)transport_poll();
    }
}
static int module_poll(void)
{transport_service();if(rd_control.stop_request)rd_exec_cancel(1);fw_delay(1);return rd_exec_poll_flags();}
static uint32_t module_table(unsigned index)
{return index<5?rd_control.runtime_tables[index]:0;}
static const debug9688_api module_api={1,sizeof(debug9688_api),fw_tick,module_poll,module_table};
static int resident_init(rd_header *h)
{
    if(h!=&rd_control || h->magic!=RD_MAGIC || !fw_matches())return 1;
    if(h->binding_id && h->binding_id!=fw_binding_id())return 1;
    h->binding_id=fw_binding_id();
    if(debug_usb_cable_connected())return 2;
    protocol=(debug_protocol){0};screen_valid=zlp_pending=0;in_poll=0;
    reset_seen=session_seen=configured_seen=stop_tick=0;h->last_error=0;h->stop_request=0;rd_exec_reset();rd_management_reset();
    event_sequence=0;memset(event_log,0,sizeof event_log);record_event(1u,h->binding_id);
    int rc=debug_usb_open(&usb);if(rc)return rc;
    rc=fw_wake_install(h);if(rc){debug_usb_close(&usb);h->last_error=0x300u|(unsigned)rc;return 3;}
    return 0;
}
static void resident_shutdown(rd_header *h)
{
    if(exec_state==EXEC_RUNNING){rd_exec_cancel(1);return;}
    rd_management_shutdown();
    if(rd_management_input_pending())return; /* Retry reserved release at a safe GUI boundary. */
    debug_usb_close(&usb);
    if(fw_wake_remove()){h->last_error=0x400;h->state=RD_FAULT;return;}
    screen_valid=0;h->state=RD_STOPPED;record_event(4u,h->last_error);
}
static void resident_worker(void *arg)
{
    rd_header *h=arg;
    for(;;){
        if(h->state==RD_RUNNING){
            if(!h->stop_request){transport_service();rd_management_step();}
            if(exec_state==EXEC_PENDING && !h->stop_request && !protocol.tx_size && debug_usb_tx_idle(&usb))rd_exec_invoke(&module_api);
            if(h->stop_request){
                if(!stop_tick)stop_tick=fw_tick();(void)transport_poll();
                if((!protocol.tx_size && debug_usb_tx_idle(&usb)) || fw_tick()-stop_tick>=40u)h->state=RD_STOPPING;
            }
        }
        if(h->state==RD_STOPPING)resident_shutdown(h);
        fw_delay(h->state==RD_STOPPED?20u:1u);
    }
}
