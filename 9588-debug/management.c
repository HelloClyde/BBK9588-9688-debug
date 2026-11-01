/* SPDX-License-Identifier: GPL-3.0-only
 * Resident file service and checked native GUI work queue. No SDK BDA globals
 * are used after another application reuses the foreground loading window. */
#include "management.h"
extern uint32_t fw_tick(void);
#include "debug.h"
#include "protocol.h"
#include "management_profiles.h"
#define R16(a) (*(volatile uint16_t *)(uintptr_t)(a))
#define R32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define PATH_MAXIMUM 240u
#define CHUNK_MAXIMUM 480u
#define FS_ERROR 5u
#define UNSUPPORTED 3u
#define NOT_FOUND 4u
#define WRONG_TARGET 6u
#define JOB_IDLE 0u
#define JOB_PENDING 1u
#define JOB_RUNNING 2u
#define JOB_DONE 3u
#define JOB_FAILED 4u
#define JOB_CANCELLED 5u
#define GUI_JOB_MESSAGE 0x7ddu
struct find_data { uint32_t cursor,size,attr;uint16_t time,date,volume;char name[0x20a];uint32_t aux; };
struct path_info { uint16_t volume,attr,volume_copy,reserved;uint32_t size,time[3]; };
static const struct mgmt_profile *binding;
static uint32_t features,serial_number;
static struct {uint32_t id,state,action,progress,total,result,crc,expected_crc;int file;uint32_t target,original_handler,deadline,flag;char path[PATH_MAXIMUM+1];} job;
static struct {uint32_t token,size,offset,expected_crc;int file;char path[PATH_MAXIMUM+1];} writer;
static struct {uint32_t token,size;int file;} reader;
static struct {uint32_t token,first,ended;struct find_data data;} listing;
int rd_management_gui_bridge(uint32_t,uint32_t,uint32_t,uint32_t);
static struct {uint32_t id,target,original,action,flag;char path[PATH_MAXIMUM+1];} gui_job;
static volatile uint32_t gui_inflight;
static uint32_t held_target,held_kind,held_code,held_arg,held_handler,held_owner,held_deadline,held_release_node,held_pool_base,held_release_pending;
static unsigned get16(const uint8_t *p){return p[0]|((unsigned)p[1]<<8);}
static uint32_t get32(const uint8_t *p){return get16(p)|((uint32_t)get16(p+2)<<16);}
static void put32(uint8_t *p,uint32_t n){for(unsigned i=0;i<4;i++)p[i]=(uint8_t)(n>>(8*i));}
static unsigned output_words(uint8_t *p,const uint32_t *v,unsigned n){for(unsigned i=0;i<n;i++)put32(p+4*i,v[i]);return n*4;}
static int range(uint32_t a,uint32_t n){return a>=0x80004000u && a<0x81c00000u && n<=0x81c00000u-a;}
static unsigned length(const char *p,unsigned max){unsigned n=0;while(n<max && p[n])n++;return n;}
static uint32_t next_token(void){if(!++serial_number)++serial_number;return serial_number;}
static uint32_t crc_update(uint32_t c,const uint8_t *p,unsigned n){while(n--){c^=*p++;for(unsigned i=0;i<8;i++)c=(c>>1)^(0xedb88320u&(0u-(c&1u)));}return c;}
static int file_valid(int f){return f && f!=-1;}
static uint32_t fs_function(unsigned offset){return R32(rd_control.runtime_tables[1]+offset);}
static int fs0(unsigned o){return ((int(*)(void))(uintptr_t)fs_function(o))();}
static int fs1(unsigned o,uint32_t a){return ((int(*)(uint32_t))(uintptr_t)fs_function(o))(a);}
static int fs2(unsigned o,uint32_t a,uint32_t b){return ((int(*)(uint32_t,uint32_t))(uintptr_t)fs_function(o))(a,b);}
static int fs3(unsigned o,uint32_t a,uint32_t b,uint32_t c){return ((int(*)(uint32_t,uint32_t,uint32_t))(uintptr_t)fs_function(o))(a,b,c);}
static int fs4(unsigned o,uint32_t a,uint32_t b,uint32_t c,uint32_t d){return ((int(*)(uint32_t,uint32_t,uint32_t,uint32_t))(uintptr_t)fs_function(o))(a,b,c,d);}
#define PTR(p) ((uint32_t)(uintptr_t)(p))
static int open_file(const char *p,int writing){return fs2(0,PTR(p),PTR(writing?"wb":"rb"));}
static int close_file(int f){return fs1(4,(uint32_t)f);}
static int stat_path(const char *p,struct path_info *s){memset(s,0,sizeof *s);return fs2(0x54,PTR(p),PTR(s));}
/* Canonical absolute GBK paths. ASCII separators cannot occur as the trail of
 * a legal multibyte character without being consumed by its lead byte. */
