/* SPDX-License-Identifier: GPL-3.0-only
 * Conservative MIPS address discovery. Included once by firmware.c.
 * A live loader table anchors a graph of independently hashed native functions.
 * Only RAM address operands relocate. Branches and all MMIO instructions are
 * still exact. Nothing discovered here is called until the complete graph and
 * binding relations pass. This is not a disassembler for arbitrary kernels.
 */
#include "discovery_generated.h"
uint32_t rd_binding_values[RD_DISC_BIND_COUNT]=RD_DISC_INITIAL_VALUES;
static uint32_t discovery_mode,discovery_index,discovery_error,discovery_scanned;
static uint32_t discovery_matched,discovery_refs;
struct rd_disc_map { uint32_t old_address,new_address; };
static struct rd_disc_map discovery_map[1536];
static unsigned discovery_map_count;
static uint32_t discovery_found[256];
static int disc_ram(uint32_t a){return a>=0x80004000u && a<0x81000000u;}
static int disc_range(uint32_t a,uint32_t n)
{return !(a&3u) && disc_ram(a) && n<=0x81000000u-a;}
static uint32_t disc_lookup(uint32_t a)
{
    for(unsigned i=0;i<discovery_map_count;i++)if(discovery_map[i].old_address==a)return discovery_map[i].new_address;
    return 0;
}
static int disc_map(uint32_t a,uint32_t b)
{
    if(!disc_ram(a))return a==b;
    if(!disc_ram(b))return 0;
    for(unsigned i=0;i<discovery_map_count;i++){
        if(discovery_map[i].old_address==a)return discovery_map[i].new_address==b;
        if(discovery_map[i].new_address==b)return 0;
    }
    if(discovery_map_count>=sizeof discovery_map/sizeof discovery_map[0])return 0;
    discovery_map[discovery_map_count++]=(struct rd_disc_map){a,b};return 1;
}
static uint32_t disc_crc_word(uint32_t c,uint32_t value)
{
    for(unsigned byte=0;byte<4;byte++){
        c^=value&255u;value>>=8;
        for(unsigned bit=0;bit<8;bit++)c=(c>>1)^(0xedb88320u&(0u-(c&1u)));
    }
    return c;
}
static uint32_t disc_reference(uint32_t at,const struct rd_disc_ref *r)
{
    uint32_t high=R32(at+r->high);
    if(r->kind==2u)return ((at+r->high+4u)&0xf0000000u)|((high&0x03ffffffu)<<2);
    uint32_t low=R32(at+r->low)&65535u;
    return r->kind==1u ? ((high&65535u)<<16)|low : ((high&65535u)<<16)+(int32_t)(int16_t)low;
}
static int disc_pattern(uint32_t at,const struct rd_disc_pattern *p,int add)
{
    if(!disc_range(at,p->length))return 0;
    /* A target inside a known body is the same instruction offset after that
     * body moves. It cannot independently relocate to an arbitrary entry. */
    for(unsigned i=0;i<discovery_map_count;i++){
        uint32_t old=discovery_map[i].old_address;
        if(old>=p->address && old-p->address<p->length && discovery_map[i].new_address!=at+(old-p->address))return 0;
    }
    uint32_t c=~0u;
    for(unsigned offset=0;offset<p->length;offset+=4){
        uint32_t value=R32(at+offset),mask=~0u;
        for(unsigned i=0;i<p->count;i++){
            const struct rd_disc_ref *r=&p->refs[i];
            if(r->high==offset)mask&=r->kind==2u ? 0xfc000000u : 0xffff0000u;
            if(r->kind!=2u && r->low==offset)mask&=0xffff0000u;
        }
        c=disc_crc_word(c,value&mask);
    }
    if(~c!=p->crc)return 0;
    uint32_t mapped=disc_lookup(p->address);
    if(mapped && mapped!=at)return 0;
    for(unsigned i=0;i<p->count;i++){
        uint32_t target=disc_reference(at,&p->refs[i]);
        uint32_t prior=disc_lookup(p->refs[i].target);
        if(!disc_ram(target) || (prior && prior!=target))return 0;
        uint32_t old=p->refs[i].target;
        if(old>=p->address && old-p->address<p->length && target!=at+(old-p->address))return 0;
    }
    if(add){
        if(!disc_map(p->address,at))return 0;
        for(unsigned i=0;i<p->count;i++)if(!disc_map(p->refs[i].target,disc_reference(at,&p->refs[i])))return 0;
    }
    return 1;
}
static int disc_tables(const struct rd_disc_profile *p)
{
    for(unsigned i=0;i<5;i++){
        const struct rd_disc_table *t=&p->tables[i];uint32_t live=R32(0x81c00004u+4u*i);
        if(!disc_range(live,t->count*4u) || !disc_map(t->address,live))return 0;
        for(unsigned j=0;j<t->count;j++){
            uint32_t old=t->values[j],value=R32(live+4u*j);
            if(disc_ram(old)){if((value&3u) || !disc_map(old,value))return 0;}
            else if(old!=value)return 0;
        }
    }
    return 1;
}
static int disc_profile(unsigned index)
{
    const struct rd_disc_profile *p=&rd_disc_profiles[index];
    discovery_map_count=0;discovery_matched=0;
    if(p->count>sizeof discovery_found/sizeof discovery_found[0] || !disc_tables(p)){discovery_error=1;return 0;}
    for(unsigned i=0;i<p->count;i++)discovery_found[i]=0;
    /* Resolve already anchored functions and their callees before scanning.
     * An independently checked call target disambiguates identical bodies. */
    for(unsigned round=0;round<p->count;round++){
        unsigned progress=0;
        for(unsigned i=0;i<p->count;i++)if(!discovery_found[i]){
            uint32_t mapped=disc_lookup(p->patterns[i].address);
            if(mapped){
                if(!disc_pattern(mapped,&p->patterns[i],1)){discovery_error=0x100u+i;return 0;}
                discovery_found[i]=mapped;progress++;discovery_matched++;
            }
        }
        if(!progress)break;
    }
    /* One word pass searches every unanchored root through a small hash table.
     * Two matches remain ambiguous until another verified reference resolves
     * them. No nearest-address or first-match fallback is permitted. */
    uint32_t candidates[256],counts[256];int heads[64],next[256];
    for(unsigned i=0;i<256;i++){((volatile uint32_t *)candidates)[i]=0;((volatile uint32_t *)counts)[i]=0;}
    for(unsigned i=0;i<64;i++)heads[i]=-1;
    for(unsigned i=0;i<p->count;i++)if(!discovery_found[i]){
        const struct rd_disc_pattern *pattern=&p->patterns[i];
        if(pattern->anchor_mask!=~0u){discovery_error=4;return 0;}
        unsigned bucket=(pattern->anchor_value^(pattern->anchor_value>>16))&63u;
        next[i]=heads[bucket];heads[bucket]=(int)i;
    }
    if(discovery_matched!=p->count)for(uint32_t anchor=p->start;anchor<p->end;anchor+=4){
        uint32_t value=R32(anchor);discovery_scanned++;
        for(int index=heads[(value^(value>>16))&63u];index>=0;index=next[index]){
            const struct rd_disc_pattern *pattern=&p->patterns[index];
            if(value!=pattern->anchor_value || anchor<p->start+pattern->anchor_offset)continue;
            uint32_t address=anchor-pattern->anchor_offset;
            if(address>p->end-pattern->length || !disc_pattern(address,pattern,0))continue;
            candidates[index]=address;counts[index]++;
        }
    }
    for(unsigned round=0;round<p->count;round++){
        unsigned progress=0;
        for(unsigned i=0;i<p->count;i++)if(!discovery_found[i]){
            uint32_t at=disc_lookup(p->patterns[i].address);
            if(!at && counts[i]==1u)at=candidates[i];
            if(!at)continue;
            if(!disc_pattern(at,&p->patterns[i],1)){discovery_error=0x200u+i;return 0;}
            discovery_found[i]=at;discovery_matched++;progress++;
        }
        if(!progress)break;
    }
    if(discovery_matched!=p->count){discovery_error=0x300u;return 0;}
    for(unsigned i=0;i<RD_DISC_BIND_COUNT;i++){
        const struct rd_disc_binding *b=&p->bindings[i];
        if(!b->reference){if(p->values[i]){discovery_error=2;return 0;}continue;}
        uint32_t value=disc_lookup(b->reference);
        if(!value || !disc_ram(value+b->addend)){discovery_error=3;return 0;}
    }
    discovery_refs=discovery_map_count;discovery_error=0;return 1;
}
static void disc_commit(unsigned index,unsigned mode)
{
    const struct rd_disc_profile *p=&rd_disc_profiles[index];
    for(unsigned i=0;i<RD_DISC_BIND_COUNT;i++){
        const struct rd_disc_binding *b=&p->bindings[i];
        rd_binding_values[i]=mode==2u && b->reference ? disc_lookup(b->reference)+b->addend : p->values[i];
    }
    discovery_index=index;discovery_mode=mode;
}
static int disc_live_ok(void)
{
    /* The graph proves code identities. These checks additionally prove that
     * its private data still describes the live native scheduler/IRQ layout. */
    if(R8(FW_OS_RUNNING)!=1u || R8(FW_SCHED_LOCK) || R8(FW_INT_NESTING))return 0;
    uint32_t current=R32(FW_CURRENT_TCB);
    if(!disc_range(current,0x70u))return 0;
    unsigned priority=R8(current+0x35u);
    if(priority>=64u || !disc_range(FW_TASK_TABLE,256u) || R32(FW_TASK_TABLE+4u*priority)!=current)return 0;
    uint32_t bits=R32(FW_LCD_BITS);
    if(R32(FW_LCD_WIDTH)!=FW_SCREEN_WIDTH || R32(FW_LCD_HEIGHT)!=FW_SCREEN_HEIGHT ||
       (bits!=16u && bits!=18u && bits!=32u))return 0;
    if(R32(FW_USB_IRQ_SLOT)!=FW_NATIVE_USB_IRQ || R32(FW_USB_IRQ_SLOT+4u))return 0;
    if(R32(FW_TICK_IRQ_SLOT+4u))return 0;
    if(R32(FW_TICK_IRQ_SLOT)!=FW_TICK_IRQ_HANDLER){
        rd_header *h=fw_find_resident();uint32_t handler=R32(FW_TICK_IRQ_SLOT);
        if(!h || (h->state!=RD_RUNNING && h->state!=RD_STOPPING) ||
           handler<h->base || handler-h->base>=h->size || h->binding_id!=fw_binding_id())return 0;
    }
    return 1;
}
static int disc_product_ok(void)
{
    unsigned wanted=0,foreign=0;uint32_t start=0x80004000u,end=0x80800000u;
    for(uint32_t word=start;word<end;word+=4u){
        uint32_t value=R32(word);discovery_scanned++;
        uint32_t different=value^0x39393939u;
        if(!((different-0x01010101u)&~different&0x80808080u))continue;
        for(unsigned shift=0;shift<4;shift++,value>>=8)if((value&255u)=='9'){
            uint32_t a=word+shift;if(a>end-7u)continue;
            if(R8(a+2u)!='8' || R8(a+3u)!='8' || R8(a+4u)!=' ' || R8(a+5u)!='O' || R8(a+6u)!='S')continue;
            unsigned second=R8(a+1u);
            if(second!=(unsigned)'5' && second!=(unsigned)'6')continue;
            unsigned model=second==(unsigned)'5' ? 9588u : 9688u;
            if(model==RD_DISC_TARGET_MODEL)wanted++;else foreign++;
        }
    }
    return wanted==1u && !foreign;
}
static int disc_reject(unsigned error)
{
    discovery_mode=0;discovery_index=0;discovery_error=error;
    for(unsigned i=0;i<RD_DISC_BIND_COUNT;i++)rd_binding_values[i]=rd_disc_profiles[0].values[i];
#ifdef RD_UNIVERSAL_LAUNCHER
    rd_selected_profile=0;
#endif
    return 0;
}
static int disc_discover(void)
{
    /* Save one accepted binding set because proving uniqueness tries the other
     * chip templates and intentionally overwrites temporary graph state. */
    uint32_t accepted[RD_DISC_BIND_COUNT];unsigned winner=~0u,matched=0,refs=0;
    discovery_mode=0;discovery_scanned=0;
    for(unsigned i=0;i<RD_DISC_PROFILE_COUNT;i++)if(disc_profile(i)){
        if(winner!=~0u)return disc_reject(0x500u);
        winner=i;matched=discovery_matched;refs=discovery_refs;disc_commit(i,2);
        for(unsigned j=0;j<RD_DISC_BIND_COUNT;j++)accepted[j]=rd_binding_values[j];
        discovery_mode=0;
    }
    if(winner==~0u)return disc_reject(discovery_error);
    for(unsigned i=0;i<RD_DISC_BIND_COUNT;i++)rd_binding_values[i]=accepted[i];
    discovery_index=winner;discovery_mode=2;discovery_error=0;discovery_matched=matched;discovery_refs=refs;
#ifdef RD_UNIVERSAL_LAUNCHER
    rd_selected_profile=&rd_launcher_profiles[winner];
#endif
    if(!disc_live_ok())return disc_reject(0x600u);
    if(!disc_product_ok())return disc_reject(0x700u);
    return 1;
}
uint32_t fw_binding_id(void)
{
    uint32_t c=disc_crc_word(~0u,rd_disc_profiles[discovery_index].soc);
    for(unsigned i=0;i<RD_DISC_BIND_COUNT;i++)c=disc_crc_word(c,rd_binding_values[i]);
    return ~c ? ~c : 1u;
}
unsigned fw_discovery_info(uint32_t *out,unsigned capacity)
{
    unsigned count=9u+2u*RD_DISC_BIND_COUNT;if(capacity<count)return 0;
    out[0]=1;out[1]=discovery_mode;out[2]=rd_disc_profiles[discovery_index].soc;
    out[3]=fw_binding_id();out[4]=discovery_error;out[5]=RD_DISC_BIND_COUNT;
    out[6]=discovery_scanned;out[7]=discovery_matched;out[8]=discovery_refs;
    for(unsigned i=0;i<RD_DISC_BIND_COUNT;i++){out[9u+2u*i]=i;out[10u+2u*i]=rd_binding_values[i];}
    return count;
}
