/* SPDX-License-Identifier: GPL-3.0-only
 * JZ4730 dedicated FIFO CDC transport, adapted from 9688 Debug (GPL-3.0-only).
 * Controller semantics follow JZ4730 Programming Manual ch.21; 9588
 * private bindings are independently checked in the selected profile.
 */
#include "usb.h"
#include "firmware.h"
#if !RD_USB_CONTROLLER_FIFO || !RD_SOC_JZ4730
#error "dedicated FIFO transport requires a checked JZ4730 profile"
#endif
#define U FW_UDC_BASE
#define G FW_USB_CABLE_GPIO_BASE
#define CABLE FW_USB_CABLE_GPIO_MASK
#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define R8(a) (*(volatile uint8_t *)(uintptr_t)(a))
#define ICR(ep) (U+0x20u*(ep))
#define ISR(ep) (ICR(ep)+4u)
#define IBSR(ep) (ICR(ep)+8u)
#define IMPR(ep) (ICR(ep)+12u)
#define OCR(ep) (U+0x200u+0x20u*(ep))
#define OSR(ep) (OCR(ep)+4u)
#define OMPR(ep) (OCR(ep)+12u)
#define UDCFGR (U+0x400u)
#define UDCR (U+0x404u)
#define UDSR (U+0x408u)
#define UDIPR (U+0x40cu)
#define UDIMR (U+0x410u)
#define UIPR (U+0x414u)
#define UIMR (U+0x418u)
#define TCFM (U+0x41cu)
#define TZERO (U+0x420u)
#define USCAR (U+0x500u)
#define INFO(ep) (U+0x504u+4u*(ep))
#define RFIFO (U+0x800u)
#define TFIFO0 (U+0x840u)
#define TFIFO2 (TFIFO0+32u+16u) /* EP0=8 words; EP1=4 words. */
#define DEVICE_EVENTS 0x3bu
#define ENDPOINT_EVENTS 0x00e1001fu
#define BULK_OUT_BIT (1u<<21)
#define IN_READY 0x40u

static const uint16_t saved_offsets[36]={
    0x000,0x008,0x00c,0x020,0x028,0x02c,0x040,0x048,0x04c,
    0x060,0x068,0x06c,0x080,0x088,0x08c,
    0x200,0x20c,0x2a0,0x2ac,0x2c0,0x2cc,0x2e0,0x2ec,
    0x400,0x404,0x410,0x418,0x500,
    0x504,0x508,0x50c,0x510,0x514,0x518,0x51c,0x520
};
static const uint8_t device[]={18,1,0x10,1,2,2,1,32,0xc4,7,(RD_USB_PID&255u),(RD_USB_PID>>8),0,1,1,2,3,1};
/* Fixed JZ4730 directions: EP1 notification IN, EP2 bulk IN, EP5 bulk OUT. */
static const uint8_t config[]={
    9,2,67,0,2,1,0,0x80,50,
    9,4,0,0,1,2,2,1,0,
    5,0x24,0,0x10,1,5,0x24,1,0,1,4,0x24,2,2,5,0x24,6,0,1,
    7,5,0x81,3,16,0,16,
    9,4,1,0,2,0x0a,0,0,0,
    7,5,0x05,2,64,0,0,7,5,0x82,2,64,0,0
};
static uint32_t opens,faults,first_valid,first_open,first_fault[24];