static unsigned decode_path(char *dst,const uint8_t *p,unsigned n,unsigned *used)
{
    if(n<2)return 1;unsigned count=get16(p);if(count<3 || count>PATH_MAXIMUM || count+2>n)return 1;
    const uint8_t *s=p+2;
    if((s[0]!='A' && s[0]!='B' && s[0]!='a' && s[0]!='b') || s[1]!=':' || (s[2]!='/' && s[2]!='\\'))return 1;
    dst[0]=(char)(s[0]&~32u);dst[1]=':';dst[2]='\\';unsigned part=3;
    for(unsigned i=3;i<count;i++){
        unsigned c=s[i];
        if(c>=0x81u && c<=0xfeu){if(i+1>=count || s[i+1]<0x40u || s[i+1]==0x7fu || s[i+1]>0xfeu)return 1;dst[i]=(char)c;++i;dst[i]=(char)s[i];continue;}
        if(c<32u || c==0x7fu || c==':' || c=='*' || c=='?' || c=='"' || c=='<' || c=='>' || c=='|')return 1;
        if(c=='/' || c=='\\'){
            unsigned k=i-part;if(!k || (k==1 && s[part]=='.') || (k==2 && s[part]=='.' && s[part+1]=='.'))return 1;
            dst[i]='\\';part=i+1;
        }else dst[i]=(char)c;
    }
    unsigned k=count-part;if((k==1 && s[part]=='.') || (k==2 && s[part]=='.' && s[part+1]=='.'))return 1;
    /* A trailing slash is accepted for a directory, but never an empty middle component. */
    dst[count]=0;*used=count+2;return 0;
}
static uint32_t irq_lock(void)
{
    uint32_t value,masked;
    __asm__ volatile("mfc0 %0,$12\n\tnop":"=r"(value)::"memory");masked=value&~1u;
    __asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(masked):"memory");return value;
}
static void irq_unlock(uint32_t value)
{__asm__ volatile("mtc0 %0,$12\n\tnop\n\tnop\n\tnop"::"r"(value):"memory");}
static int job_active(void){return job.state==JOB_PENDING || job.state==JOB_RUNNING;}
static int io_busy(void){return job_active() || reader.token || writer.token || listing.token || held_target;}
int rd_management_busy(void){return io_busy() || gui_inflight;}
static int guard_matches(const struct mgmt_guard *g,unsigned count)
{
    for(unsigned i=0;i<count;i++)if(!range(g[i].address,g[i].size) ||
        ~crc_update(~0u,(const uint8_t *)(uintptr_t)g[i].address,g[i].size)!=g[i].crc)return 0;
    return 1;
}
static int code_range(uint32_t a){return !(a&3u) && a>=0x80004000u && a<0x82000000u;}
static int valid_window(uint32_t a)
{return !(a&3u) && range(a,0x114u) && R16(a)==1u && (R16(a+2)==0x11u || R16(a+2)==0x12u) && code_range(R32(a+0x88));}
static uint32_t foreground(void)
{
    if(!binding || !(features&RD_MGMT_WINDOWS))return 0;
    uint32_t a=R32(binding->foreground);return valid_window(a)?a:0;
}
static int correct_target(uint32_t target){return target && foreground()==target;}
static int desktop_role(uint32_t target){return valid_window(target) && (R32(target+0x88)==binding->desktop_handler || R32(target+0x88)==binding->grid_handler);}
static int critical_pc(uint32_t pc)
{return pc>=binding->critical_start && pc<binding->critical_end;}
static int queue_quiescent(void)
{
    uint32_t current=R32(binding->current_tcb);
    for(unsigned priority=0;priority<64u;priority++){
        if(priority==RD_PRIORITY)continue;
        uint32_t tcb=R32(binding->task_table+4u*priority);
        if(tcb==0u || tcb==1u)continue;
        if(!range(tcb,0x40u) || *(const uint8_t *)(uintptr_t)(tcb+0x35u)!=priority)return 0;
        if(tcb==current && priority==9u && gui_inflight)continue;
        uint32_t sp=R32(tcb),pc,ra;
        /* Native tasks use ordinary TaskCreate, so Ext stack-size fields may
         * legitimately be zero. The independently CRC-checked ports establish
         * the 0x7c context frame and EPC/RA offsets for both save mechanisms. */
        if((sp&3u) || !range(sp,0xacu))return 0;
        pc=R32(sp+0x70u);ra=R32(sp);
        if(priority==9u){
            if(!code_range(pc) || !code_range(ra))return 0;
            unsigned task_status=*(const uint8_t *)(uintptr_t)(tcb+0x34u);
            /* Event-ready clears a semaphore pend bit before its task resumes.
             * A generic scheduler continuation is safe only when the two
             * checked caller frames prove the native GUI's idle TaskDelay. */
            if(pc==binding->sched_continuation && ra==pc){
                if(!task_status && R32(sp+0x90u)==binding->delay_continuation && R32(sp+0xa8u)==binding->idle_delay_return)continue;
                /* The real stock GUI normally idles in raw-input OSQPend,
                 * through the platform callback's additional 0x40-byte frame.
                 * Prove every suspended caller, not merely the generic RTOS
                 * continuation or a ready bit which can already be cleared. */
                if((task_status==0u || task_status==4u) && range(sp,0x118u) &&
                   R32(sp+0x90u)==binding->queue_pend_continuation &&
                   R32(sp+0xb4u)==binding->raw_wait_return &&
                   R32(sp+0xf8u)==binding->idle_delay_return &&
                   R32(sp+0x114u)==binding->message_idle_return)continue;
                return 0;
            }
            if(task_status || pc<0x80010000u)return 0; /* Heap/RTOS continuations are not GUI idle. */
        }
        if(critical_pc(pc) || critical_pc(ra))return 0;
    }
    return 1;
}
static int pool_ready(void)
{
    uint32_t pool=binding->pool;
    return range(pool,16u) && R32(pool)==0x24u && R32(pool+4u)==64u &&
        R32(pool+8u)<=64u && range(R32(pool+12u),64u*0x24u);
}
static unsigned pool_available(void)
{
    if(!pool_ready())return 0;unsigned count=0;
    uint32_t base=R32(binding->pool+12u),cursor=R32(binding->pool+8u);
    for(unsigned i=cursor;i<64u;i++)if(!R32(base+i*0x24u))count++;
    return count;
}
static uint32_t pool_take(void)
{
    uint32_t base=R32(binding->pool+12u),cursor=R32(binding->pool+8u);
    for(unsigned i=cursor;i<64u;i++)if(!R32(base+i*0x24u)){
        R32(base+i*0x24u)=2u;R32(binding->pool+8u)=i+1u;return base+i*0x24u+4u;
    }
    return 0;
}
static int pool_owns(uint32_t node,uint32_t base)
{
    return pool_ready() && R32(binding->pool+12u)==base && node>=base+4u &&
        node-base-4u<64u*0x24u && (node-base-4u)%0x24u==0u && R32(node-4u)==2u;
}
static void pool_release(uint32_t node)
{
    uint32_t base=R32(binding->pool+12u),index=(node-4u-base)/0x24u;
    R32(node-4u)=0;uint32_t cursor=R32(binding->pool+8u);if(index<cursor)R32(binding->pool+8u)=index;
}
static unsigned queue_ready(uint32_t target)
{
    if(!valid_window(target))return WRONG_TARGET;
    if(!queue_quiescent() || !pool_ready())return 2u;
    uint32_t owner=R32(target+0xf0u);
    if(!range(owner,0x20u) || (R32(owner)&0x80000000u))return WRONG_TARGET;
    uint32_t head=R32(owner+4u),tail=R32(owner+8u);
    if(head && (!range(head,0x20u) || !range(tail,0x20u) || R32(tail+0x1cu)))return 2u;
    return 0;
}
/* All callers hold the short IRQ lock. These are the checked native pool's
 * non-allocating tag-2 path and linked-queue ABI, with no native calls, heap,
 * semaphore, or explicit scheduler entry. Pool exhaustion is an explicit BUSY. */
