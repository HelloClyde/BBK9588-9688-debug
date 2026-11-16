# SPDX-License-Identifier: GPL-3.0-only
"""Management wire contract, transaction failures, and persistent sessions.

The peer is an in-memory protocol fixture, not firmware or USB validation.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zlib

spec = importlib.util.spec_from_file_location("workflow_test_client", Path(__file__).resolve().parents[1] / "client.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
workflow = client._workflow


def packed(*values):
    return struct.pack(f"<{len(values)}I", *values)


def path_at(data, offset=0):
    size, = struct.unpack_from("<H", data, offset)
    end = offset + 2 + size
    return data[offset + 2:end].decode("gbk"), end


class Peer:
    def __init__(self):
        self.pending = bytearray()
        self.calls = []
        self.files = {}
        self.sessions = {}
        self.next_token = 1
        self.job = (1, 3, 0, 0, 0)
        self.features = 63
        self.corrupt_crc = False
        self.fail_rename = None
        self.drop_rename = None
        self.closed = False
        self.event_entries = [(20, 100, 1, 999), (21, 105, 2, 0x29000014)]
        self.image = None
        self.screen_requests = []
        self.active_handle, self.active_handler, self.queue_owner = 0x9000, 0x81c00020, 3
        self.desktop_handler = 0x80020000
        self.injected = []

    def token(self, state):
        result = self.next_token
        self.next_token += 1
        self.sessions[result] = state
        return result

    def job_start(self, path, action):
        data = self.files[path]
        crc = zlib.crc32(data) ^ int(self.corrupt_crc)
        self.job = (self.job[0] + 1, 3, 0, crc, len(data))
        self.job_action = action
        return packed(self.job[0])

    def write(self, packet):
        _, _, op, seq, count, _ = client.HEADER.unpack(packet[:12])
        data = packet[12:12+count]
        self.calls.append((op, data))
        status, reply = self.respond(op, data)
        if reply is not None:
            self.pending.extend(client.frame(op | 128, seq, reply, status=status))
        return len(packet)

    def read(self, count):
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result

    def close(self):
        self.closed = True

    def respond(self, op, data):
        if op == 1:return 0, client.IDENTITY
        if op == 9:return 0, packed(1,511,512,0x80000000,0x2000000,240,320,123)
        if op == 2:return 0, packed(*([1,1,1]+[0]*13))
        if op == 40:return 0, packed(1,self.features,480,240,0,0,0,0,0,0,0,0)
        if op == 47:return 0, packed(1,2,4740,0x12345678,0,2,4096,8,21,0,0x80001234,1,0x80005678)
        if op == 48:
            cursor, = struct.unpack("<I",data)
            entries = [entry for entry in self.event_entries if entry[0] > cursor]
            return 0, packed(1,entries[-1][0] if entries else cursor,20,len(entries))+b"".join(packed(*entry) for entry in entries)
        if op == 42:return 0, packed(1,self.job[0],self.job[1],self.job_action,self.job[4],self.job[4],self.job[2],self.job[3])
        if op == 43:return 0, packed(*self.job)
        if op == 44:return 0, b""
        if op == 6:
            if self.image is None:return 3,b""
            self.screen_requests.append(data)
            selected = self.image
            if data:
                x,y,w,h=struct.unpack("<4I",data);selected=selected.crop((x,y,x+w,y+h))
            raw = bytearray()
            pixels=selected.tobytes()
            for offset in range(0,len(pixels),3):
                r,g,b=pixels[offset:offset+3];raw.extend((b,g,r,0))
            self.screen_data=bytes(raw)
            return 0, packed(5,selected.width,selected.height,selected.width*4,2,len(raw),0xa0f82000,22)
        if op == 7:
            token,offset,count=struct.unpack("<III",data)
            return 0, packed(token,offset,len(self.screen_data))+self.screen_data[offset:offset+count]
        if op == 8:return 0,b""
        if op != 41:raise AssertionError(f"unexpected op {op}")
        action,=struct.unpack_from("<I",data);data=data[4:]
        if action == 1:
            path,_=path_at(data)
            return (0,packed(len(self.files[path]),0,0,0,0)) if path in self.files else (4,b"")
        if action == 8:
            return 0,packed(0,0,0,0,0,0,self.job[0],0)
        if action == 2:
            directory,_=path_at(data)
            names = [path.rsplit("/",1)[1] for path in self.files if path.rsplit("/",1)[0] == directory.rstrip("/")]
            token=self.token({"directory":directory,"names":iter(names)})
            return 0,packed(token)
        if action == 3:
            token,=struct.unpack("<I",data);state=self.sessions[token]
            name=next(state["names"],"");raw=name.encode("gbk")
            return 0,packed(token,len(self.files[state["directory"].rstrip("/")+"/"+name]) if name else 0,0,len(raw))+raw
        if action in (4,12,23):
            token,=struct.unpack("<I",data);del self.sessions[token];return 0,b""
        if action == 6:
            path,_=path_at(data)
            if path not in self.files:return 4,b""
            del self.files[path];return 0,b""
        if action == 7:
            old,offset=path_at(data);new,_=path_at(data,offset)
            if self.fail_rename and self.fail_rename(old,new):
                self.fail_rename=None;return 5,b""
            if old not in self.files or new in self.files:return 5,b""
            self.files[new]=self.files.pop(old)
            if self.drop_rename and self.drop_rename(old,new):return 0,None
            return 0,b""
        if action == 10:
            path,_=path_at(data)
            token=self.token({"path":path});return 0,packed(token,len(self.files[path]))
        if action == 11:
            token,offset,count=struct.unpack("<III",data);contents=self.files[self.sessions[token]["path"]]
            return 0,packed(token,offset,len(contents))+contents[offset:offset+count]
        if action == 20:
            size,crc=struct.unpack_from("<II",data);path,_=path_at(data,8)
            if path in self.files:return 5,b""
            self.files[path]=b"";token=self.token({"path":path,"size":size,"crc":crc})
            return 0,packed(token)
        if action == 21:
            token,offset=struct.unpack_from("<II",data);path=self.sessions[token]["path"]
            if offset != len(self.files[path]):return 1,b""
            self.files[path] += data[8:];return 0,packed(len(self.files[path]))
        if action == 22:
            token,=struct.unpack("<I",data);path=self.sessions.pop(token)["path"]
            return 0,self.job_start(path,22)
        if action == 30:
            path,_=path_at(data);return 0,self.job_start(path,30)
        if action == 50:
            index,=struct.unpack("<I",data)
            if index:return 0,packed(index,*([0]*9))
            title="测试窗口".encode("gbk")
            return 0,packed(0,self.active_handle,0,0,0,self.active_handler,self.queue_owner,0,self.active_handle,len(title))+title
        if action == 55:
            return 0,packed(self.active_handle,self.active_handle if self.active_handler==self.desktop_handler else 0,self.desktop_handler)
        if action in (51,52):
            self.job=(self.job[0]+1,3,0,0,1);self.job_action=action
            if action==52:self.active_handle,self.active_handler=0x9000,0x81c00020
            return 0,packed(self.job[0])
        if action == 53:
            values=struct.unpack("<7I",data)
            if (values[0],values[5],values[6]) != (self.active_handle,self.active_handler,self.queue_owner):return 6,b""
            self.injected.append(values)
            return 0,b""
        if action == 54:return 0,b""
        raise AssertionError(f"unexpected action {action}")


class WorkflowTests(unittest.TestCase):
    def connection(self, peer=None):
        peer=peer or Peer()
        result=client.Client(peer,timeout=.5)
        result.handshake()
        return result,peer

    def test_absolute_chinese_paths_are_lossless_and_unsafe_paths_reject(self):
        path="b:\\应用\\程序\\测试.bda"
        self.assertEqual(path_at(workflow.pack_path(path))[0],"B:/应用/程序/测试.bda")
        for bad in ("应用/a.bda","C:/x","A:/../x","A:/x/./y","A://x","A:/x\x00y","A:/x*","A:/😀", "A:/"+"汉"*121):
            with self.subTest(path=bad),self.assertRaises((ValueError,UnicodeError)):
                workflow.pack_path(bad)

    def test_old_and_partial_bdas_fail_before_mutating_request(self):
        c,p=self.connection();c.caps=(1,31,512,0x80000000,0x2000000,240,320,1)
        before=len(p.calls)
        with self.assertRaises(workflow.FeatureUnavailable):c.fs_remove("A:/x")
        self.assertEqual(len(p.calls),before)
        c,p=self.connection();p.features=1
        with self.assertRaises(workflow.FeatureUnavailable):c.input_event(0x9000,"tap",code=0x1c)
        self.assertNotIn(41,[op for op,_ in p.calls])

    def test_bundle_stages_all_files_before_replacing_any_and_keeps_backups(self):
        c,p=self.connection();p.files={"A:/app.bda":b"old-app","B:/资源.bin":b"old-resource"}
        with tempfile.TemporaryDirectory() as directory:
            one,two=Path(directory)/"app.bda",Path(directory)/"res.bin"
            one.write_bytes(b"new-app"*200);two.write_bytes(b"new-resource"*130)
            result=c.deploy([{"source":one,"target":"A:/app.bda"},{"source":two,"target":"B:/资源.bin"}])
        self.assertEqual(result["state"],"committed")
        self.assertEqual(p.files["A:/app.bda"],b"new-app"*200)
        self.assertEqual(p.files[result["files"][0]["backup"]],b"old-app")
        actions=[struct.unpack_from("<I",data)[0] for op,data in p.calls if op==41]
        self.assertEqual(actions[:actions.index(7)].count(22),2)
        writes=[data for op,data in p.calls if op==41 and struct.unpack_from("<I",data)[0]==21]
        self.assertTrue(all(len(data)-12<=480 for data in writes))
        self.assertTrue(all(len(entry["temporary"].rsplit("/",1)[1].split(".")[0])==8 for entry in result["files"]))
        self.assertGreater(result["files"][0]["upload"]["payload_bytes_per_second"],0)

    def test_known_rename_failure_rolls_back_previous_file_and_current_backup(self):
        c,p=self.connection();p.files={"A:/one.bda":b"one-old","A:/two.bda":b"two-old"}
        p.fail_rename=lambda old,new:new=="A:/two.bda" and old.lower().endswith(".tmp")
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"new";source.write_bytes(b"new")
            with self.assertRaises(client.DeviceError):c.deploy([{"source":source,"target":"A:/one.bda"},{"source":source,"target":"A:/two.bda"}])
        self.assertEqual(p.files["A:/one.bda"],b"one-old")
        self.assertEqual(p.files["A:/two.bda"],b"two-old")
        self.assertEqual(c.last_deployment["state"],"rolled-back")

    def test_lost_rename_ack_never_replays_or_rolls_back_an_unknown_outcome(self):
        c,p=self.connection();p.files={"A:/app.bda":b"old"}
        c.timeout=.001
        p.drop_rename=lambda old,new:new.lower().endswith(".bak")
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"app";source.write_bytes(b"new")
            with self.assertRaises(TimeoutError):c.deploy([{"source":source,"target":"A:/app.bda"}])
        actions=[struct.unpack_from("<I",data)[0] for op,data in p.calls if op==41]
        self.assertEqual(actions.count(7),1)
        self.assertEqual(c.last_deployment["state"],"outcome-unknown")
        self.assertTrue(c.broken)

    def test_readback_crc_failure_does_not_replace_the_live_file(self):
        c,p=self.connection();p.files={"A:/app.bda":b"old"};p.corrupt_crc=True
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"app";source.write_bytes(b"new")
            with self.assertRaises(workflow.ManagementError):c.deploy([{"source":source,"target":"A:/app.bda"}])
        self.assertEqual(p.files["A:/app.bda"],b"old")
        self.assertFalse(any(op==41 and struct.unpack_from("<I",data)[0]==7 for op,data in p.calls))

    def test_matching_device_crc_skips_whole_file_upload(self):
        c,p=self.connection();p.files={"A:/app.bda":b"same"}
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"app";source.write_bytes(b"same")
            result=c.deploy([{"source":source,"target":"A:/app.bda"}])
        self.assertTrue(result["files"][0]["skipped"])
        self.assertFalse(any(op==41 and struct.unpack_from("<I",data)[0] in (7,20,21,22) for op,data in p.calls))

    def test_download_is_checked_before_replacing_the_host_file(self):
        c,p=self.connection();p.files={"A:/data":bytes(range(256))*7}
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/"download"
            result=c.fs_download("A:/data",output)
            self.assertEqual(output.read_bytes(),p.files["A:/data"])
            self.assertEqual(result["bytes"],1792)
            output.write_bytes(b"preserve");p.corrupt_crc=True
            with self.assertRaises(workflow.ManagementError):c.fs_download("A:/data",output)
            self.assertEqual(output.read_bytes(),b"preserve")

    def test_empty_file_upload_download_and_directory_cursor_cleanup(self):
        c,p=self.connection()
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"empty";source.write_bytes(b"")
            c.fs_upload(source,"A:/空白")
            c.fs_download("A:/空白",Path(directory)/"copy")
        self.assertEqual(c.fs_list("A:/"),[{"name":"空白","path":"A:/空白","size":0,"attributes":0,"directory":False}])
        self.assertEqual(p.sessions,{})

    def test_manual_key_release_on_close_and_same_connection_context(self):
        c,p=self.connection()
        with c:
            c.input_event(0x9000,"down",code=0x1c)
            self.assertEqual(c.status()["state"],1)
        self.assertTrue(p.closed)
        self.assertEqual(struct.unpack_from("<I",p.calls[-1][1])[0],54)
        self.assertEqual(sum(op==1 for op,_ in p.calls),1)
        self.assertEqual(p.injected[0],(0x9000,1,0x1c,0,1,0x81c00020,3))

    def test_timed_hold_keeps_the_connection_open_until_device_releases(self):
        c,p=self.connection();original=p.respond;polls=[]
        def respond(op,data):
            if op==40 and any(a==41 and struct.unpack_from("<I",b)[0]==53 for a,b in p.calls):
                polls.append(1)
                return 0,packed(1,63,480,240,0,0,0,0,0,0,0x9000 if len(polls)<3 else 0,0x1c)
            return original(op,data)
        p.respond=respond
        result=c.input_event(0x9000,"hold",code=0x1c,duration_ticks=20)
        self.assertTrue(result["accepted"])
        self.assertEqual(len(polls),3)
        self.assertFalse(p.closed)
        self.assertFalse(c._held_input)

    def test_io_recovery_query_and_explicit_abort_preserve_partial_file(self):
        c,p=self.connection();p.files["A:/partial"]=b"part"
        token=p.token({"path":"A:/partial"})
        self.assertEqual(c.fs_io_state()["writer_token"],0)
        self.assertTrue(c.fs_abort(token)["partial_file_retained"])
        self.assertEqual(p.files["A:/partial"],b"part")

    def test_windows_reveal_handler_owner_and_unique_foreground(self):
        c,_=self.connection();windows=c.windows()
        self.assertEqual(len(windows),1)
        self.assertEqual(windows[0]["title"],"测试窗口")
        self.assertEqual(windows[0]["handler"],0x81c00020)
        self.assertEqual(windows[0]["foreground"],windows[0]["handle"])

    def test_discovery_report_decodes_scanned_mode_and_addresses(self):
        c,_=self.connection();result=c.firmware_info()
        self.assertEqual(result["mode_name"],"startup-scan")
        self.assertEqual(result["bindings"][1]["address_hex"],"0x80005678")
        self.assertFalse(result["truncated"])

    def test_event_log_reports_truncation_without_generating_mutations(self):
        c,p=self.connection();result=c.logs()
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_cursor"],21)
        self.assertEqual([entry["sequence"] for entry in result["entries"]],[20,21])
        self.assertNotIn(41,[op for op,_ in p.calls])

    def test_roi_matches_full_display_crop_for_every_rotation(self):
        from PIL import Image
        native=Image.new("RGB",(240,320))
        native.putdata([(x%256,y%256,(x*3+y*5)%256) for y in range(320) for x in range(240)])
        for rotation in (0,90,180,270):
            with self.subTest(rotation=rotation),tempfile.TemporaryDirectory() as directory:
                c,p=self.connection();p.image=native
                output=Path(directory)/"roi.png"
                result=c.screenshot(output,region=(13,17,27,31),rotation_degrees=rotation)
                display=native if not rotation else native.transpose({90:Image.Transpose.ROTATE_90,180:Image.Transpose.ROTATE_180,270:Image.Transpose.ROTATE_270}[rotation])
                with Image.open(output) as actual:
                    self.assertEqual(actual.tobytes(),display.crop((13,17,40,48)).tobytes())
                self.assertEqual(result["bytes"],27*31*4)
                self.assertLess(result["bytes"],result["full_screen_bytes"])
                self.assertLess(result["chunks"],(result["full_screen_bytes"]+499)//500)

    def test_roi_rejects_old_capability_or_bad_bounds_before_capture(self):
        c,p=self.connection();c.caps=(1,31,512,0x80000000,0x2000000,240,320,1)
        with self.assertRaises(workflow.FeatureUnavailable):c.screenshot(Path("unused.png"),region=(0,0,1,1))
        c.caps=(1,511,512,0x80000000,0x2000000,240,320,1)
        for bounds in ((-1,0,1,1),(0,0,0,1),(239,0,2,1),(0,319,1,2)):
            with self.assertRaises(ValueError):c.screenshot(Path("unused.png"),region=bounds)
        self.assertNotIn(6,[op for op,_ in p.calls])

    def test_local_baseline_reports_exact_changed_pixel_and_bbox(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            baseline,actual=Path(directory)/"before.png",Path(directory)/"after.png"
            image=Image.new("RGB",(12,8));image.save(baseline)
            image.putpixel((3,5),(255,128,1));image.save(actual)
            result=workflow.screen_comparison(actual,baseline)
            self.assertEqual(result["changed_pixels"],1)
            self.assertEqual(result["changed_bbox"],(3,5,4,6))

    def test_session_report_preserves_failure_evidence_and_one_handshake(self):
        from PIL import Image
        c,p=self.connection();p.image=Image.new("RGB",(240,320))
        with tempfile.TemporaryDirectory() as directory:
            result=c.run_steps([{"action":"status"},{"action":"not-a-command"},{"action":"status"}],report_directory=directory)
            saved=json.loads((Path(directory)/"report.json").read_text(encoding="utf-8"))
            self.assertFalse(result["ok"])
            self.assertEqual(len(saved["stages"]),2)
            self.assertEqual(saved["stages"][1]["error_type"],"ValueError")
            self.assertTrue((Path(directory)/"failure.png").is_file())
            self.assertIn("logs_after",saved)
        self.assertEqual(sum(op==1 for op,_ in p.calls),1)

    def test_unknown_outcome_stops_plan_without_further_rpc_or_retry(self):
        c,p=self.connection()
        c.timeout=.001
        original=p.respond
        def respond(op,data):
            if op==41 and struct.unpack_from("<I",data)[0]==53:return 0,None
            return original(op,data)
        p.respond=respond
        result=c.run_steps([{"action":"input","target":0x9000,"kind":"tap","code":0x1c},{"action":"status"}])
        self.assertFalse(result["ok"])
        self.assertTrue(result["stages"][0]["outcome_unknown"])
        self.assertEqual(p.calls[-1][0],41)
        self.assertEqual(sum(op==41 and struct.unpack_from("<I",data)[0]==53 for op,data in p.calls),1)

    def test_jsonl_paths_resolve_relative_to_plan_without_host_cwd_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            plan=Path(directory)/"plan.jsonl"
            plan.write_text('{"action":"deploy","files":[{"source":"app.bda","target":"A:/app.bda"}]}\n{"action":"input","kind":"tap","target":"0x9000","code":"0x1c"}\n',encoding="utf-8")
            steps=workflow.read_steps(plan)
            expected_source=(Path(directory)/"app.bda").resolve()
        self.assertEqual(Path(steps[0]["files"][0]["source"]),expected_source)
        self.assertEqual(steps[1]["target"],0x9000)

    def test_session_cli_reports_a_failed_plan_with_nonzero_exit(self):
        c,_=self.connection()
        with tempfile.TemporaryDirectory() as directory:
            plan=Path(directory)/"plan.json";plan.write_text('[{"action":"unknown"}]',encoding="utf-8")
            with patch.object(client.Client,"connect",return_value=c),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(client.main(["session",str(plan)]),1)

    def test_running_image_compares_executable_bytes_and_foreground_handler(self):
        header=bytearray(52);header[:6]=b"\x7fELF\x01\x01"
        struct.pack_into("<H",header,18,8);struct.pack_into("<I",header,32,52)
        struct.pack_into("<HH",header,46,40,2)
        code=b"\x24\x10\x00\x01"*4
        section=struct.pack("<10I",0,1,6,0x81c00020,132,len(code),0,0,4,0)
        with tempfile.TemporaryDirectory() as directory:
            elf=Path(directory)/"app.elf";elf.write_bytes(header+bytes(40)+section+code)
            c,_=self.connection();c.read=lambda address,size:code
            result=c.verify_running_image(elf)
            self.assertTrue(result["verified"])
            self.assertEqual(result["sections"][0]["bytes"],len(code))
            c.read=lambda address,size:b"\0"*size
            with self.assertRaises(workflow.ManagementError):c.verify_running_image(elf)
            c.read=lambda address,size:code
            windows=c.windows();windows[0]["handler"]=0x80012340
            with self.assertRaises(workflow.ManagementError):c.verify_running_image(elf,windows)

    def test_dynamic_target_never_chooses_an_unverified_foreground(self):
        c,p=self.connection()
        with self.assertRaises(workflow.ManagementError):c.input_event("foreground","tap",code=0x1c)
        with self.assertRaises(workflow.ManagementError):c.apps_refresh("desktop")
        self.assertFalse(any(op==41 and struct.unpack_from("<I",data)[0] in (51,53) for op,data in p.calls))

    def test_verified_launch_resolves_live_handle_then_rejects_owner_change(self):
        header=bytearray(52);header[:6]=b"\x7fELF\x01\x01"
        struct.pack_into("<H",header,18,8);struct.pack_into("<I",header,32,52)
        struct.pack_into("<HH",header,46,40,2)
        code=b"\x24\x10\x00\x01"*4
        section=struct.pack("<10I",0,1,6,0x81c00020,132,len(code),0,0,4,0)
        c,p=self.connection();p.files["A:/app.bda"]=b"application"
        p.active_handler,p.active_handle=p.desktop_handler,0x8800
        with tempfile.TemporaryDirectory() as directory:
            elf=Path(directory)/"app.elf";elf.write_bytes(header+bytes(40)+section+code)
            c.read=lambda address,size:code
            launch=c.apps_launch("desktop","A:/app.bda",image_elf=elf)
            self.assertTrue(launch["loaded_version_verified"])
            self.assertEqual(launch["resolved_target"]["handle"],0x8800)
            result=c.input_event("$launched","tap",code=0x1c)
            self.assertEqual(result["target"],0x9000)
            p.queue_owner=4
            with self.assertRaises(workflow.ManagementError):c.input_event("$launched","tap",code=0x1c)
        self.assertEqual(sum(op==41 and struct.unpack_from("<I",data)[0]==53 for op,data in p.calls),1)

    def test_frame_reuse_between_preflight_and_post_is_rejected_without_retry(self):
        c,p=self.connection();original=p.respond
        def respond(op,data):
            if op==41 and struct.unpack_from("<I",data)[0]==53:
                p.queue_owner+=1
            return original(op,data)
        p.respond=respond
        with self.assertRaises(client.DeviceError) as failure:c.input_event(0x9000,"tap",code=0x1c)
        self.assertEqual(failure.exception.status,6)
        self.assertEqual(p.injected,[])
        self.assertEqual(sum(op==41 and struct.unpack_from("<I",data)[0]==53 for op,data in p.calls),1)
        self.assertFalse(c.broken)

    def test_serialized_rpc_calls_share_sequence_without_interleaving(self):
        c,p=self.connection();original=p.respond
        def respond(op,data):
            if op==3:
                time.sleep(.001)
                return 0,data
            return original(op,data)
        p.respond=respond;results=[];failures=[]
        def invoke(value):
            try:results.append(c.rpc(3,packed(value)))
            except Exception as exc:failures.append(exc)
        threads=[threading.Thread(target=invoke,args=(i,)) for i in range(8)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(failures,[])
        self.assertEqual(set(results),{packed(i) for i in range(8)})


if __name__ == "__main__":
    unittest.main()
