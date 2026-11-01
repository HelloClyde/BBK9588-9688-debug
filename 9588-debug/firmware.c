/* SPDX-License-Identifier: GPL-3.0-only
 * Task/TCB lifecycle adapted from H1Debug firmware.c. Addresses and code
 * fingerprints are independently derived from the selected 9588 firmware. */
#include "firmware.h"
#define R8(a) (*(volatile uint8_t *)(uintptr_t)(a))
#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))
static rd_header *wake_owner;
static volatile uint32_t wake_pending,wake_at;
static uint32_t wake_installed;
static uint32_t match_error;
#ifdef RD_UNIVERSAL_LAUNCHER
const struct rd_launcher_profile *rd_selected_profile;
#endif
static uint32_t irq_lock(void)
{
    uint32_t v,m;
    __asm__ volatile("mfc0 %0,$12\n\tnop":"=r"(v)::"memory");m=v&~1u;
    __asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(m):"memory");return v;
}
static void irq_unlock(uint32_t v)
{__asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(v):"memory");}
static uint32_t code_crc(uint32_t address,uint32_t count)
{
    const volatile uint8_t *p=(const volatile uint8_t *)(uintptr_t)address;
    uint32_t c=~0u;
    while(count--){c^=*p++;for(unsigned i=0;i<8;i++)c=(c>>1)^(0xedb88320u&(0u-(c&1u)));}
    return ~c;
}
static unsigned check_guards(const struct rd_fw_guard *guards,unsigned count)
{
    for(unsigned i=0;i<count;i++)
        if(code_crc(guards[i].address,guards[i].length)!=guards[i].crc32)return i+1u;
    return 0;
}
#ifndef RD_UNIVERSAL_LAUNCHER
static int runtime_tables_match(void)
{
    return R32(FW_RUNTIME_BASE+4u)==FW_GUI_TABLE &&
        R32(FW_RUNTIME_BASE+8u)==FW_FS_TABLE &&
        R32(FW_RUNTIME_BASE+12u)==FW_SYS_TABLE &&
        R32(FW_RUNTIME_BASE+16u)==FW_MEM_TABLE &&
        R32(FW_RUNTIME_BASE+20u)==FW_RES_TABLE;
}
#endif
uint32_t fw_selected_soc(void)
{
#ifdef RD_UNIVERSAL_LAUNCHER
    return rd_selected_profile ? rd_selected_profile->soc : 0u;
#else
    return RD_SOC_ID;
#endif
}
uint32_t fw_match_error(void){return match_error;}
int fw_notice_safe(void)
{
    /* This test is independent of the private RTOS/USB guards. An exact
     * exported message-box binding may still report a failed private guard.
     * An unknown GUI is never invoked merely to display an error. */
#ifdef RD_UNIVERSAL_LAUNCHER
    for(unsigned i=0;i<RD_LAUNCHER_PROFILE_COUNT;i++){
        const struct rd_launcher_profile *p=&rd_launcher_profiles[i];
        if(R32(p->fw_runtime_base+4u)==p->fw_gui_table && p->notice_guard_count &&
           !check_guards(p->notice_guards,p->notice_guard_count))return 1;
    }
#if RD_EXTRA_NOTICE_PROFILE_COUNT
    for(unsigned i=0;i<RD_EXTRA_NOTICE_PROFILE_COUNT;i++){
        const struct rd_notice_profile *p=&rd_extra_notice_profiles[i];
        if(R32(p->runtime_base+4u)==p->gui_table && p->count &&
           !check_guards(p->guards,p->count))return 1;
    }
#endif
    return 0;
#else
    return R32(FW_RUNTIME_BASE+4u)==FW_GUI_TABLE && RD_NOTICE_GUARDS_COUNT &&
        !check_guards(rd_notice_guards,RD_NOTICE_GUARDS_COUNT);
#endif
}
int fw_matches(void)
{
#ifdef RD_UNIVERSAL_LAUNCHER
    rd_selected_profile=0;
    match_error=0xff00u;
    for(unsigned i=0;i<RD_LAUNCHER_PROFILE_COUNT;i++){
        const struct rd_launcher_profile *p=&rd_launcher_profiles[i];
        if(R32(p->fw_runtime_base+4u)!=p->fw_gui_table)continue;
        match_error=(p->soc<<16)|0xff01u;
        if(R32(p->fw_runtime_base+8u)!=p->fw_fs_table ||
           R32(p->fw_runtime_base+12u)!=p->fw_sys_table ||
           R32(p->fw_runtime_base+16u)!=p->fw_mem_table ||
           R32(p->fw_runtime_base+20u)!=p->fw_res_table)continue;
        unsigned failed=check_guards(p->guards,p->guard_count);
        if(failed){match_error=(p->soc<<16)|failed;continue;}
        failed=check_guards(p->notice_guards,p->notice_guard_count);
        if(failed){match_error=(p->soc<<16)|0x8000u|failed;continue;}
        if(rd_selected_profile){
            rd_selected_profile=0;
            match_error=0xff02u; /* More than one complete predicate matched. */
            return 0;
        }
        rd_selected_profile=p;
    }
    if(rd_selected_profile){match_error=0;return 1;}
    return 0;
#else
    /* ROM identity does not prove that the BDA loader's live imports are
     * intact. Reject before using any table or calling private services. */
    if(!runtime_tables_match()){
        match_error=(fw_selected_soc()<<16)|0xff01u;
        return 0;
    }
    unsigned failed=check_guards(rd_fw_guards,RD_FW_GUARDS_COUNT);
    match_error=failed ? (fw_selected_soc()<<16)|failed : 0;
    if(failed)return 0;
    failed=check_guards(rd_notice_guards,RD_NOTICE_GUARDS_COUNT);
    if(failed){match_error=(fw_selected_soc()<<16)|0x8000u|failed;return 0;}
    return 1;
#endif
}
uint32_t fw_tick(void)
{return ((uint32_t (*)(void))(uintptr_t)FW_RAW_TICK)();}
void fw_delay(uint32_t ticks)
{
    if(!ticks)return;
    uint32_t current=R32(FW_CURRENT_TCB);
    if(!current || R8(current+0x35u)!=RD_PRIORITY){
        uint32_t start=fw_tick();
        for(unsigned n=0;n<20000000u && fw_tick()-start<ticks;n++){}
        return;
    }
    uint32_t saved=irq_lock();wake_at=fw_tick()+ticks;wake_pending=1;
    ((void (*)(uint32_t))(uintptr_t)FW_TASK_DELAY)(1u);irq_unlock(saved);
}
static int heap_range(uint32_t a,uint32_t n)
{return a>=FW_SDRAM_START && a<FW_RUNTIME_BASE && n<=FW_RUNTIME_BASE-a;}
static void tick_irq(void *argument)
{
    ((void (*)(void *))(uintptr_t)FW_TICK_IRQ_HANDLER)(argument);
    if(wake_pending && (int32_t)(fw_tick()-wake_at)>=0){
        uint32_t tcb=R32(FW_TASK_TABLE+4u*RD_PRIORITY);
        if(!(tcb&3u) && heap_range(tcb,0x70u) &&
           R32(tcb+4u)==(uint32_t)(uintptr_t)wake_owner && R32(tcb+0x30u)){
            unsigned result=((unsigned (*)(unsigned))(uintptr_t)FW_TASK_RESUME)(RD_PRIORITY);
            if(!result)wake_pending=0;
        }
    }
}
int fw_wake_install(rd_header *h)
{
    uint32_t saved=irq_lock();int result=0;
    if(wake_installed || R32(FW_TICK_IRQ_SLOT)!=FW_TICK_IRQ_HANDLER || R32(FW_TICK_IRQ_SLOT+4u))result=1;
    else {wake_owner=h;wake_installed=1;R32(FW_TICK_IRQ_SLOT)=(uint32_t)(uintptr_t)tick_irq;}
    irq_unlock(saved);return result;
}
int fw_wake_remove(void)
{
    uint32_t saved=irq_lock();int result=0;
    if(wake_installed){
        if(R32(FW_TICK_IRQ_SLOT)!=(uint32_t)(uintptr_t)tick_irq || R32(FW_TICK_IRQ_SLOT+4u))result=1;
        else {R32(FW_TICK_IRQ_SLOT)=FW_TICK_IRQ_HANDLER;wake_installed=0;}
    }
    irq_unlock(saved);return result;
}
void *fw_alloc(uint32_t n)
{return ((void *(*)(uint32_t))(uintptr_t)FW_GLOBAL_ALLOC)(n);}
void fw_free(void *p)
{((void (*)(void *))(uintptr_t)FW_GLOBAL_FREE)(p);}
static int resident_pointer(const rd_header *h,uint32_t a)
{return !(a&3u) && a>=h->base && a-h->base<h->size;}
static int valid_control(rd_header *h)
{
    uint32_t a=(uint32_t)(uintptr_t)h;
    if((a&31u) || !heap_range(a,sizeof *h))return 0;
    if(h->magic!=RD_MAGIC || h->version!=RD_VERSION || h->base!=a ||
       h->priority!=RD_PRIORITY || h->size<sizeof *h || h->size>0x400000u ||
       !heap_range(a,h->size) || !heap_range(h->allocation,h->size+31u) ||
       h->allocation>a || a-h->allocation>31u || h->state>RD_FAULT)return 0;
    return resident_pointer(h,(uint32_t)(uintptr_t)h->init) &&
        resident_pointer(h,(uint32_t)(uintptr_t)h->worker) &&
        resident_pointer(h,(uint32_t)(uintptr_t)h->shutdown) &&
        resident_pointer(h,h->stack_bottom) && resident_pointer(h,h->stack_top) &&
        h->stack_top>=h->stack_bottom && h->stack_top-h->stack_bottom==RD_STACK_BYTES-4u;
}
int fw_task_slot_empty(void)
{return R32(FW_TASK_TABLE+4u*RD_PRIORITY)==0;}
rd_header *fw_find_resident(void)
{
    uint32_t tcb=R32(FW_TASK_TABLE+4u*RD_PRIORITY);
    if((tcb&3u) || !heap_range(tcb,0x70u) || R8(tcb+0x35u)!=RD_PRIORITY)return 0;
    rd_header *h=(rd_header *)(uintptr_t)R32(tcb+4u);
    if(!valid_control(h) || R32(tcb+8u)!=h->stack_bottom || R32(tcb+12u)!=RD_STACK_BYTES/4u)return 0;
    return h;
}
rd_header *fw_task_control(void){return fw_find_resident();}
int fw_task_create(rd_header *h)
{
    typedef unsigned (*create_fn)(void (*)(void *),void *,uint32_t *,unsigned,
        unsigned,uint32_t *,uint32_t,void *,unsigned);
    if(!valid_control(h) || !fw_task_slot_empty())return -1;
    if(R8(FW_OS_RUNNING)!=1 || R8(FW_SCHED_LOCK) || R8(FW_INT_NESTING))return -2;
    return (int)((create_fn)(uintptr_t)FW_TASK_CREATE_EXT)(h->worker,h,
        (uint32_t *)(uintptr_t)h->stack_top,RD_PRIORITY,0x3936u,
        (uint32_t *)(uintptr_t)h->stack_bottom,RD_STACK_BYTES/4u,h,0u);
}
