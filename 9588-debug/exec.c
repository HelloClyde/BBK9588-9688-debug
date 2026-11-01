/* SPDX-License-Identifier: GPL-3.0-only */
#include "exec.h"
#include "debug.h"
#include "firmware.h"
#include "protocol.h"
static uint8_t image[EXEC_BYTES] __attribute__((aligned(32)));
static uint8_t args[256],output[512];
uint32_t exec_state;
static uint32_t image_size,memory_size,entry,expected_crc,received;
static uint32_t opcode,budget,arg_size,output_size,start_tick,end_tick,cancel_flags;
static int result;
static uint32_t get32(const uint8_t *p)
{return p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static void put32(uint8_t *p,uint32_t v){for(unsigned i=0;i<4;i++)p[i]=(uint8_t)(v>>(8*i));}
static unsigned words(uint8_t *p,const uint32_t *v,unsigned n)
{for(unsigned i=0;i<n;i++)put32(p+4*i,v[i]);return 4*n;}
void rd_exec_reset(void)
{exec_state=EXEC_EMPTY;image_size=memory_size=entry=received=cancel_flags=output_size=0;}
void rd_exec_cancel(unsigned flag)
{
    if(exec_state==EXEC_PENDING){cancel_flags|=flag;result=-1;start_tick=end_tick=fw_tick();exec_state=EXEC_DONE;}
    else if(exec_state==EXEC_RUNNING)cancel_flags|=flag;
}
int rd_exec_poll_flags(void)
{
    if(exec_state==EXEC_RUNNING && fw_tick()-start_tick>=budget)cancel_flags|=2u;
    return (int)cancel_flags;
}
unsigned rd_exec_command(unsigned op,const uint8_t *p,unsigned n,uint8_t *out,unsigned *status)
{
    *status=1;
    switch(op){
    case 26:{
        if(n)return 0;
        uint32_t v[]={1,(uint32_t)(uintptr_t)image,EXEC_BYTES,exec_state,image_size,memory_size,entry,256,512,RD_STACK_BYTES};
        *status=0;return words(out,v,10);
    }
    case 27:
        if(n!=16)return 0;
        if(exec_state==EXEC_RUNNING || exec_state==EXEC_PENDING){*status=2;return 0;}
        {
            uint32_t a=get32(p),b=get32(p+4),c=get32(p+8);
            if(!a || a>EXEC_BYTES || b<a || b>EXEC_BYTES || c>=a || (c&3u))return 0;
            image_size=a;memory_size=b;entry=c;expected_crc=get32(p+12);received=0;
            memset(image,0,EXEC_BYTES);output_size=0;exec_state=EXEC_LOADING;
        }
        break;
    case 28:
        if(n<5 || exec_state!=EXEC_LOADING || get32(p)!=received || n-4>image_size-received)return 0;
        memcpy(image+received,p+4,n-4);received+=n-4;break;
    case 29:
        if(n || exec_state!=EXEC_LOADING || received!=image_size)return 0;
        if(debug_crc32(image,image_size)!=expected_crc){*status=3;return 0;}
        rd_cache_sync(image,image+memory_size);exec_state=EXEC_READY;break;
    case 30:
        if(n<8 || n>264 || (exec_state!=EXEC_READY && exec_state!=EXEC_DONE))return 0;
        if(!get32(p+4) || get32(p+4)>400u)return 0;
        opcode=get32(p);budget=get32(p+4);arg_size=n-8;memcpy(args,p+8,arg_size);
        output_size=cancel_flags=0;result=0;exec_state=EXEC_PENDING;break;
    case 31:{
        if(n)return 0;
        uint32_t v[]={1,exec_state,(uint32_t)result,cancel_flags,start_tick,end_tick,budget,output_size,received};
        *status=0;return words(out,v,9);
    }
    case 32:
        if(n)return 0;rd_exec_cancel(1);break;
    case 33:{
        if(n!=8 || exec_state!=EXEC_DONE)return 0;
        uint32_t offset=get32(p),wanted=get32(p+4);
        if(!wanted || wanted>504 || offset>output_size)return 0;
        uint32_t count=output_size-offset;if(count>wanted)count=wanted;
        put32(out,offset);put32(out+4,output_size);memcpy(out+8,output+offset,count);*status=0;return 8+count;
    }
    case 34:
        if(n || exec_state==EXEC_RUNNING || exec_state==EXEC_PENDING)return 0;
        rd_exec_reset();break;
    default:return 0;
    }
    *status=0;return 0;
}
void rd_exec_invoke(const debug9588_api *api)
{
    if(exec_state!=EXEC_PENDING)return;
    memset(output,0,sizeof output);
    debug9588_call call={opcode,args,arg_size,output,sizeof output,0};
    start_tick=fw_tick();end_tick=0;exec_state=EXEC_RUNNING;
    result=((int (*)(const debug9588_api *,debug9588_call *))(uintptr_t)(image+entry))(api,&call);
    end_tick=fw_tick();
    if(call.output_size>sizeof output){result=-2;output_size=0;}else output_size=call.output_size;
    exec_state=EXEC_DONE;
}
