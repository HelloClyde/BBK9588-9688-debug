#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Integration regressions for real resident RPC, ROI, CDC sessions and logs."""
from __future__ import annotations
import struct
import unittest
from test_device import USBModel, dispatch, native_screen

class WorkflowDeviceTests(unittest.TestCase):
    def test_capabilities_advertise_management_discovery_roi_and_events(self):
        model=USBModel()
        status,data=dispatch(model,9)
        self.assertEqual(status,0)
        caps=struct.unpack("<8I",data)
        self.assertEqual(caps[1]&0x1e0,0x1e0)
        self.assertEqual(dispatch(model,40)[0],0)
        self.assertEqual(dispatch(model,47)[0],0)
        self.assertEqual(dispatch(model,47,b"x"),(1,b""))

    def test_roi_copies_only_requested_rows_in_both_pixel_formats(self):
        for bits in (16,32):
            with self.subTest(bits=bits):
                model=USBModel();desc,source,pixels=native_screen(model,bits)
                status,data=dispatch(model,6,struct.pack("<4I",3,5,7,9))
                self.assertEqual(status,0)
                token,w,h,stride,fmt,size,address,tick=struct.unpack("<8I",data)
                bpp=bits//8
                self.assertEqual((w,h,stride,fmt,size,address),(7,9,7*bpp,1 if bits==16 else 2,7*9*bpp,source|0xa0000000))
                status,chunk=dispatch(model,7,struct.pack("<3I",token,0,500))
                self.assertEqual(status,0)
                width=240
                expected=b"".join(pixels[((y*width+3)*bpp):((y*width+10)*bpp)] for y in range(5,14))
                self.assertEqual(chunk[12:],expected)
                self.assertEqual(dispatch(model,8,struct.pack("<I",token)),(0,b""))

    def test_roi_rejects_overflow_empty_and_out_of_screen(self):
        model=USBModel();native_screen(model)
        for rect in ((0,0,0,1),(0,0,1,0),(240,0,1,1),(0,320,1,1),(239,0,2,1),(0,319,1,2),(0xffffffff,0,2,1),(0,0,0xffffffff,1)):
            with self.subTest(rect=rect):
                self.assertEqual(dispatch(model,6,struct.pack("<4I",*rect)),(1,b""))
        for invalid in (b"x",bytes(4),bytes(12),bytes(20)):
            self.assertEqual(dispatch(model,6,invalid),(1,b""))

    def test_dtr_reopen_does_not_cancel_pending_or_running_exec(self):
        for state in (3,4):
            with self.subTest(state=state):
                model=USBModel()
                model.put(model.symbols["exec_state"],struct.pack("<I",state))
                before=struct.unpack("<9I",dispatch(model,31)[1])
                for lines in (1,0,1):
                    model.control(0x21,0x22,lines);model.finish_control()
                    model.call("transport_poll" if "transport_poll" in model.symbols else "transport_poll.part.0")
                after=struct.unpack("<9I",dispatch(model,31)[1])
                self.assertEqual(after[:4],before[:4])
                self.assertEqual(dispatch(model,40)[0],0)
                self.assertEqual(dispatch(model,41,struct.pack("<I",1)),(2,b""))
                self.assertEqual(dispatch(model,47)[0],0)
                self.assertEqual(dispatch(model,48)[0],0)

    def test_usb_bus_reset_still_cancels_exec(self):
        model=USBModel()
        model.put(model.symbols["exec_state"],struct.pack("<I",4))
        model.bus_reset();model.pump()
        state=struct.unpack("<9I",dispatch(model,31)[1])
        self.assertEqual(state[1],4)
        self.assertEqual(state[3]&4,4)

    def test_event_log_is_bounded_readonly_and_recoverable_across_dtr(self):
        model=USBModel()
        for _ in range(80):self.assertEqual(dispatch(model,127),(1,b""))
        status,first=dispatch(model,48)
        self.assertEqual(status,0)
        version,cursor,oldest,count=struct.unpack_from("<4I",first)
        self.assertEqual((version,count),(1,31))
        self.assertGreater(oldest,1)
        rows=list(struct.iter_unpack("<4I",first[16:]))
        self.assertEqual([r[0] for r in rows],list(range(oldest,oldest+31)))
        self.assertTrue(all(r[2:]==(2,127<<24|1<<16) for r in rows))
        self.assertEqual(dispatch(model,48)[1],first)
        status,second=dispatch(model,48,struct.pack("<I",cursor))
        self.assertEqual(status,0)
        self.assertEqual(struct.unpack_from("<4I",second)[3],31)
        last=struct.unpack_from("<I",second,4)[0]
        status,third=dispatch(model,48,struct.pack("<I",last))
        self.assertEqual(status,0)
        self.assertEqual(struct.unpack_from("<4I",third)[3],2)
        last=struct.unpack_from("<I",third,4)[0]
        model.control(0x21,0x22,1);model.finish_control();model.pump()
        model.control(0x21,0x22,0);model.finish_control();model.pump()
        _,after=dispatch(model,48,struct.pack("<I",last))
        self.assertTrue(any(r[2]==3 for r in struct.iter_unpack("<4I",after[16:])))

if __name__=="__main__":unittest.main()
