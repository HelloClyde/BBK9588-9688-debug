#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Execute the actual relocated resident management service in MIPS.

FS/media and native GUI queue boundaries are explicit models; optional local
ROM checks execute the real independent feature predicates. This suite opens
no serial port and does not claim physical-device or full-GUI acceptance.
"""
from pathlib import Path
import json, os, struct, unittest, zlib
from unicorn import UC_HOOK_CODE,UC_HOOK_MEM_WRITE
from unicorn.mips_const import UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,UC_MIPS_REG_A3,UC_MIPS_REG_V0
from test_device import USBModel, firmware_bytes

SOURCE=Path(__file__).resolve().parents[1]

def pack(*v):return struct.pack('<'+'I'*len(v),*v)
def path(p):
    b=p.encode('gbk') if isinstance(p,str) else p
    return struct.pack('<H',len(b))+b

def command(m,op,data=b''):
    scratch=0x81c10000;out=scratch+2048;status=out+1024
    m.put(scratch,data);m.put(status,pack(0xffffffff))
    n=m.call('rd_management_command',op,scratch,len(data),out,status)
    return m.word(status),m.get(out,n)

def action(m,a,data=b''):return command(m,41,pack(a)+data)
def unpack(data):return struct.unpack('<'+'I'*(len(data)//4),data)

class FilesystemModel:
    def __init__(self):
        self.m=USBModel(start=False);m=self.m
        profiles=json.loads((SOURCE/'management-bindings.json').read_text(encoding='utf-8'))['profiles']
        self.index,self.profile=next((i,p) for i,p in enumerate(profiles) if p['id'].endswith(m.report['soc'].lower()))
        m.put(m.symbols['binding'],pack(m.symbols['mgmt_profiles']+self.index*116))
        m.put(m.symbols['features'],pack(63))
        self.table=0x80700000;m.put(m.base+60,pack(self.table))
        self.files={b'A:\\existing.bin':b'original'};self.handles={};self.next_handle=10
        self.native_post_calls=0;self.calls=[];self.fail_write=False;self.ready=1;self.tick=0;self.post=[];self.gui_calls=[]
        self.addresses={}
        for offset in (0,4,8,12,16,20,28,36,40,48,60,64,68,84,116,124):
            a=0x80680000+offset*4;self.addresses[a]=offset;m.put(self.table+offset,pack(a));m.put(a,pack(0x03e00008,0))
        self.native_post=self.profile['post'];m.put(self.native_post,pack(0x03e00008,0))
        for key in ('reset_apps','scan_apps','reset_selection','launch'):m.put(self.profile[key],pack(0x03e00008,0))
        m.u.hook_add(UC_HOOK_CODE,self.hook)
        m.u.hook_add(UC_HOOK_MEM_WRITE,self.observe_queue,begin=0x718008,end=0x71800b)
        self.pool_base=0x80660000;self.gui_tcb=0x80750000;self.gui_stack=0x80760000
        m.put(self.profile['pool'],pack(0x24,64,0,self.pool_base));m.put(self.pool_base,bytes(64*0x24))
        m.put(self.profile['task_table'],bytes(64*4));m.put(self.profile['task_table']+9*4,pack(self.gui_tcb))
        m.put(self.gui_tcb,bytes(0x40));m.put(self.gui_tcb,pack(self.gui_stack));m.put(self.gui_tcb+0x35,b'\x09')
        m.put(self.gui_stack,pack(0x81c00400));m.put(self.gui_stack+0x70,pack(0x81c00400))
        m.put(self.profile['current_tcb'],pack(0x80751000))
        m.put(0x80718000,bytes(0x20))
        self.desktop=0x80710000;self.bda=0x80711000
        self.window(self.desktop,self.profile['desktop_handler'],b'Native Desktop')
        m.put(self.profile['foreground'],pack(self.desktop))
    def window(self,a,handler,title=b'Example'):
        m=self.m;m.put(a,bytes(0x114));m.put(a,struct.pack('<HH',1,0x11));m.put(a+0x88,pack(handler,a+0x200));m.put(a+0xf0,pack(0x80718000));m.put(a+0x200,title+b'\0')
    def string(self,a):
        out=bytearray()
        while self.m.get(a+len(out),1)!=b'\0':
            out.extend(self.m.get(a+len(out),1))
            if len(out)>1000:raise AssertionError('unterminated native string')
        return bytes(out)
    def observe_queue(self,u,access,address,size,value,_):
        if address==(0x80718008&0x1fffffff) and size==4 and value:
            self.post.append(struct.unpack('<4I',self.m.get(value,16)))
    def hook(self,u,address,size,_):
        m=self.m;physical=address&0x1fffffff
        registers=(UC_MIPS_REG_A0,UC_MIPS_REG_A1,UC_MIPS_REG_A2,UC_MIPS_REG_A3)
        a,b,c,d=(u.reg_read(r) for r in registers)
        if physical==(self.native_post&0x1fffffff):
            self.native_post_calls+=1;u.reg_write(UC_MIPS_REG_V0,0);return
        for key in ('reset_apps','scan_apps','reset_selection','launch'):
            if physical==(self.profile[key]&0x1fffffff):
                self.gui_calls.append((key,self.string(a) if key in ('scan_apps','launch') else None))
                if key=='launch':
                    self.window(self.bda,0x81c00120);m.put(self.profile['foreground'],pack(self.bda))
                u.reg_write(UC_MIPS_REG_V0,0);return
        match=next((v for k,v in self.addresses.items() if k&0x1fffffff==physical),None)
        if match is None:return
        o=match;self.calls.append((o,a,b,c,d));result=0
        if o==0:
            p,mode=self.string(a),self.string(b)
            if mode==b'wb':self.files[p]=b''
            if p not in self.files:result=0
            else:
                result=self.next_handle;self.next_handle+=1;self.handles[result]=[p,0,mode]
        elif o==4:
            if a not in self.handles:result=-1
            else:del self.handles[a]
        elif o==8:
            p,off,mode=self.handles[d];data=self.files[p][off:off+b*c];m.put(a,data);self.handles[d][1]+=len(data);result=len(data)
        elif o==12:
            if self.fail_write:result=0
            else:
                p,off,mode=self.handles[d];data=m.get(a,b*c);old=self.files[p];self.files[p]=old[:off]+data+old[off+len(data):];self.handles[d][1]+=len(data);result=len(data)
        elif o==16:
            if a not in self.handles:result=-1
            else:self.handles[a][1]=b;result=b
        elif o==36:
            p=self.string(a)
            if p in self.files:del self.files[p]
            else:result=-1
        elif o==40:
            p,q=self.string(a),self.string(b)
            if p not in self.files or q in self.files:result=-1
            else:self.files[q]=self.files.pop(p)
        elif o==84:
            p=self.string(a)
            if p not in self.files:result=-1
            else:m.put(b,struct.pack('<4H4I',0,0,0,0,len(self.files[p]),1,2,3))
        elif o==124:result=self.ready
        elif o==60:
            self.names=list(self.files);self.find_index=0
            result=self.find(c)
        elif o==64:self.find_index+=1;result=self.find(a)
        elif o in (68,116):pass
        else:raise AssertionError('unexpected native FS action '+str(o))
        u.reg_write(UC_MIPS_REG_V0,result&0xffffffff)
    def find(self,a):
        if self.find_index>=len(self.names):return -1
        p=self.names[self.find_index];name=p.rsplit(b'\\',1)[-1];self.m.put(a,bytes(0x220));self.m.put(a+4,pack(len(self.files[p]),0));self.m.put(a+18,name+b'\0');return 0
    def upload(self,name,data,crc=None):
        m=self.m;status,out=action(m,20,pack(len(data),zlib.crc32(data) if crc is None else crc)+path(name));assert status==0,(status,out)
        token=unpack(out)[0]
        for off in range(0,len(data),480):assert action(m,21,pack(token,off)+data[off:off+480])[0]==0
        status,out=action(m,22,pack(token));assert status==0
        return unpack(out)[0]
    def finish(self,j):
        for _ in range(256):
            self.m.call('rd_management_step');result=unpack(command(self.m,42,pack(j))[1])
            if result[2]>=3:return result
        raise AssertionError('job did not terminate')

class ManagementTests(unittest.TestCase):
    def test_unknown_feature_binding_rejects_before_any_native_call(self):
        m=USBModel(start=False);m.call('rd_management_reset')
        self.assertEqual(unpack(command(m,40)[1])[1],0)
        self.assertEqual(action(m,20,pack(1,0)+path('A:/new.bin')),(3,b''))
    def test_upload_is_sequential_verified_by_reread_and_renamed(self):
        f=FilesystemModel();data=bytes(range(256))*32;j=f.upload('A:/new.tmp',data)
        status=f.finish(j);self.assertEqual((status[2],status[4],status[7]),(3,len(data),zlib.crc32(data)))
        self.assertEqual(action(f.m,7,path('A:/new.tmp')+path('A:/installed.bin')),(0,b''))
        self.assertEqual(f.files[b'A:\\installed.bin'],data)
        self.assertFalse(f.handles)
    def test_crc_mismatch_never_replaces_existing_file(self):
        f=FilesystemModel();j=f.upload('A:/bad.tmp',b'bad',crc=0);status=f.finish(j)
        self.assertEqual(status[2],4);self.assertEqual(f.files[b'A:\\existing.bin'],b'original')
        self.assertEqual(action(f.m,7,path('A:/bad.tmp')+path('A:/existing.bin'))[0],2)
    def test_gbk_trail_backslash_and_b_volume_preserved(self):
        f=FilesystemModel();name=b'B:/\x81\x5c/file.tmp';data=b'GBK';j=f.upload(name,data);self.assertEqual(f.finish(j)[2],3)
        self.assertEqual(f.files[b'B:\\\x81\x5c\\file.tmp'],data)
    def test_bad_paths_never_reach_native_open(self):
        f=FilesystemModel();before=len(f.calls)
        for p in (b'A:/../bad',b'A:/./bad',b'A:/bad//x',b'../x',b'C:/x',b'A:/bad\0x',b'A:/\x81'):
            with self.subTest(p=p):self.assertEqual(action(f.m,20,pack(1,0)+path(p))[0],1)
        self.assertEqual(len(f.calls),before)
    def test_partial_write_and_unknown_outcome_are_queryable(self):
        f=FilesystemModel();status,out=action(f.m,20,pack(100,0)+path('A:/tx.tmp'));token=unpack(out)[0]
        self.assertEqual(action(f.m,21,pack(token,1)+b'x')[0],1)
        self.assertEqual(action(f.m,21,pack(token,0)+b'abc')[0],0)
        f.m.call('rd_management_disconnect')
        state=action(f.m,8)[1];self.assertEqual(unpack(state[:32])[2:5],(token,100,3))
        self.assertEqual(action(f.m,23,pack(token)),(0,b''));self.assertFalse(f.handles)
        self.assertEqual(f.files[b'A:\\tx.tmp'],b'abc')
    def test_crc_survives_disconnect_but_shutdown_closes(self):
        f=FilesystemModel();f.files[b'A:\\hash.bin']=b'x'*20000
        j=unpack(action(f.m,30,path('A:/hash.bin'))[1])[0]
        f.m.call('rd_management_step');f.m.call('rd_management_disconnect')
        self.assertEqual(unpack(command(f.m,42,pack(j))[1])[2],2)
        f.m.call('rd_management_shutdown');self.assertFalse(f.handles)
        self.assertEqual(unpack(command(f.m,42,pack(j))[1])[2],5)
    def test_read_directory_and_tokens_reject_stale_clients(self):
        f=FilesystemModel();status,out=action(f.m,2,path('A:/'));token=unpack(out)[0]
        self.assertEqual(action(f.m,3,pack(token))[0],0)
        self.assertEqual(action(f.m,4,pack(token)),(0,b''))
        self.assertEqual(action(f.m,3,pack(token))[0],1)
        status,out=action(f.m,10,path('A:/existing.bin'));token,size=unpack(out)
        self.assertEqual(action(f.m,11,pack(token,0,size))[1],pack(token,0,size)+b'original')
        f.m.call('rd_management_shutdown');self.assertFalse(f.handles)
    def test_bda_handler_range_foreground_and_release_on_disconnect(self):
        f=FilesystemModel();m=f.m;f.window(f.bda,0x81c00120);m.put(f.profile['foreground'],pack(f.bda))
        self.assertEqual(unpack(action(m,50,pack(0))[1][:40])[1],f.bda)
        self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0))[0],6)
        self.assertEqual(action(m,53,pack(f.bda,1,1,0,0)),(0,b''))
        self.assertEqual(f.post[-1],(f.bda,0x10,1,0));m.call('rd_management_disconnect')
        self.assertEqual(f.post[-1],(f.bda,0x12,1,0))
    def test_touch_is_packed_and_never_released_into_reused_handle(self):
        f=FilesystemModel();m=f.m
        self.assertEqual(action(m,53,pack(f.desktop,5,123,245,0)),(0,b''))
        self.assertEqual(f.post[-1],(f.desktop,1,0,123|(245<<16)))
        m.call('rd_management_disconnect');self.assertEqual(f.post[-1],(f.desktop,2,0,123|(245<<16)))
        self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0)),(0,b''));before=len(f.post)
        m.put(f.desktop+0x88,pack(0x81c00400));m.call('rd_management_disconnect');self.assertEqual(len(f.post),before)
    def test_input_expected_tuple_and_release_queue_ownership(self):
        f=FilesystemModel();m=f.m;f.window(f.bda,0x81c00120);m.put(f.profile['foreground'],pack(f.bda))
        request=pack(f.bda,1,1,0,0,0x81c00120,0x80718000)
        self.assertEqual(action(m,53,request),(0,b''))
        before=len(f.post);m.put(f.bda+0xf0,pack(0x80719000));m.call('rd_management_disconnect')
        self.assertEqual(len(f.post),before)
        self.assertEqual(action(m,53,request)[0],6)
        self.assertEqual(action(m,53,pack(f.bda,1,1,0,0,0x81c00124,0x80719000))[0],6)

    def test_stale_private_callback_after_cancel_and_new_crc_job(self):
        f=FilesystemModel();m=f.m;j=unpack(action(m,51,pack(f.desktop))[1])[0]
        self.assertEqual(command(m,44,pack(j)),(0,b''))
        crc=unpack(action(m,30,path('A:/existing.bin'))[1])[0]
        self.assertNotEqual(crc,j)
        m.call('rd_management_gui_bridge',f.desktop,0x7dd,j,0)
        self.assertEqual(unpack(command(m,42,pack(crc))[1])[3],30)
        self.assertFalse(f.gui_calls)
        self.assertEqual(f.finish(crc)[2],3)

    def test_stale_normal_callback_routes_checked_native_handler_after_cancel(self):
        f=FilesystemModel();m=f.m;j=unpack(action(m,51,pack(f.desktop))[1])[0]
        command(m,44,pack(j));action(m,30,path('A:/existing.bin'))
        # Model native handler boundary to prove no zero/new-job function call.
        target=f.profile['desktop_handler'];m.put(target,pack(0x03e00008,0x2402002a))
        self.assertEqual(m.call('rd_management_gui_bridge',f.desktop,0xb1,0,0),42)

    def test_inflight_loader_context_survives_transport_reset_and_new_crc_job(self):
        f=FilesystemModel();m=f.m
        # This is the exact suspended-native-loader state: GUI has restored its
        # handler and worker observed the new BDA frame. Its stack is still live.
        j=unpack(action(m,52,pack(f.desktop)+path('A:/existing.bin'))[1])[0]
        m.put(f.desktop+0x88,pack(f.profile['desktop_handler']))
        m.put(m.symbols['gui_inflight'],pack(1));m.put(m.symbols['job']+4,pack(3))
        m.call('rd_management_reset')
        self.assertEqual(m.word(m.symbols['gui_inflight']),1)
        self.assertEqual(action(m,51,pack(f.desktop))[0],2)
        self.assertEqual(action(m,30,path('A:/existing.bin'))[0],0)
        self.assertEqual(m.call('rd_management_busy'),1)

    def test_pool_exhaustion_never_calls_allocator_or_native_post(self):
        f=FilesystemModel();m=f.m
        m.put(f.pool_base,pack(2)* (64*0x24//4))
        self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0))[0],2)
        self.assertEqual(f.native_post_calls,0);self.assertFalse(f.post)
        m.put(f.profile['pool']+12,pack(0))
        self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0))[0],2)
        self.assertEqual(f.native_post_calls,0)

    def test_keyup_slot_is_reserved_even_when_every_other_slot_is_full(self):
        f=FilesystemModel();m=f.m
        self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0)),(0,b''))
        reserved=m.word(m.symbols['held_release_node']);self.assertNotEqual(reserved,0)
        for i in range(64):
            slot=f.pool_base+i*0x24
            if slot!=reserved-4:m.put(slot,pack(2))
        m.call('rd_management_disconnect')
        self.assertEqual(f.post[-1],(f.desktop,0x12,1,0))
        self.assertEqual(m.call('rd_management_input_pending'),0)
        self.assertEqual(f.native_post_calls,0)

    def test_saved_pop_and_pool_contexts_defer_input_and_release(self):
        f=FilesystemModel();m=f.m
        self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0)),(0,b''))
        before=len(f.post);m.put(f.gui_stack+0x70,pack(f.profile['critical_start']+4))
        m.call('rd_management_disconnect')
        self.assertEqual(len(f.post),before);self.assertEqual(m.call('rd_management_input_pending'),1)
        m.put(f.gui_stack+0x70,pack(0x81c00400));m.call('rd_management_step')
        # disconnect makes release pending immediately, independent of hold duration.
        self.assertEqual(f.post[-1],(f.desktop,0x12,1,0))
        self.assertEqual(m.call('rd_management_input_pending'),0)

    def test_idle_context_unwind_rejects_woken_sem_and_accepts_gui_delay(self):
        f=FilesystemModel();m=f.m;p=f.profile
        m.put(f.gui_stack,pack(p['sched_continuation']));m.put(f.gui_stack+0x70,pack(p['sched_continuation']))
        m.put(f.gui_stack+0x90,pack(p['delay_continuation']));m.put(f.gui_stack+0xa8,pack(0x8000bb54))
        self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0))[0],2)
        m.put(f.gui_stack+0xa8,pack(p['idle_delay_return']))
        self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0)),(0,b''))

    def test_real_stock_gui_queue_wait_snapshot_accepts_only_exact_idle_chain(self):
        f=FilesystemModel();m=f.m;p=f.profile
        # Captured in the full stock C200 GUI9 with ordinary TaskCreate:
        # RA/EPC=OS_Sched+0xc0, caller90=OSQPend+0xf4, callerA8=queue owner.
        m.put(f.gui_stack,pack(p['sched_continuation']));m.put(f.gui_stack+0x70,pack(p['sched_continuation']))
        m.put(f.gui_stack+0x90,pack(p['queue_pend_continuation']))
        m.put(f.gui_stack+0xa8,pack(0x80825840))
        m.put(f.gui_stack+0xb4,pack(p['raw_wait_return']))
        m.put(f.gui_stack+0xf8,pack(p['idle_delay_return']))
        m.put(f.gui_stack+0x114,pack(p['message_idle_return']))
        for status in (0,4):
            m.put(f.gui_tcb+0x34,bytes([status]))
            self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0)),(0,b''))
            m.call('rd_management_disconnect')
            self.assertEqual(m.call('rd_management_input_pending'),0)
        m.put(f.gui_tcb+0x34,b'\x08')
        self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0))[0],2)
        m.put(f.gui_tcb+0x34,b'\0')
        for offset in (0x90,0xb4,0xf8,0x114):
            original=m.word(f.gui_stack+offset);m.put(f.gui_stack+offset,pack(original+4))
            self.assertEqual(action(m,53,pack(f.desktop,1,1,0,0))[0],2)
            m.put(f.gui_stack+offset,pack(original))

    def test_loaded_bda_title_uses_full_ram_read_window(self):
        f=FilesystemModel();m=f.m;f.window(f.bda,0x81c00120);m.put(f.profile['foreground'],pack(f.bda))
        m.put(f.bda+0x8c,pack(0x81c01000));m.put(0x81c01000,b'Probe Title\0')
        result=action(m,50,pack(0))[1]
        self.assertEqual(unpack(result[:40])[9],11);self.assertEqual(result[40:],b'Probe Title')

    def test_same_title_bda_desktop_is_not_native_desktop(self):
        f=FilesystemModel();m=f.m;f.window(f.bda,0x81c00120,b'Native Desktop');m.put(f.profile['foreground'],pack(f.bda))
        self.assertEqual(unpack(action(m,55)[1])[1],0)
        self.assertEqual(action(m,51,pack(f.bda))[0],6)
    def test_refresh_executes_reset_before_scan_on_gui_callback(self):
        f=FilesystemModel();m=f.m;original=f.profile['desktop_handler'];j=unpack(action(m,51,pack(f.desktop))[1])[0]
        self.assertFalse(f.gui_calls);self.assertEqual(m.word(f.desktop+0x88),m.symbols['rd_management_gui_bridge'])
        m.call('rd_management_gui_bridge',f.desktop,0x7dd,j,0)
        self.assertEqual([r[0] for r in f.gui_calls],['reset_apps','scan_apps','reset_selection'])
        self.assertEqual(m.word(f.desktop+0x88),original)
        self.assertEqual(unpack(command(m,42,pack(j))[1])[2],3)
    def test_checked_native_grid_is_an_independent_desktop_role(self):
        f=FilesystemModel();m=f.m;f.window(f.desktop,f.profile['grid_handler'])
        self.assertEqual(unpack(action(m,55)[1]),(f.desktop,f.desktop,f.profile['grid_handler']))
        self.assertEqual(action(m,51,pack(f.desktop))[0],0)

    def test_pending_gui_job_cancel_restores_only_owned_hook(self):
        f=FilesystemModel();m=f.m;j=unpack(action(m,51,pack(f.desktop))[1])[0]
        self.assertEqual(command(m,44,pack(j+1))[0],1)
        m.call('rd_management_disconnect')
        self.assertEqual(m.word(f.desktop+0x88),f.profile['desktop_handler'])
        self.assertEqual(unpack(command(m,42,pack(j))[1])[2],5)

    def test_local_original_rom_feature_predicates_and_independent_corruption(self):
        m=USBModel(start=False);raw=firmware_bytes(m);m.put(int(m.report['firmware']['load_base'],0),raw)
        tables=[m.macros[x] for x in ('FW_GUI_TABLE','FW_FS_TABLE','FW_SYS_TABLE','FW_MEM_TABLE','FW_RES_TABLE')]
        m.put(m.base+56,pack(*tables));m.call('rd_management_reset')
        self.assertEqual(unpack(command(m,40)[1])[1],63)
        records=json.loads((SOURCE/'management-bindings.json').read_text(encoding='utf-8'))['profiles']
        p=next(p for p in records if p['id'].endswith(m.report['soc'].lower()))
        address=p['gui_guards'][-1]['address'];before=m.get(address,1);m.put(address,bytes([before[0]^1]));m.call('rd_management_reset')
        self.assertEqual(unpack(command(m,40)[1])[1],3)
        address=p['fs_guards'][1]['address'];before=m.get(address,1);m.put(address,bytes([before[0]^1]));m.call('rd_management_reset')
        self.assertEqual(unpack(command(m,40)[1])[1],0)

if __name__=='__main__':unittest.main()
