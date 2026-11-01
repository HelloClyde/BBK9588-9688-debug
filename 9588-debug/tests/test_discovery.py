# SPDX-License-Identifier: GPL-3.0-only
"""Execute address discovery in MIPS, with moved synthetic code and globals.

The synthetic kernels are defined here and have no vendor instruction bytes.
Optional tests use an explicitly supplied original local ROM; those tests only
execute debugger code, never pretend to boot a relocated kernel or USB host.
"""
from __future__ import annotations
import copy, hashlib, importlib.util, json, os, struct, tempfile, time, unittest, zlib
from pathlib import Path
from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE
from unicorn.mips_const import UC_MIPS_REG_CP0_STATUS, UC_MIPS_REG_RA, UC_MIPS_REG_SP, UC_MIPS_REG_PC, UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_V0

SOURCE=Path(__file__).resolve().parents[1]
TARGET=SOURCE.name[:4]
spec=importlib.util.spec_from_file_location('discovery_builder_'+TARGET,SOURCE/'build.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
gen=builder.DISCOVERY_GENERATOR
number=gen['number']

def packed(values):return struct.pack('<'+'I'*len(values),*values)

def synthetic_profile(profile):
    profile=copy.deepcopy(profile);old=profile['discovery'];refs=sorted({number(b[0]) for b in old['bindings'].values()})
    instructions=[]
    for address in refs:
        instructions += [0x3c080000|(((address+0x8000)>>16)&65535),0x25080000|(address&65535)]
    callee=0x801d0000
    instructions += [0x0c000000|((callee>>2)&0x3ffffff),0,0x03e00008,0]
    root=0x80040000;board=0x801f0000
    code=packed(instructions); root_refs=gen['references'](code,root);norm,masks=gen['normalized'](code,root_refs)
    board_code=packed([0x3c0ab304,0x354a0054,0x03e00008,0])
    callee_code=packed([0x27bdfff0,0xafbf000c,0x8fbf000c,0x03e00008,0x27bd0010])
    patterns=[{'name':'synthetic-loader-anchor','address':hex(root),'length':len(code),'anchor_offset':len(code)-8,'anchor_mask':'0xffffffff','anchor_value':'0x03e00008','crc32':hex(zlib.crc32(norm)),'refs':[[a,b,hex(t),k] for a,b,t,k in root_refs]},
              {'name':'synthetic-fixed-board','address':hex(board),'length':len(board_code),'anchor_offset':0,'anchor_mask':'0xffffffff','anchor_value':'0x3c0ab304','crc32':hex(zlib.crc32(board_code)),'refs':[]},
              {'name':'synthetic-direct-helper-entry','address':hex(callee),'length':len(callee_code),'anchor_offset':0,'anchor_mask':'0xffffffff','anchor_value':'0x27bdfff0','crc32':hex(zlib.crc32(callee_code)),'refs':[]}]
    profile['discovery']={**old,'patterns':patterns,'tables':[{**t,'values':[hex(root)]} for t in old['tables']]}
    return profile,{root:code,board:board_code,callee:callee_code,0x802f0000:(TARGET+' OS').encode('ascii')}

class Model:
    sentinel=0x819ff000
    def __init__(self,raw,symbols,profile,segments,*,mover=None):
        self.profile,self.symbols,self.segments=profile,symbols,segments
        self.u=Uc(UC_ARCH_MIPS,UC_MODE_MIPS32|UC_MODE_LITTLE_ENDIAN);self.u.mem_map(0,0x2000000)
        self.put(0x81a00000,raw);self.mover=mover or (lambda a:a)
        self.native_executions=[]
        self.u.hook_add(UC_HOOK_CODE,self.observe,begin=0x80000000,end=0x819fffff)
        self.u.hook_add(UC_HOOK_CODE,self.observe,begin=0x81a20000,end=0x81ffffff)
        for address,data in segments.items():self.put(self.mover(address),self.relocate(address,data))
        d=profile['discovery']
        for i,table in enumerate(d['tables']):
            self.set_word(0x81c00004+4*i,self.mover(number(table['address'])))
            self.put(self.mover(number(table['address'])),packed([self.mover(number(v)) if gen['ram'](number(v)) else number(v) for v in table['values']]))
        self.macros={n:number(v) for n,v in profile['macros'].items()}
        self.live_setup()
    def observe(self,u,address,size,_):
        if not 0x81a00000<=address<0x81a20000:
            self.native_executions.append(address)
            raise AssertionError(f'discovery invoked an unvalidated native target: {address:#x}')
    def put(self,a,data):self.u.mem_write(a&0x1fffffff,data)
    def get(self,a,n):return bytes(self.u.mem_read(a&0x1fffffff,n))
    def word(self,a):return struct.unpack('<I',self.get(a,4))[0]
    def set_word(self,a,v):self.put(a,struct.pack('<I',v))
    def relocate(self,address,data):
        out=bytearray(data)
        for pat in self.profile['discovery']['patterns']:
            if not address<=number(pat['address'])<address+len(data):continue
            start=number(pat['address'])-address
            for high,low,target,kind in pat['refs']:
                moved=self.mover(number(target));hiword=struct.unpack_from('<I',out,start+high)[0]
                if kind==2:struct.pack_into('<I',out,start+high,(hiword&0xfc000000)|((moved>>2)&0x3ffffff))
                else:
                    highpart=(moved>>16) if kind==1 else ((moved+0x8000)>>16)
                    struct.pack_into('<I',out,start+high,(hiword&0xffff0000)|(highpart&65535))
                    lowword=struct.unpack_from('<I',out,start+low)[0]
                    struct.pack_into('<I',out,start+low,(lowword&0xffff0000)|(moved&65535))
        return bytes(out)
    def live_setup(self):
        m=self.macros;move=self.mover
        for field,value in [('FW_LCD_WIDTH',240),('FW_LCD_HEIGHT',320),('FW_LCD_BITS',16)]:self.set_word(move(m[field]),value)
        self.put(move(m['FW_OS_RUNNING']),b'\x01');self.put(move(m['FW_SCHED_LOCK']),b'\0');self.put(move(m['FW_INT_NESTING']),b'\0')
        self.set_word(move(m['FW_CURRENT_TCB']),0x80f00000);self.put(0x80f00035,b'\x09');self.set_word(move(m['FW_TASK_TABLE'])+36,0x80f00000)
        for slot,handler in [('FW_TICK_IRQ_SLOT','FW_TICK_IRQ_HANDLER'),('FW_USB_IRQ_SLOT','FW_NATIVE_USB_IRQ')]:
            self.put(move(m[slot]),packed([move(m[handler]),0]))
    def call(self,name,*args):
        u=self.u;u.reg_write(UC_MIPS_REG_CP0_STATUS,0x10000000);u.reg_write(UC_MIPS_REG_SP,0x819f0000);u.reg_write(UC_MIPS_REG_RA,self.sentinel)
        for register,value in zip((UC_MIPS_REG_A0,UC_MIPS_REG_A1),args):u.reg_write(register,value)
        u.emu_start(self.symbols[name],self.sentinel,count=300000000)
        if u.reg_read(UC_MIPS_REG_PC)!=self.sentinel:raise AssertionError(f'discovery did not return within its instruction budget: PC={u.reg_read(UC_MIPS_REG_PC):#x}')
        return u.reg_read(UC_MIPS_REG_V0)
    def info(self):
        count=self.call('fw_discovery_info',0x819e0000,128)
        return struct.unpack('<'+'I'*count,self.get(0x819e0000,4*count))

class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:cls.gcc,cls.objcopy=builder.find_toolchain()
        except RuntimeError as error:raise unittest.SkipTest(str(error))
        cls.temp=tempfile.TemporaryDirectory();cls.folder=Path(cls.temp.name)
        builddir=Path(os.environ.get('BBK'+TARGET+'_BUILD_DIR',SOURCE.parent/'build'/(TARGET+'-debug')))
        report=json.loads((builddir/'build.json').read_text(encoding='utf-8')) if (builddir/'build.json').is_file() else {}
        soc=report.get('soc','JZ4740');soc=soc if soc in builder.SOC_PROFILES else 'JZ4740'
        profilepath=builder.SOC_PROFILES[soc]
        cls.original=json.loads(profilepath.read_text(encoding='utf-8'))
        cls.synthetic,cls.segments=synthetic_profile(cls.original)
        cls.synthetic_raw,cls.synthetic_symbols=cls.compile(cls.synthetic,'synthetic')
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    @classmethod
    def compile(cls,profile,name,universal=False):
        folder=cls.folder/name;folder.mkdir()
        (folder/'profile_generated.h').write_text(builder.profile_header(profile),encoding='ascii')
        if universal:(folder/'launcher_profile_generated.h').write_text(builder.launcher_profile_header([profile]),encoding='ascii')
        (folder/'discovery_generated.h').write_text(gen['c_header']([profile]),encoding='ascii')
        script=folder/'fixture.ld';script.write_text('ENTRY(fw_matches)\nSECTIONS {. = 0x81a00000;.text : { *(.text*) }.rodata : { *(.rodata*) }.data : { *(.data*) *(.sdata*) }.bss : { *(.bss*) *(.sbss*) }}',encoding='ascii')
        flags=['-EL','-march=mips32','-mabi=32','-msoft-float','-mno-abicalls','-G0','-fno-pic','-O2','-ffreestanding','-fno-builtin','-fno-stack-protector','-ffunction-sections','-fdata-sections','-Wall','-Wextra','-Werror','-Wno-misleading-indentation','-Wl,-u,fw_discovery_info','-Wl,-u,fw_binding_id','-Wl,-u,fw_selected_soc','-I',folder,'-I',SOURCE]
        if universal:flags += ['-DRD_UNIVERSAL_LAUNCHER=1']
        elf,raw=builder.compile_link([SOURCE/'firmware.c'],'fixture',script,folder,cls.gcc,cls.objcopy,flags)
        with elf.open('rb') as stream:symbols={s.name:s['st_value'] for s in ELFFile(stream).get_section_by_name('.symtab').iter_symbols() if s['st_value']}
        return raw,symbols
    def model(self,mover=None):return Model(self.synthetic_raw,self.synthetic_symbols,self.synthetic,self.segments,mover=mover or self.moved)
    @staticmethod
    def moved(a):
        if not gen['ram'](a):return a
        if a<0x80200000:return a+0x100000
        if a<0x80600000:return a+0x20000
        return a+0x100000
    def test_scans_independently_moved_code_tables_and_private_globals(self):
        model=self.model();self.assertEqual(model.call('fw_matches'),1)
        info=model.info();self.assertEqual(info[1],2);self.assertEqual(info[7],3);self.assertGreater(info[6],0)
        names=gen['dynamic_names']([self.synthetic]);pairs=dict(zip(info[9::2],info[10::2]))
        for index,name in enumerate(names):self.assertEqual(pairs[index],self.moved(number(self.synthetic['macros'][name])),name)
        self.assertFalse(model.native_executions)
    def test_binding_identity_is_stable_and_changes_with_addresses(self):
        a=self.model();b=self.model(lambda value:value+0x10000)
        self.assertEqual(a.call('fw_matches'),1);one=a.call('fw_binding_id');self.assertEqual(a.call('fw_matches'),1);self.assertEqual(a.call('fw_binding_id'),one)
        self.assertEqual(b.call('fw_matches'),1);self.assertNotEqual(one,b.call('fw_binding_id'))
    def test_corrupted_jal_operand_or_helper_interior_is_rejected(self):
        for displacement in (4,0x100):
            with self.subTest(displacement=displacement):
                model=self.model();at=self.moved(0x80040000)+len(self.segments[0x80040000])-16
                wrong=self.moved(0x801d0000)+displacement
                model.set_word(at,0x0c000000|((wrong>>2)&0x3ffffff))
                self.assertEqual(model.call('fw_matches'),0)
    def test_duplicate_unanchored_board_root_is_rejected(self):
        model=self.model();root=0x801f0000;model.put(self.moved(root)+0x1000,self.segments[root])
        self.assertEqual(model.call('fw_matches'),0)
    def test_wrong_board_mmio_constant_is_rejected(self):
        model=self.model();model.set_word(self.moved(0x801f0000),0x3c0ab300)
        self.assertEqual(model.call('fw_matches'),0)
    def test_changed_instruction_or_control_flow_is_rejected(self):
        model=self.model();model.set_word(self.moved(0x80040000)+len(self.segments[0x80040000])-8,0x03e00009)
        self.assertEqual(model.call('fw_matches'),0)
    def test_collapsed_global_reference_map_is_rejected(self):
        values=sorted({number(b[0]) for b in self.synthetic['discovery']['bindings'].values()})
        a,b=values[:2];model=self.model(lambda value:self.moved(a) if value==b else self.moved(value))
        self.assertEqual(model.call('fw_matches'),0)
    def test_invalid_import_pointer_is_rejected_without_mmio(self):
        model=self.model();model.set_word(0x81c00004,0xb3040000)
        self.assertEqual(model.call('fw_matches'),0)
    def test_live_irq_and_scheduler_structures_are_required(self):
        for field in ('FW_USB_IRQ_SLOT','FW_TICK_IRQ_SLOT','FW_OS_RUNNING','FW_CURRENT_TCB'):
            with self.subTest(field=field):
                model=self.model();model.set_word(self.moved(model.macros[field]),0)
                self.assertEqual(model.call('fw_matches'),0)
    def test_universal_rejection_clears_selected_soc_and_dynamic_bindings(self):
        code,symbols=self.compile(self.synthetic,'universal',universal=True)
        model=Model(code,symbols,self.synthetic,self.segments,mover=self.moved)
        self.assertEqual(model.call('fw_matches'),1)
        self.assertEqual(model.call('fw_selected_soc'),int(self.synthetic['soc'][2:]))
        model.set_word(self.moved(model.macros['FW_USB_IRQ_SLOT']),0)
        self.assertEqual(model.call('fw_matches'),0)
        self.assertEqual(model.call('fw_selected_soc'),0)
        info=model.info();self.assertEqual(info[1],0)
        names=gen['dynamic_names']([self.synthetic]);pairs=dict(zip(info[9::2],info[10::2]))
        for index,name in enumerate(names):self.assertEqual(pairs[index],number(self.synthetic['macros'][name]))
    def test_missing_foreign_or_ambiguous_product_marker_is_rejected(self):
        for kind in ('missing','foreign','duplicate','both'):
            with self.subTest(kind=kind):
                model=self.model();at=self.moved(0x802f0000)
                foreign='9688' if TARGET=='9588' else '9588'
                if kind=='missing':model.put(at,bytes(7))
                elif kind=='foreign':model.put(at,(foreign+' OS').encode('ascii'))
                elif kind=='duplicate':model.put(at+16,(TARGET+' OS').encode('ascii'))
                else:model.put(at+16,(foreign+' OS').encode('ascii'))
                self.assertEqual(model.call('fw_matches'),0)
    def test_local_original_rom_and_synthetic_address_moved_rom(self):
        supplied=os.environ.get('BBK'+TARGET+'_FIRMWARE')
        if not supplied:self.skipTest('set local original firmware for independent decoded-reference validation')
        profile=self.original;raw=Path(supplied).read_bytes();size=number(profile['firmware']['size']);raw=raw[-size:]
        self.assertEqual(hashlib.sha256(raw).hexdigest(),profile['firmware']['sha256'])
        generated=gen['generate'](profile,raw)
        self.assertEqual(generated,profile['discovery'])
        code,symbols=self.compile(profile,'native')
        model=Model(code,symbols,profile,{number(profile['load_base']):raw})
        self.assertEqual(model.call('fw_matches'),1);self.assertEqual(model.info()[1],1)
        # Only debugger scanner code executes. The transformed kernel is a
        # relocation fixture, not an executable firmware compatibility claim.
        moved=Model(code,symbols,profile,{number(profile['load_base']):raw},mover=lambda a:a+0x10000)
        self.assertEqual(moved.call('fw_matches'),1);self.assertEqual(moved.info()[1],2)
        self.assertEqual(moved.info()[7],len(profile['discovery']['patterns']))
        self.assertFalse(moved.native_executions)

if __name__=='__main__':unittest.main()