static uint32_t lock(void)
{
    uint32_t old,masked;
    __asm__ volatile("mfc0 %0,$12\n\tnop":"=r"(old)::"memory");masked=old&~1u;
    __asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(masked):"memory");return old;
}
static void unlock(uint32_t old)
{__asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(old):"memory");}
static unsigned le16(const uint8_t *p){return p[0]|((unsigned)p[1]<<8);}
int debug_usb_cable_connected(void)
{
    unsigned detected=!!(R32(G)&CABLE);
#if FW_USB_CABLE_ACTIVE_LOW
    detected=!detected;
#endif
    return (int)detected;
}
#if FW_USB_PULLUP_MASK
static void pullup_set(unsigned connected)
{
    unsigned high=connected ? !FW_USB_PULLUP_ACTIVE_LOW : FW_USB_PULLUP_ACTIVE_LOW;
    uint32_t old=R32(FW_USB_PULLUP_BASE);
    R32(FW_USB_PULLUP_BASE)=(old&~FW_USB_PULLUP_MASK)|(high?FW_USB_PULLUP_MASK:0u);
}
static void pullup_save_disconnect(debug_usb *u)
{
    u->pullup_data=R32(FW_USB_PULLUP_BASE);
    u->pullup_dir=R32(FW_USB_PULLUP_DIR_ADDR);
    u->pullup_func=R32(FW_USB_PULLUP_FUNC_ADDR);
    u->pullup_irq=R32(FW_USB_PULLUP_IRQ_ADDR);
    pullup_set(0);
    R32(FW_USB_PULLUP_IRQ_ADDR)&=~FW_USB_PULLUP_MASK;
    R32(FW_USB_PULLUP_FUNC_ADDR)=(R32(FW_USB_PULLUP_FUNC_ADDR)&~FW_USB_PULLUP_FUNC_MASK)|
        FW_USB_PULLUP_FUNC_VALUE;
    R32(FW_USB_PULLUP_DIR_ADDR)|=FW_USB_PULLUP_MASK;
}
static int pullup_owned(void)
{
    unsigned high=!FW_USB_PULLUP_ACTIVE_LOW;
    return !!(R32(FW_USB_PULLUP_BASE)&FW_USB_PULLUP_MASK)==high &&
        (R32(FW_USB_PULLUP_DIR_ADDR)&FW_USB_PULLUP_MASK)==FW_USB_PULLUP_MASK &&
        (R32(FW_USB_PULLUP_FUNC_ADDR)&FW_USB_PULLUP_FUNC_MASK)==FW_USB_PULLUP_FUNC_VALUE &&
        !(R32(FW_USB_PULLUP_IRQ_ADDR)&FW_USB_PULLUP_MASK);
}
static void pullup_restore(const debug_usb *u)
{
    R32(FW_USB_PULLUP_DIR_ADDR)=(R32(FW_USB_PULLUP_DIR_ADDR)&~FW_USB_PULLUP_MASK)|
        (u->pullup_dir&FW_USB_PULLUP_MASK);
    R32(FW_USB_PULLUP_FUNC_ADDR)=(R32(FW_USB_PULLUP_FUNC_ADDR)&~FW_USB_PULLUP_FUNC_MASK)|
        (u->pullup_func&FW_USB_PULLUP_FUNC_MASK);
    R32(FW_USB_PULLUP_IRQ_ADDR)=(R32(FW_USB_PULLUP_IRQ_ADDR)&~FW_USB_PULLUP_MASK)|
        (u->pullup_irq&FW_USB_PULLUP_MASK);
    R32(FW_USB_PULLUP_BASE)=(R32(FW_USB_PULLUP_BASE)&~FW_USB_PULLUP_MASK)|
        (u->pullup_data&FW_USB_PULLUP_MASK);
}
#else
static void pullup_set(unsigned connected){(void)connected;}
static void pullup_save_disconnect(debug_usb *u){(void)u;}
static int pullup_owned(void){return 1;}
static void pullup_restore(const debug_usb *u){(void)u;}
#endif

static int clock_active(void){return !(R32(FW_CPM_CLKGR)&FW_CPM_USB_GATE_MASK);}
unsigned debug_usb_frame(void){return clock_active() ? (R32(UDSR)>>18)&0x7ffu : 0u;}