static unsigned enqueue_node(uint32_t target,uint32_t message,uint32_t w,uint32_t l,uint32_t node)
{
    uint32_t owner=R32(target+0xf0u),head=R32(owner+4u),tail=R32(owner+8u);
    R32(node)=target;R32(node+4u)=message;R32(node+8u)=w;R32(node+12u)=l;R32(node+0x1cu)=0;
    if(head)R32(tail+0x1cu)=node;else R32(owner+4u)=node;
    R32(owner+8u)=node;R32(owner)|=0x10000000u;return 0;
}
static unsigned post_locked(uint32_t target,uint32_t message,uint32_t w,uint32_t l)
{
    unsigned error=queue_ready(target);if(error)return error;
    uint32_t node=pool_take();if(!node)return 2u;
    return enqueue_node(target,message,w,l,node);
}
static unsigned post(uint32_t target,uint32_t message,uint32_t w,uint32_t l)
{
    uint32_t saved=irq_lock();unsigned result=post_locked(target,message,w,l);irq_unlock(saved);return result;
}
int rd_management_input_pending(void){return held_target!=0;}
static void release_held(void)
{
    uint32_t saved=irq_lock();
    if(!held_target){irq_unlock(saved);return;}
    held_release_pending=1;
    int owns=pool_owns(held_release_node,held_pool_base);
    int same=valid_window(held_target) && R32(held_target+0x88)==held_handler && R32(held_target+0xf0)==held_owner;
    if(owns){
        if(!queue_quiescent()){irq_unlock(saved);return;}
        if(same && queue_ready(held_target)){irq_unlock(saved);return;}
        if(same)enqueue_node(held_target,held_kind==1u?binding->key_up:2u,held_kind==1u?held_code:0u,
                            held_kind==1u?0u:(held_code|(held_arg<<16)),held_release_node);
        else pool_release(held_release_node);
    }
    held_target=held_kind=held_code=held_arg=held_handler=held_owner=held_deadline=held_release_node=held_pool_base=held_release_pending=0;
    irq_unlock(saved);
}
void rd_management_disconnect(void)
{
    release_held();
    /* File and CRC transactions remain queryable across DTR/session changes. */
    uint32_t saved=irq_lock();
    if(job.state==JOB_PENDING && (job.action==51u || job.action==52u)){
        if(valid_window(gui_job.target) && R32(gui_job.target+0x88)==PTR(rd_management_gui_bridge))
            R32(gui_job.target+0x88)=gui_job.original;
        job.state=JOB_CANCELLED;job.result=7;
    }
    irq_unlock(saved);
}
void rd_management_shutdown(void)
{
    rd_management_disconnect();
    if(!(features&RD_MGMT_FILES))return;
    if(file_valid(reader.file))close_file(reader.file);
    if(file_valid(writer.file))close_file(writer.file);
    if(listing.token)fs1(0x44,PTR(&listing.data));
    reader.file=writer.file=0;reader.token=writer.token=listing.token=0;
    if(job_active() && job.action!=51u && job.action!=52u){
        if(file_valid(job.file))close_file(job.file);
        job.file=0;job.state=JOB_CANCELLED;job.result=7;
    }
    (void)fs0(0x74);
}
static unsigned start_job(unsigned action,int f,unsigned total,uint32_t expected,const char *path)
{
    job=(__typeof__(job)){0};job.id=next_token();job.state=JOB_PENDING;job.action=action;job.file=f;job.total=total;
    job.crc=~0u;job.expected_crc=expected;
    if(path)memcpy(job.path,path,length(path,PATH_MAXIMUM)+1);
    return job.id;
}
/* The temporary callback is restored before invoking a native function that
 * can enter an application's event pump. All GUI-owned work runs in the
 * original event-dispatch task, never in the USB worker. */
