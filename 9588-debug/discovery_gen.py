#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Derive relocation-tolerant code fingerprints from an explicit local ROM.

The checked database contains hashes, masked anchors and reference coordinates.
No original ROM is needed to build. Instruction layout, control flow and board
register constants must still match; only verified address operands may move.
"""
from __future__ import annotations
import argparse, collections, hashlib, json, struct, zlib
from pathlib import Path

def number(v): return int(v, 0) if isinstance(v, str) else int(v)
def ram(v): return 0x80004000 <= v < 0x81000000

def references(data, address):
    w = list(struct.unpack('<'+'I'*(len(data)//4), data[:len(data)//4*4]))
    registers = {}; refs = []
    for i, v in enumerate(w):
        op=v>>26; rs=(v>>21)&31; rt=(v>>16)&31; rd=(v>>11)&31; imm=v&65535
        signed=imm if imm<32768 else imm-65536
        if op in (2,3):
            target=((address+4*i+4)&0xf0000000)|((v&0x3ffffff)<<2)
            if ram(target): refs.append([4*i,0xffffffff,target,2])
            continue
        if op==15:
            registers[rt]=(i,imm<<16); continue
        if op in (9,13) or 32<=op<=63:
            prior=registers.get(rs)
            if prior:
                high,base=prior; target=(base|imm) if op==13 else (base+signed)&0xffffffff
                if ram(target): refs.append([4*high,4*i,target,1 if op==13 else 0])
            if op in (9,13) or 32<=op<40 or 48<=op<56: registers.pop(rt,None)
            continue
        if op==0:
            fn=v&63
            if fn in (33,37) and (rs==0 or rt==0):
                src=rt if rs==0 else rs
                if src in registers: registers[rd]=registers[src]
                else: registers.pop(rd,None)
            elif fn not in (8,9,12,13,16,18,24,25,26,27): registers.pop(rd,None)
        elif op not in (1,4,5,6,7,16,17,18): registers.pop(rt,None)
    return refs

def normalized(data, refs):
    out=bytearray(data); masks={}
    for high,low,target,kind in refs:
        masks[high]=0xfc000000 if kind==2 else 0xffff0000
        if kind!=2: masks[low]=0xffff0000
    for offset,mask in masks.items():
        struct.pack_into('<I',out,offset,struct.unpack_from('<I',out,offset)[0]&mask)
    return bytes(out),masks

DATA={'bda_seed','lcd_geometry','gui_exports','fs_exports','sys_exports','mem_exports','res_exports','embedded_version','gui_version_text','sdram_geometry_template'}
TABLES=['FW_GUI_TABLE','FW_FS_TABLE','FW_SYS_TABLE','FW_MEM_TABLE','FW_RES_TABLE']
EXCLUDE={'FW_BDA_SEED','FW_SDRAM_START','FW_SDRAM_BASE','FW_RUNTIME_BASE'}

def generate(profile, raw):
    base=number(profile['load_base']); macros={n:number(v) for n,v in profile['macros'].items()}
    guards=[g for g in profile['guards'] if g['name'] not in DATA]+[g for g in profile.get('notice_guards',[]) if g['name']!='gui_msgbox_slot']
    histogram=collections.Counter(struct.unpack('<'+'I'*(len(raw)//4),raw[:len(raw)//4*4]))
    patterns=[]; allrefs=set()
    primary_count=len(guards)
    def append_pattern(g):
        address=number(g['address']); length=number(g['length']); data=raw[address-base:address-base+length]
        if length%4: raise ValueError('unaligned instruction signature: '+g['name'])
        refs=references(data,address); norm,masks=normalized(data,refs)
        candidates=[o for o in range(0,min(length,64),4) if o not in masks and struct.unpack_from('<I',data,o)[0]!=0]
        if not candidates: candidates=[0]
        off=min(candidates,key=lambda o:histogram[struct.unpack_from('<I',data,o)[0]])
        mask=masks.get(off,0xffffffff); value=struct.unpack_from('<I',data,off)[0]&mask
        patterns.append({'name':g['name'],'address':hex(address),'length':length,'anchor_offset':off,'anchor_mask':hex(mask),'anchor_value':hex(value),'crc32':hex(zlib.crc32(norm)),'refs':[[a,b,hex(t),k] for a,b,t,k in refs]})
        allrefs.add(address); allrefs.update(t for _,_,t,_ in refs)
    for g in guards:append_pattern(g)
    primary=list(patterns)
    # Authenticate every direct J/JAL destination not already covered by a
    # primary body. This bounds the trust expansion to direct helper entries,
    # rather than guessing a recursive disassembly of the complete kernel.
    callees={number(target) for pat in primary for high,low,target,kind in pat['refs'] if kind==2}
    for target in sorted(callees):
        if any(number(p['address'])<=target<number(p['address'])+p['length'] for p in primary):continue
        offset=target-base
        if offset<0 or offset+64>len(raw):raise ValueError('direct helper entry escapes checked ROM')
        length=64
        for pos in range(0,60,4):
            instruction=struct.unpack_from('<I',raw,offset+pos)[0]
            # Stop at a real JR return (including its delay slot). This avoids
            # hashing the next unrelated routine after a short helper.
            if instruction==0x03e00008:
                length=pos+8;break
        append_pattern({'name':'direct_helper_entry_'+hex(target),'address':hex(target),'length':length})
        patterns[-1]['scope']='entry-prefix'
    tables=[]
    for name in TABLES:
        if name not in macros:
            seed=macros['FW_BDA_SEED']-base
            macros[name]=struct.unpack_from('<I',raw,seed+4+4*TABLES.index(name))[0]
        guard=next((g for g in profile['guards'] if number(g['address'])==macros[name]),None)
        size=number(guard['length']) if guard else {'FW_GUI_TABLE':0x820,'FW_FS_TABLE':0x90,'FW_SYS_TABLE':0xd0,'FW_MEM_TABLE':0x34,'FW_RES_TABLE':0xa0}[name]
        values=list(struct.unpack('<'+'I'*(size//4),raw[macros[name]-base:macros[name]-base+size]))
        tables.append({'macro':name,'address':hex(macros[name]),'values':[hex(x) for x in values]})
        allrefs.add(macros[name]); allrefs.update(x for x in values if ram(x))
    bindings={}
    for name,value in macros.items():
        if not ram(value) or name in EXCLUDE: continue
        if value in allrefs: bindings[name]=[hex(value),0]; continue
        if name.endswith('_IRQ_SLOT'):
            candidates=[x for x in allrefs if 0<=value-x<=0x400]
        elif name in ('FW_LCD_HEIGHT','FW_LCD_BITS'):
            candidates=[macros['FW_LCD_WIDTH']]
        else: candidates=[]
        if not candidates: raise ValueError('no decoded address reference for '+name+' '+hex(value))
        near=max(candidates); bindings[name]=[hex(near),value-near]
    if 'FW_NATIVE_USB_ENDPOINTS' in macros and not macros['FW_NATIVE_USB_ENDPOINTS']:
        bindings['FW_NATIVE_USB_ENDPOINTS']=['0x0',0]
    return {'version':1,'scan_start':'0x80004000','scan_end':'0x80800000','primary_count':primary_count,'patterns':patterns,'tables':tables,'bindings':bindings}

def dynamic_names(profiles):
    return sorted({name for p in profiles for name in p.get('discovery',{}).get('bindings',{})})

def binding_header(profiles):
    names=dynamic_names(profiles)
    lines=['/* Generated checked address-binding layout. */', '#ifndef RD_DISCOVERY_BINDINGS_H','#define RD_DISCOVERY_BINDINGS_H',
           f'#define RD_DISC_BIND_COUNT {len(names)}u', 'extern uint32_t rd_binding_values[RD_DISC_BIND_COUNT];']
    for index,name in enumerate(names):
        lines += [f'#undef {name}', f'#define {name} (rd_binding_values[{index}])']
    return '\n'.join(lines+['#endif',''])

def validate_database(profile):
    d=profile.get('discovery',{});macros={n:number(v) for n,v in profile['macros'].items()}
    if d.get('version')!=1:raise ValueError('checked discovery database v1 is required')
    start,end=number(d['scan_start']),number(d['scan_end'])
    if start&3 or end&3 or start<0x80004000 or not start<end<=0x80800000:raise ValueError('discovery scan window must stay in the bounded kernel RAM region')
    pats=d.get('patterns',[])
    if not 1<=len(pats)<=256:raise ValueError('discovery needs 1..256 checked code patterns')
    for p in pats:
        at,size,off=number(p['address']),number(p['length']),number(p['anchor_offset'])
        if not ram(at) or at&3 or not 0<size<=0x10000 or size&3 or off&3 or off>=size:raise ValueError('invalid discovery instruction signature range')
        if number(p['anchor_mask'])!=0xffffffff:raise ValueError('discovery root anchor must be one exact non-address instruction')
        for key in ('crc32','anchor_value'):
            if not 0<=number(p[key])<=0xffffffff:raise ValueError('invalid discovery CRC or anchor')
        for high,low,target,kind in p['refs']:
            if kind not in (0,1,2) or high&3 or not 0<=high<size or not ram(number(target)):raise ValueError('invalid discovery reference')
            if kind!=2 and (low&3 or not 0<=low<size):raise ValueError('discovery low-half reference escapes checked function')
            if kind==2 and low!=0xffffffff:raise ValueError('jump signature has no low-half instruction')
    tables=d.get('tables',[])
    if len(tables)!=5 or [t['macro'] for t in tables]!=TABLES:raise ValueError('discovery must anchor all five ordered loader tables')
    for t in tables:
        if number(t['address'])!=macros[t['macro']] or not 1<=len(t['values'])<=1024:raise ValueError('invalid discovery loader table')
        for value in t['values']:
            if not 0<=number(value)<=0xffffffff:raise ValueError('invalid discovery loader table value')
    expected={name for name,value in macros.items() if ram(value) and name not in EXCLUDE}
    if macros.get('FW_NATIVE_USB_ENDPOINTS')==0:expected.add('FW_NATIVE_USB_ENDPOINTS')
    if set(d['bindings'])!=expected:raise ValueError('discovery must bind every private RAM macro exactly once')
    for name,(reference,addend) in d['bindings'].items():
        reference,addend=number(reference),number(addend)
        if not reference and not macros[name] and not addend:continue
        if not ram(reference) or not 0<=addend<=0x400 or reference+addend!=macros[name]:raise ValueError('discovery binding does not reconstruct its checked original address: '+name)
    irq=23 if profile['soc']=='JZ4730' else 22
    if d['bindings']['FW_TICK_IRQ_SLOT'][1]!=8*irq:raise ValueError('tick slot is not indexed from the independently decoded native IRQ table base')
    usb_irq=macros['FW_USB_IRQ_MASK'].bit_length()-1
    if d['bindings']['FW_USB_IRQ_SLOT'][1]!=8*usb_irq:raise ValueError('USB slot is not indexed from the independently decoded native IRQ table base')

def c_header(profiles):
    names=dynamic_names(profiles)
    def u(v): return f'0x{number(v):08x}u'
    lines=['/* Generated relocation-tolerant fingerprint database. No ROM required. */', '#ifndef RD_DISCOVERY_GENERATED_H','#define RD_DISCOVERY_GENERATED_H', f'#define RD_DISC_TARGET_MODEL {int(profiles[0].get("target",profiles[0]["id"][:4]))}u',
           'struct rd_disc_ref { uint32_t high,low,target,kind; };',
           'struct rd_disc_pattern { uint32_t address,length,anchor_offset,anchor_mask,anchor_value,crc,count; const struct rd_disc_ref *refs; };',
           'struct rd_disc_table { uint32_t address,count; const uint32_t *values; };',
           'struct rd_disc_binding { uint32_t reference,addend; };',
           'struct rd_disc_profile { uint32_t soc,start,end,count; const struct rd_disc_pattern *patterns; const struct rd_disc_table *tables; const struct rd_disc_binding *bindings; const uint32_t *values; };']
    for i,p in enumerate(profiles):
        validate_database(p)
        d=p.get('discovery')
        if not d or d['version']!=1: raise ValueError('checked discovery database v1 is required')
        for j,pat in enumerate(d['patterns']):
            lines += [f'static const struct rd_disc_ref rd_drefs_{i}_{j}[] = {{']
            lines += ['{'+','.join(u(x) for x in ref)+'},' for ref in pat['refs']]
            lines += ['};']
        lines += [f'static const struct rd_disc_pattern rd_dpats_{i}[] = {{']
        for j,pat in enumerate(d['patterns']):
            fields=[pat[n] for n in ('address','length','anchor_offset','anchor_mask','anchor_value','crc32')]+[len(pat['refs'])]
            lines += ['{'+','.join(u(x) for x in fields)+f',rd_drefs_{i}_{j}'+'},']
        lines += ['};']
        for j,t in enumerate(d['tables']):
            lines += [f'static const uint32_t rd_dtvals_{i}_{j}[] = {{'+','.join(u(x) for x in t['values'])+'};']
        lines += [f'static const struct rd_disc_table rd_dtabs_{i}[] = {{']
        lines += [f'{{{u(t["address"])},{len(t["values"])}u,rd_dtvals_{i}_{j}}},' for j,t in enumerate(d['tables'])]
        lines += ['};',f'static const struct rd_disc_binding rd_dbinds_{i}[] = {{']
        for name in names:
            reference,addend=d['bindings'].get(name,[0,0]);lines += [f'{{{u(reference)},{u(addend)}}},']
        lines += ['};', f'static const uint32_t rd_dvalues_{i}[] = {{'+','.join(u(p['macros'].get(n,0)) for n in names)+'};']
    lines += [f'#define RD_DISC_PROFILE_COUNT {len(profiles)}u', 'static const struct rd_disc_profile rd_disc_profiles[] = {']
    for i,p in enumerate(profiles):
        d=p['discovery'];lines += [f'{{{int(p["soc"][2:])}u,{u(d["scan_start"])},{u(d["scan_end"])},{len(d["patterns"])}u,rd_dpats_{i},rd_dtabs_{i},rd_dbinds_{i},rd_dvalues_{i}'+'},']
    lines += ['};', '#define RD_DISC_INITIAL_VALUES {'+','.join(u(profiles[0]['macros'].get(n,0)) for n in names)+'}',
              '#endif','']
    return '\n'.join(lines)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('profile',type=Path);p.add_argument('firmware',type=Path);p.add_argument('--write',action='store_true'); a=p.parse_args()
    profile=json.loads(a.profile.read_text(encoding='utf-8'));raw=a.firmware.read_bytes();expected=profile['firmware']['sha256']
    if hashlib.sha256(raw).hexdigest()!=expected:
        if hashlib.sha256(raw[64:]).hexdigest()!=expected: raise ValueError('local ROM does not match checked profile')
        raw=raw[64:]
    discovery=generate(profile,raw)
    if a.write:
        profile['discovery']=discovery;a.profile.write_text(json.dumps(profile,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    else: print(json.dumps(discovery,indent=2))
if __name__=='__main__':main()