static void snapshot(const debug_usb *u,uint32_t *v,unsigned events,unsigned reason)
{
    uint32_t cp0;__asm__ volatile("mfc0 %0,$12\n\tnop":"=r"(cp0));
    v[0]=fw_tick();v[1]=reason;v[2]=events;
    v[3]=R32(FW_CPM_CPCCR);v[4]=R32(FW_CPM_CPPCR);
    v[5]=R32(FW_CPM_CLKGR);v[6]=R32(FW_CPM_OPCR);v[7]=cp0;
    v[8]=R32(FW_INTC_IMR);v[9]=R32(G+0x24u);v[10]=R32(G);v[11]=R32(G+0x28u);
    unsigned active=!(v[5]&FW_CPM_USB_GATE_MASK);
    v[12]=active?R32(UDCFGR):0;v[13]=active?R32(UDIMR):0;v[14]=active?R32(UIMR):0;
    v[15]=R32(FW_USB_SBA_CTRL);v[16]=active?debug_usb_frame():0;
    v[17]=active?(R32(UDSR)&15u):0;v[18]=u->soc_tx_pending;
    v[19]=u->configured;v[20]=u->resets;v[21]=u->session_epoch;
    v[22]=u->rx_packets;v[23]=u->tx_packets;
}
void debug_usb_diagnostics(const debug_usb *u,uint32_t out[DEBUG_USB_DIAG_WORDS])
{
    /* v3 identifies dedicated JZ4730 register meanings at snapshot words12..18. */
    out[0]=3;out[1]=56;out[2]=opens;out[3]=faults;out[4]=first_valid;out[5]=first_open;
    out[6]=24;out[7]=0;snapshot(u,out+8,0,u->fault_reason);
    for(unsigned i=0;i<24;i++)out[32+i]=first_fault[i];
}
static void fault(debug_usb *u,unsigned reason,unsigned events)
{
    if(u->ownership_fault)return;
    u->ownership_fault=1;u->fault_reason=reason;
    u->fault_imr=R32(FW_INTC_IMR);u->fault_pdmask=R32(G+0x24u);u->fault_pin=R32(G);
    ++faults;
    if(!first_valid){snapshot(u,first_fault,events,reason);first_valid=1;first_open=opens;}
}
static void flush_in(unsigned ep,unsigned type)
{R32(ICR(ep))=type|2u;R32(ICR(ep))=type;R32(ISR(ep))=0;R32(UIPR)=1u<<ep;}
static unsigned receive(unsigned ep,uint8_t *dest,unsigned capacity)
{
    uint32_t status=R32(OSR(ep));unsigned n=(status>>11)&0x7ffu;
    if(n>64u || n>capacity)return 0x10000u|n;
    unsigned off=0;
    while(n-off>=4u){uint32_t word=R32(RFIFO);for(unsigned j=0;j<4;j++)dest[off++]=(uint8_t)(word>>(8*j));}
    while(off<n)dest[off++]=R8(RFIFO);
    if(!n)(void)R32(TCFM); /* Dedicated zero-OUT acknowledgement. */
    R32(OSR(ep))=status&~0x30u;R32(UIPR)=1u<<(16u+ep);return n;
}
static void discard_out(unsigned ep)
{
    if(R32(OSR(ep))&0x30u){uint8_t ignored[64];(void)receive(ep,ignored,sizeof ignored);}
}
static void flush_session(debug_usb *u)
{
    /* F flushes payload, not endpoint configuration or the host DATA toggle.
     * SET_CONFIGURATION/bus reset performs the USB-defined toggle reset. */
    flush_in(2,0x20u);discard_out(5);u->soc_rx_length=0;u->soc_tx_pending=0;
}
static void endpoints(debug_usb *u)
{
    for(unsigned ep=0;ep<5;ep++){
        unsigned type=ep==0?0u:ep==1?0x30u:ep==4?0x10u:0x20u;
        flush_in(ep,type);
        R32(IBSR(ep))=ep==0?8u:ep==1?4u:ep==2?16u:0u;
        R32(IMPR(ep))=ep==0?32u:ep==1?16u:ep==2?64u:0u;
    }
    R32(OCR(0))=0;R32(OMPR(0))=32;
    R32(OCR(5))=0x20;R32(OMPR(5))=64;
    R32(OCR(6))=0x20;R32(OMPR(6))=0;
    R32(OCR(7))=0x10;R32(OMPR(7))=0;
    R32(INFO(0))=0x01000080u;
    R32(INFO(1))=0x008000f1u;
    R32(INFO(2))=0x020008d2u;
    R32(INFO(5))=0x020008c5u;
    R32(INFO(3))=R32(INFO(4))=R32(INFO(6))=R32(INFO(7))=0;
    R32(OSR(0))=R32(OSR(5))=R32(OSR(6))=R32(OSR(7))=0;
    R32(UIPR)=ENDPOINT_EVENTS;
    u->soc_tx_pending=u->soc_control_busy=u->soc_control_zlp=u->soc_rx_length=0;
}
static void send_packet(unsigned ep,const uint8_t *data,unsigned length)
{
    uint32_t fifo=ep==0?TFIFO0:TFIFO2;
    R32(ISR(ep))=0;R32(UIPR)=1u<<ep;
    if(!length){R32(TZERO)=0;R32(fifo)=0;}
    else {
        unsigned i=0;
        while(length-i>=4u){uint32_t w=data[i]|((uint32_t)data[i+1]<<8)|((uint32_t)data[i+2]<<16)|((uint32_t)data[i+3]<<24);R32(fifo)=w;i+=4;}
        while(i<length)R8(fifo)=data[i++]; /* Native firmware uses SB tails. */
    }
    R32(TCFM)=0;
    /* An IN token received while the CPU filled an unconfirmed FIFO was
     * NAKed. Do not count that stale indication as consumption of this
     * packet. Clearing a real token in this tiny window is conservative:
     * a later IN retries/observes the empty FIFO before we reuse it. */
    R32(ISR(ep))=0;R32(UIPR)=1u<<ep;
}
static void stall(debug_usb *u)
{R32(ICR(0))|=1u;R32(OCR(0))|=1u;u->control_phase=0;++u->stalls;}
static void send_control(debug_usb *u)
{
    if(u->soc_control_busy)return;
    unsigned remaining=u->control_length-u->control_offset;
    if(remaining){
        unsigned n=remaining>32u?32u:remaining;
        send_packet(0,u->control+u->control_offset,n);u->control_offset+=(uint16_t)n;u->soc_control_busy=1;
    }else if(u->soc_control_zlp){send_packet(0,u->control,0);u->soc_control_zlp=0;u->soc_control_busy=1;}
    else u->control_phase=0;
}
static void reply(debug_usb *u,const uint8_t *data,unsigned length,unsigned requested)
{
    if(length>requested)length=requested;
    for(unsigned i=0;i<length;i++)u->control[i]=data[i];
    u->control_length=(uint16_t)length;u->control_offset=0;
    u->soc_control_zlp=(length<requested && length%32u==0u);u->control_phase=1;send_control(u);
}
static void status(debug_usb *u)
{u->control_length=u->control_offset=0;u->soc_control_zlp=1;u->control_phase=1;send_control(u);}
static void descriptor(debug_usb *u,unsigned type,unsigned index,unsigned requested)
{
    if(type==1 && !index){reply(u,device,sizeof device,requested);return;}
    if(type==2 && !index){reply(u,config,sizeof config,requested);return;}
    if(type==3){
        if(!index){const uint8_t language[]={4,3,9,4};reply(u,language,4,requested);return;}
        const char *s=index==1?"BBK 9588 lab":index==2?"9588 USB Debug JZ4730":index==3?"9588DBG-JZ4730":0;
        if(s){
            unsigned n=2;while(*s && n+2<=sizeof u->control){u->control[n++]=(uint8_t)*s++;u->control[n++]=0;}
            u->control[0]=(uint8_t)n;u->control[1]=3;reply(u,u->control,n,requested);return;
        }
    }
    stall(u);
}
static void setup(debug_usb *u,const uint8_t s[8])
{
    unsigned value=le16(s+2),index=le16(s+4),length=le16(s+6);++u->setups;
    flush_in(0,0);R32(OCR(0))=0;u->soc_control_busy=u->soc_control_zlp=0;u->control_phase=0;
    if(s[0]==0x80 && s[1]==6){descriptor(u,value>>8,value&255,length);return;}
    if(s[0]==0x21 && s[1]==0x20 && !value && !index && length==7){u->control_phase=2;return;}
    if(s[0]==0xa1 && s[1]==0x21 && !value && !index && length==7){reply(u,u->line,7,7);return;}
    if(s[0]==0x21 && s[1]==0x22 && !index && !length && !(value&~3u)){
        if((u->control_lines^value)&1u){++u->session_epoch;flush_session(u);}
        u->control_lines=(uint16_t)value;status(u);return;
    }
    if(s[0]==0x21 && s[1]==0x23 && !index && !length){status(u);return;}
    /* The PM Table1-3 hardware handles SET_ADDRESS, GET/SET_CONFIGURATION,
     * GET/SET_INTERFACE, GET_STATUS and CLEAR/SET_FEATURE. They deliberately
     * do not arrive here; SC/SI are handled from UDIPR in poll(). */
    stall(u);
}
int debug_usb_open(debug_usb *u)
{
    if(!u)return 1;
    uint8_t *clear=(uint8_t *)u;for(unsigned i=0;i<sizeof *u;i++)clear[i]=0;
    if(!fw_matches())return 1;
    if(debug_usb_cable_connected() || (!(R32(FW_CPM_CLKGR)&FW_CPM_USB_GATE_MASK) && (R32(UDSR)&15u)))return 2;
    uint32_t irq=lock();
    pullup_save_disconnect(u);
    u->clock_gate=R32(FW_CPM_CLKGR);u->opcr=R32(FW_CPM_OPCR);u->imr=R32(FW_INTC_IMR);
    u->pdmask=R32(G+0x24u);u->pdflag=R32(G+0x28u);u->soc_sba=R32(FW_USB_SBA_CTRL);
    R32(FW_INTC_IMSR)=FW_USB_IRQ_MASK;R32(G+0x24u)|=CABLE;
    R32(FW_CPM_CLKGR)&=~FW_CPM_USB_GATE_MASK;
    R32(FW_CPM_OPCR)&=~FW_CPM_USB_PHY_MASK; /* JZ4730 SPEND0 is active-high suspend. */
    R32(FW_USB_SBA_CTRL)=(R32(FW_USB_SBA_CTRL)&~FW_USB_SBA_MASK)|FW_USB_SBA_DEVICE_VALUE;
    for(unsigned i=0;i<36;i++)u->soc_saved[i]=R32(U+saved_offsets[i]);
    R32(UDIMR)=DEVICE_EVENTS;R32(UIMR)=0xffffffffu;
    R32(UDCFGR)=3u;R32(UDCR)=0;R32(USCAR)=0xffffu;endpoints(u);R32(UDIPR)=DEVICE_EVENTS;
    u->line[1]=0xc2;u->line[2]=1;u->line[6]=8;u->owned=1;++opens;
    pullup_set(1);unlock(irq);return 0;
}
void debug_usb_poll(debug_usb *u)
{
    if(!u || !u->owned || u->ownership_fault)return;
    unsigned reason=0;
    if(!pullup_owned())reason|=2048u;
    if(!(R32(FW_INTC_IMR)&FW_USB_IRQ_MASK))reason|=8;
    if(!(R32(G+0x24u)&CABLE))reason|=16;
    if((R32(FW_CPM_CLKGR)&FW_CPM_USB_GATE_MASK) || (R32(FW_CPM_OPCR)&FW_CPM_USB_PHY_MASK) ||
       (R32(FW_USB_SBA_CTRL)&FW_USB_SBA_MASK)!=FW_USB_SBA_DEVICE_VALUE)reason|=128;
    if(reason){fault(u,reason,0);return;}
    uint32_t events=R32(UDIPR);
    if(R32(UDIMR)!=DEVICE_EVENTS || R32(UIMR)!=0xffffffffu){fault(u,1,events);return;}
    if(events&8u){
        ++u->resets;++u->session_epoch;u->configured=0;u->control_phase=0;u->control_lines=0;
        endpoints(u);R32(UDIPR)=events&DEVICE_EVENTS;return;
    }
    if(events&1u){
        unsigned configured=R32(UDSR)&15u;
        if(configured>1u){fault(u,256u,events);return;}
        u->configured=(uint8_t)configured;u->control_phase=0;u->control_lines=0;
        ++u->session_epoch;flush_session(u);
    }
    if(events&2u){
        uint32_t setting=R32(UDSR);
        if(((setting>>4)&15u)>1u || ((setting>>8)&15u)){fault(u,256u,events);return;}
    }
    if(events&DEVICE_EVENTS)R32(UDIPR)=events&DEVICE_EVENTS;
    uint32_t pending=R32(UIPR),out0=R32(OSR(0));
    uint32_t out_pending=pending&0x00e10000u;
    /* There is no RX endpoint selector or packet-order register. The native
     * firmware also dispatches one OUT bit at a time. Refuse ambiguous
     * simultaneous OUT indications rather than execute misordered bytes. */
    if(out_pending && (out_pending&(out_pending-1u))){fault(u,1024u,events);return;}
    if((pending&(1u<<16)) && (out0&0x30u)){
        uint8_t data[32];unsigned n=receive(0,data,sizeof data);
        if(n>32u){fault(u,512u,events);return;}
        if((out0&0x30u)==0x20u && n==8u)setup(u,data);
        else if(u->control_phase==2 && n==7u){for(unsigned i=0;i<7;i++)u->line[i]=data[i];status(u);}
        else if(!n){u->control_phase=0;u->soc_control_busy=u->soc_control_zlp=0;}
        else stall(u);
    }
    if((R32(UIPR)&1u) || (R32(ISR(0))&IN_READY)){
        R32(ISR(0))=0;R32(UIPR)=1;u->soc_control_busy=0;
        if(u->control_phase==1)send_control(u);
    }
    if(!u->soc_rx_length && (R32(UIPR)&BULK_OUT_BIT) && (R32(OSR(5))&0x10u)){
        unsigned n=receive(5,u->soc_rx,sizeof u->soc_rx);
        if(n>64u){fault(u,512u,events);return;}
        if(u->configured){u->soc_rx_length=n;++u->rx_packets;}
    }
}
int debug_usb_read(debug_usb *u,uint8_t *data,unsigned capacity)
{
    if(!u || !u->owned || u->ownership_fault || !u->configured)return 0;
    if(!clock_active()){fault(u,128u,0);return 0;}
    if(u->soc_rx_length>capacity)return -1;
    unsigned n=u->soc_rx_length;for(unsigned i=0;i<n;i++)data[i]=u->soc_rx[i];u->soc_rx_length=0;return (int)n;
}
int debug_usb_tx_idle(debug_usb *u)
{
    if(!u || !u->owned || u->ownership_fault)return 1;
    if(!clock_active()){fault(u,128u,0);return 1;}
    if(u->soc_tx_pending && ((R32(UIPR)&4u) || (R32(ISR(2))&IN_READY))){
        R32(ISR(2))=0;R32(UIPR)=4;u->soc_tx_pending=0;
    }
    return !u->soc_tx_pending;
}
int debug_usb_write(debug_usb *u,const uint8_t *data,unsigned length)
{
    if(!u || !u->owned || u->ownership_fault || !u->configured || length>64u)return 0;
    if(!clock_active()){fault(u,128u,0);return 0;}
    if(!debug_usb_tx_idle(u) || u->ownership_fault)return 0;
    send_packet(2,data,length);u->soc_tx_pending=1;++u->tx_packets;return 1;
}
void debug_usb_close(debug_usb *u)
{
    if(!u || !u->owned)return;
    uint32_t disconnect_irq=lock();pullup_set(0);unlock(disconnect_irq);
    R32(FW_CPM_OPCR)|=FW_CPM_USB_PHY_MASK;
    uint32_t begin=fw_tick();for(unsigned i=0;i<100u && fw_tick()-begin<20u;i++)fw_delay(1);
    uint32_t irq=lock();
    /* Clock ownership loss is one shutdown trigger. Re-enable our module
     * clock for bounded register cleanup, then restore the original bit. */
    R32(FW_CPM_CLKGR)&=~FW_CPM_USB_GATE_MASK;
    R32(FW_CPM_OPCR)&=~FW_CPM_USB_PHY_MASK;
    R32(UDIMR)=DEVICE_EVENTS;R32(UIMR)=0xffffffffu;
    for(unsigned ep=0;ep<5;ep++)flush_in(ep,R32(ICR(ep))&0x30u);
    unsigned out=R32(UIPR)&0x00e10000u;
    if(out==(1u<<16))discard_out(0);else if(out==BULK_OUT_BIT)discard_out(5);
    R32(UDIPR)=DEVICE_EVENTS;R32(UIPR)=ENDPOINT_EVENTS;
    /* Restore only configuration registers, never replay pending status/FIFO
     * data. The next physical bus reset establishes fresh address/toggles. */
    for(unsigned i=0;i<36;i++)if(saved_offsets[i]!=0x410u && saved_offsets[i]!=0x418u)R32(U+saved_offsets[i])=u->soc_saved[i];
    R32(U+0x410u)=u->soc_saved[25];R32(U+0x418u)=u->soc_saved[26];
    R32(FW_USB_SBA_CTRL)=(R32(FW_USB_SBA_CTRL)&~FW_USB_SBA_MASK)|(u->soc_sba&FW_USB_SBA_MASK);
    R32(FW_CPM_OPCR)=(R32(FW_CPM_OPCR)&~FW_CPM_USB_PHY_MASK)|(u->opcr&FW_CPM_USB_PHY_MASK);
    R32(FW_CPM_CLKGR)=(R32(FW_CPM_CLKGR)&~FW_CPM_USB_GATE_MASK)|(u->clock_gate&FW_CPM_USB_GATE_MASK);
    if(!(u->pdflag&CABLE))R32(G+0x28u)=CABLE;
    R32(G+0x24u)=(R32(G+0x24u)&~CABLE)|(u->pdmask&CABLE);
    pullup_restore(u);
    R32((u->imr&FW_USB_IRQ_MASK)?FW_INTC_IMSR:FW_INTC_IMCR)=FW_USB_IRQ_MASK;
    u->owned=u->configured=u->control_phase=0;u->soc_tx_pending=u->soc_rx_length=0;++u->session_epoch;unlock(irq);
}