int rd_management_gui_bridge(uint32_t handle,uint32_t message,uint32_t w,uint32_t l)
{
    uint32_t saved=irq_lock();
    if(message!=GUI_JOB_MESSAGE){
        /* Native DispatchMessage may already have loaded this bridge pointer
         * when cancellation restores the window slot. Snapshot a checked
         * native handler independently of the reusable file/CRC job slot. */
        uint32_t original=valid_window(handle)?R32(handle+0x88):0u;
        if(original==PTR(rd_management_gui_bridge) && handle==gui_job.target)original=gui_job.original;
        if(!binding || (original!=binding->desktop_handler && original!=binding->grid_handler))original=0;
        irq_unlock(saved);
        return original?((int(*)(uint32_t,uint32_t,uint32_t,uint32_t))(uintptr_t)original)(handle,message,w,l):0;
    }
    /* Stale private messages have no application meaning. Never dispatch them
     * into a new job or a frame that has reused the same heap address. */
    if(job.state!=JOB_PENDING || (job.action!=51u && job.action!=52u) ||
       w!=job.id || gui_job.id!=job.id || handle!=gui_job.target){irq_unlock(saved);return 0;}
    if(!valid_window(handle) || R32(handle+0x88)!=PTR(rd_management_gui_bridge) || !correct_target(handle)){
        if(valid_window(handle) && R32(handle+0x88)==PTR(rd_management_gui_bridge))R32(handle+0x88)=gui_job.original;
        job.state=JOB_FAILED;job.result=WRONG_TARGET;irq_unlock(saved);return 0;
    }
    uint32_t id=gui_job.id,action=gui_job.action;
    const struct mgmt_profile *native=binding;
    ++gui_inflight;job.state=JOB_RUNNING;R32(handle+0x88)=gui_job.original;
    irq_unlock(saved);
    if(action==51u){
        ((void(*)(void))(uintptr_t)native->reset_apps)();
        ((void(*)(const char *))(uintptr_t)native->scan_apps)((const char *)(uintptr_t)native->scan_pattern);
        ((void(*)(void))(uintptr_t)native->reset_selection)();
        unsigned queued=post(handle,0xb1u,0,0);
        saved=irq_lock();if(job.id==id && job.state==JOB_RUNNING){job.progress=1;if(!queued){job.state=JOB_DONE;job.result=0;}else if(queued!=2u){job.state=JOB_FAILED;job.result=queued;}}irq_unlock(saved);
    }else{
        /* Dedicated storage outlives any new CRC/file job accepted after the
         * worker has observed the launched foreground frame. */
        gui_job.flag=0;
        (void)((int(*)(const char *,uint32_t,uint32_t *))(uintptr_t)native->launch)(gui_job.path,handle,&gui_job.flag);
        saved=irq_lock();
        if(job.id==id && job.state==JOB_RUNNING){
            uint32_t current=foreground();
            if(current && current!=handle && R32(current+0x88)>=0x81c00020u){job.progress=1;job.state=JOB_DONE;job.result=0;}
            else {job.state=JOB_FAILED;job.result=8;}
        }
        irq_unlock(saved);
    }
    saved=irq_lock();--gui_inflight;irq_unlock(saved);
    return 0;
}
void rd_management_reset(void)
{
    /* A stopped debugger retains its allocation. A launcher may restart the
     * transport while an earlier native loader is still in an app event loop. */
    if(gui_inflight || held_target)return;
    /* Reset is only called after stop/init; release surviving handles first. */
    if(features&RD_MGMT_FILES){if(file_valid(reader.file))close_file(reader.file);if(file_valid(writer.file))close_file(writer.file);if(file_valid(job.file))close_file(job.file);if(listing.token)fs1(0x44,PTR(&listing.data));}
    memset(&gui_job,0,sizeof gui_job);
    binding=0;features=0;memset(&job,0,sizeof job);memset(&writer,0,sizeof writer);memset(&reader,0,sizeof reader);memset(&listing,0,sizeof listing);
    held_target=held_kind=held_code=held_arg=held_handler=held_owner=held_deadline=held_release_node=held_pool_base=held_release_pending=0;
    /* Feature predicates are independent of the resident's core RTOS binding.
     * A discovered core does not silently authorize unknown FS or GUI ABIs. */
    for(unsigned i=0;i<sizeof mgmt_profiles/sizeof mgmt_profiles[0];i++){
        const struct mgmt_profile *p=&mgmt_profiles[i];
        if(p->fs_table!=rd_control.runtime_tables[1] || !guard_matches(p->fs_guards,p->fs_count))continue;
        features=RD_MGMT_FILES|RD_MGMT_CRC;binding=p;
        if(p->gui_table==rd_control.runtime_tables[0] && guard_matches(p->gui_guards,p->gui_count))features|=p->gui_features;
        break;
    }
}
void rd_management_step(void)
{
    if(held_target && (held_release_pending || (held_deadline && (int32_t)(fw_tick()-held_deadline)>=0) || foreground()!=held_target || !valid_window(held_target) || R32(held_target+0x88)!=held_handler || R32(held_target+0xf0)!=held_owner))release_held();
    if(!job_active())return;
    if(job.action==51u || job.action==52u){
        if(job.action==51u && job.state==JOB_RUNNING && job.progress==1u){
            unsigned queued=post(job.target,0xb1u,0,0);
            if(!queued){job.state=JOB_DONE;job.result=0;return;}
            if(queued!=2u){job.state=JOB_FAILED;job.result=queued;return;}
        }
        if(job.action==52u && job.state==JOB_RUNNING && foreground()!=job.target && foreground()){
            uint32_t a=foreground();
            if(R32(a+0x88)>=0x81c00020u && R32(a+0x88)<0x82000000u){job.progress=1;job.state=JOB_DONE;job.result=0;return;}
        }
        if((int32_t)(fw_tick()-job.deadline)>=0){
            if(job.state==JOB_PENDING && valid_window(job.target) && R32(gui_job.target+0x88)==PTR(rd_management_gui_bridge))R32(gui_job.target+0x88)=gui_job.original;
            job.state=JOB_FAILED;job.result=9;
        }
        return;
    }
    job.state=JOB_RUNNING;uint8_t block[4096];unsigned wanted=job.total-job.progress;
    if(wanted>sizeof block)wanted=sizeof block;
    if(wanted){
        int n=fs4(8,PTR(block),1,wanted,(uint32_t)job.file);
        if(n<=0 || (unsigned)n>wanted){close_file(job.file);job.file=0;job.state=JOB_FAILED;job.result=FS_ERROR;return;}
        job.crc=crc_update(job.crc,block,(unsigned)n);job.progress+=(unsigned)n;
    }
    if(job.progress==job.total){
        int rc=close_file(job.file);job.file=0;job.crc=~job.crc;
        job.state=(!rc && (job.action!=22u || job.crc==job.expected_crc))?JOB_DONE:JOB_FAILED;
        job.result=job.state==JOB_DONE?0u:10u;
    }
}
unsigned rd_management_command(unsigned op,const uint8_t *p,unsigned n,uint8_t *out,unsigned *status)
{
    *status=1;
    if(op==40u){if(n)return 0;uint32_t v[]={1,features,CHUNK_MAXIMUM,PATH_MAXIMUM,job.id,job.state,job.action,job.progress,job.total,job.result,held_target,held_code};*status=0;return output_words(out,v,12);}
    if(op==42u){if(n && (n!=4 || get32(p)!=job.id))return 0;uint32_t v[]={1,job.id,job.state,job.action,job.progress,job.total,job.result,job.crc};*status=0;return output_words(out,v,8);}
    if(op==43u){if(n!=4 || get32(p)!=job.id || !job.id)return 0;uint32_t v[]={job.id,job.state,job.result,job.crc,job.progress};*status=0;return output_words(out,v,5);}
    if(op==44u){
        if(n!=4 || get32(p)!=job.id || !job.id)return 0;
        uint32_t saved=irq_lock();
        if(job_active()){
            if(job.action==51u || job.action==52u){
                if(job.state==JOB_RUNNING){irq_unlock(saved);*status=2;return 0;}
                if(valid_window(gui_job.target) && R32(gui_job.target+0x88)==PTR(rd_management_gui_bridge))R32(gui_job.target+0x88)=gui_job.original;
                job.state=JOB_CANCELLED;job.result=7;irq_unlock(saved);
            }else {
                int f=job.file;job.file=0;job.state=JOB_CANCELLED;job.result=7;irq_unlock(saved);
                if(file_valid(f))close_file(f);
            }
        }else irq_unlock(saved);
        release_held();*status=0;return 0;
    }
    if(op!=41u || n<4)return 0;unsigned action=get32(p);p+=4;n-=4;
    if(action==54u){if(n)return 0;release_held();*status=0;return 0;}
    if(action<50u && !(features&RD_MGMT_FILES)){*status=UNSUPPORTED;return 0;}
    char path[PATH_MAXIMUM+1];unsigned used=0;
    switch(action){
    case 1:{
        if(decode_path(path,p,n,&used) || used!=n)return 0;struct path_info s;
        if(stat_path(path,&s)){*status=fs0(0x7c)?NOT_FOUND:FS_ERROR;return 0;}
        uint32_t v[]={s.size,s.attr,s.time[0],s.time[1],s.time[2]};*status=0;return output_words(out,v,5);
    }
    case 2:{
        if(io_busy()){*status=2;return 0;}if(decode_path(path,p,n,&used) || used!=n)return 0;
        unsigned l=length(path,PATH_MAXIMUM);if(l>PATH_MAXIMUM-2)return 0;if(path[l-1]!='\\')path[l++]='\\';path[l++]='*';path[l]=0;
        memset(&listing,0,sizeof listing);int rc=fs3(0x3c,PTR(path),0,PTR(&listing.data));
        listing.token=next_token();listing.first=1;listing.ended=rc!=0;put32(out,listing.token);*status=0;return 4;
    }
    case 3:{
        if(n!=4 || !listing.token || get32(p)!=listing.token)return 0;
        if(!listing.first && !listing.ended)listing.ended=fs1(0x40,PTR(&listing.data))!=0;listing.first=0;
        unsigned l=listing.ended?0:length(listing.data.name,sizeof listing.data.name);if(l>496u){*status=FS_ERROR;return 0;}
        uint32_t v[]={listing.token,listing.data.size,listing.data.attr,l};output_words(out,v,4);memcpy(out+16,listing.data.name,l);*status=0;return 16+l;
    }
    case 4:if(n!=4 || get32(p)!=listing.token || !listing.token)return 0;fs1(0x44,PTR(&listing.data));listing.token=0;*status=0;return 0;
    case 5:case 6:{
        if(io_busy()){*status=2;return 0;}if(decode_path(path,p,n,&used) || used!=n)return 0;
        *status=fs1(action==5u?0x30u:0x24u,PTR(path))?FS_ERROR:0;return 0;
    }
    case 7:{
        if(io_busy()){*status=2;return 0;}char target[PATH_MAXIMUM+1];unsigned extra;
        if(decode_path(path,p,n,&used) || decode_path(target,p+used,n-used,&extra) || used+extra!=n || path[0]!=target[0])return 0;
        struct path_info s;if(stat_path(path,&s)){*status=FS_ERROR;return 0;}if(!stat_path(target,&s)){*status=2;return 0;}
        if(!fs0(0x7c)){*status=FS_ERROR;return 0;}
        *status=fs2(0x28,PTR(path),PTR(target))?FS_ERROR:0;if(!*status)(void)fs0(0x74);return 0;
    }
    case 8:{
        if(n)return 0;unsigned l=writer.token?length(writer.path,PATH_MAXIMUM):0;
        uint32_t v[]={reader.token,reader.size,writer.token,writer.size,writer.offset,listing.token,job.id,l};
        output_words(out,v,8);memcpy(out+32,writer.path,l);*status=0;return 32+l;
    }
    case 10:{
        if(io_busy()){*status=2;return 0;}if(decode_path(path,p,n,&used) || used!=n)return 0;struct path_info s;
        if(stat_path(path,&s) || (s.attr&0x4000u)){*status=FS_ERROR;return 0;}int f=open_file(path,0);if(!file_valid(f)){*status=FS_ERROR;return 0;}
        reader.token=next_token();reader.file=f;reader.size=s.size;put32(out,reader.token);put32(out+4,reader.size);*status=0;return 8;
    }
    case 11:{
        if(n!=12 || !reader.token || get32(p)!=reader.token)return 0;uint32_t offset=get32(p+4),count=get32(p+8);
        if(count>CHUNK_MAXIMUM || offset>reader.size || count>reader.size-offset)return 0;
        if(fs3(0x10,(uint32_t)reader.file,offset,0)!=(int)offset){*status=FS_ERROR;return 0;}
        int got=fs4(8,PTR(out+12),1,count,(uint32_t)reader.file);if(got<0 || (unsigned)got>count){*status=FS_ERROR;return 0;}
        put32(out,reader.token);put32(out+4,offset);put32(out+8,reader.size);*status=0;return 12+(unsigned)got;
    }
    case 12:if(n!=4 || !reader.token || get32(p)!=reader.token)return 0;*status=close_file(reader.file)?FS_ERROR:0;reader.token=0;reader.file=0;return 0;
    case 20:{
        if(io_busy()){*status=2;return 0;}if(n<8 || decode_path(path,p+8,n-8,&used) || used+8!=n)return 0;
        struct path_info s;if(!stat_path(path,&s)){*status=2;return 0;}if(!fs0(0x7c)){*status=FS_ERROR;return 0;}
        int f=open_file(path,1);if(!file_valid(f)){*status=FS_ERROR;return 0;}
        writer.token=next_token();writer.file=f;writer.size=get32(p);writer.expected_crc=get32(p+4);writer.offset=0;memcpy(writer.path,path,length(path,PATH_MAXIMUM)+1);
        put32(out,writer.token);*status=0;return 4;
    }
    case 21:{
        if(n<9 || n>8+CHUNK_MAXIMUM || !writer.token || get32(p)!=writer.token || !file_valid(writer.file))return 0;
        unsigned count=n-8;if(get32(p+4)!=writer.offset || count>writer.size-writer.offset)return 0;
        int wrote=fs4(0xc,PTR(p+8),1,count,(uint32_t)writer.file);
        if(wrote<0 || (unsigned)wrote!=count){*status=FS_ERROR;return 0;}writer.offset+=count;put32(out,writer.offset);*status=0;return 4;
    }
    case 22:{
        if(n!=4 || !writer.token || get32(p)!=writer.token || writer.offset!=writer.size || !file_valid(writer.file))return 0;
        int rc=close_file(writer.file);writer.file=0;(void)fs0(0x74);if(rc){*status=FS_ERROR;return 0;}
        int f=open_file(writer.path,0);if(!file_valid(f)){*status=FS_ERROR;return 0;}
        unsigned id=start_job(action,f,writer.size,writer.expected_crc,writer.path);writer.token=0;put32(out,id);*status=0;return 4;
    }
    case 23:if(n!=4 || !writer.token || get32(p)!=writer.token)return 0;if(file_valid(writer.file))close_file(writer.file);writer.file=0;writer.token=0;*status=0;return 0;
    case 30:{
        if(io_busy()){*status=2;return 0;}if(decode_path(path,p,n,&used) || used!=n)return 0;struct path_info s;
        if(stat_path(path,&s) || (s.attr&0x4000u)){*status=FS_ERROR;return 0;}int f=open_file(path,0);if(!file_valid(f)){*status=FS_ERROR;return 0;}
        put32(out,start_job(action,f,s.size,0,path));*status=0;return 4;
    }
    case 50:{
        if(!(features&RD_MGMT_WINDOWS)){*status=UNSUPPORTED;return 0;}if(n!=4)return 0;unsigned index=get32(p);
        uint32_t a=foreground(),root=a;
        /* Traverse only checked frame links with a bounded cycle guard. */
        for(unsigned i=0;a && i<index && i<128;i++){
            uint32_t child=R32(a+0xe8),next=R32(a+0xec);
            if(valid_window(child))a=child;
            else if(valid_window(next))a=next;
            else {unsigned depth=0;while(a!=root && depth++<32){a=R32(a+0xe4);if(!valid_window(a)){a=0;break;}next=R32(a+0xec);if(valid_window(next)){a=next;break;}}if(a==root)a=0;}
        }
        if(index>=128)a=0;
        uint32_t v[]={index,a,0,0,0,0,0,0,foreground(),0};unsigned title=0;
        if(valid_window(a)){
            v[2]=R32(a+0xe4);v[3]=R32(a+0xe8);v[4]=R32(a+0xec);v[5]=R32(a+0x88);
            v[6]=R32(a+0xf0); /* Native message-queue owner, not a guessed application identity. */
            v[7]=R32(a+0x24);uint32_t s=R32(a+0x8c);if(s>=0x80004000u && s<0x82000000u){
                while(title<240 && s+title<0x82000000u && *(const uint8_t *)(uintptr_t)(s+title))title++;
                memcpy(out+40,(const void *)(uintptr_t)s,title);
            }v[9]=title;
        }
        output_words(out,v,10);*status=0;return 40+title;
    }
    case 51:case 52:{
        unsigned feature=action==51u?RD_MGMT_REFRESH:RD_MGMT_LAUNCH;
        if(!(features&feature)){*status=UNSUPPORTED;return 0;}
        if(io_busy() || gui_inflight){*status=2;return 0;}
        if(n<4 || !correct_target(get32(p)) || !desktop_role(get32(p))){*status=WRONG_TARGET;return 0;}
        if(action==51u){if(n!=4)return 0;}else if(decode_path(path,p+4,n-4,&used) || used+4!=n)return 0;
        uint32_t a=get32(p),saved=irq_lock();
        if(gui_inflight || !correct_target(a) || !desktop_role(a)){irq_unlock(saved);*status=WRONG_TARGET;return 0;}
        unsigned queued=queue_ready(a);if(queued || !pool_available()){irq_unlock(saved);*status=queued?queued:2u;return 0;}
        start_job(action,0,1,0,action==52u?path:0);job.target=a;job.deadline=fw_tick()+160;
        gui_job.id=job.id;gui_job.target=a;gui_job.original=R32(a+0x88);gui_job.action=action;
        if(action==52u)memcpy(gui_job.path,path,length(path,PATH_MAXIMUM)+1);
        R32(a+0x88)=PTR(rd_management_gui_bridge);
        queued=post_locked(a,GUI_JOB_MESSAGE,job.id,0);irq_unlock(saved);
        if(queued){
            saved=irq_lock();if(job.state==JOB_PENDING && R32(a+0x88)==PTR(rd_management_gui_bridge))R32(a+0x88)=gui_job.original;
            if(job.state==JOB_PENDING){job.state=JOB_FAILED;job.result=FS_ERROR;}
            irq_unlock(saved);*status=queued;return 0;
        }
        put32(out,job.id);*status=0;return 4;
    }
    case 55:{
        if(n)return 0;if(!(features&RD_MGMT_WINDOWS)){*status=UNSUPPORTED;return 0;}
        uint32_t a=foreground(),v[]={a,(a && desktop_role(a))?a:0u,(a && desktop_role(a))?R32(a+0x88):binding->desktop_handler};
        *status=0;return output_words(out,v,3);
    }
    case 53:{
        if(!(features&RD_MGMT_INPUT)){*status=UNSUPPORTED;return 0;}if(n!=20 && n!=28)return 0;
        uint32_t target=get32(p),kind=get32(p+4),code=get32(p+8),arg=get32(p+12),ticks=get32(p+16);
        if(kind<1 || kind>7 || ticks>400)return 0;
        if(kind<=4u){if(!code || code>255u || arg)return 0;}
        else if(code>=240u || arg>=320u)return 0;
        uint32_t saved=irq_lock();
        /* Foreground, handler, queue ownership and enqueue are one atomic
         * bounded native operation. A title or recycled handle is insufficient. */
        if(!correct_target(target) || (n==28 && (R32(target+0x88)!=get32(p+20) || R32(target+0xf0)!=get32(p+24)))){
            irq_unlock(saved);*status=WRONG_TARGET;return 0;
        }
        if(job_active()){irq_unlock(saved);*status=2;return 0;}
        if(kind==2u || kind==6u){
            if(!held_target || held_target!=target || (kind==2u && (held_kind!=1u || code!=held_code)) || (kind==6u && held_kind!=2u)){
                irq_unlock(saved);return 0;
            }
            release_held();irq_unlock(saved);*status=0;return 0;
        }
        if(held_target){irq_unlock(saved);*status=2;return 0;}
        unsigned result=queue_ready(target);
        if(result || pool_available()<2u){irq_unlock(saved);*status=result?result:2u;return 0;}
        uint32_t down=pool_take(),up=pool_take();unsigned touch=kind>=5u;
        enqueue_node(target,touch?1u:binding->key_down,touch?0u:code,touch?(code|(arg<<16)):0u,down);
        held_release_node=up;held_pool_base=R32(binding->pool+12u);
        held_target=target;held_kind=touch?2u:1u;held_code=code;held_arg=arg;
        held_handler=R32(target+0x88);held_owner=R32(target+0xf0);
        held_deadline=fw_tick()+((kind==1u || kind==5u)?400u:(ticks?ticks:1u));
        irq_unlock(saved);*status=0;return 0;
    }
    default:return 0;
    }
}
