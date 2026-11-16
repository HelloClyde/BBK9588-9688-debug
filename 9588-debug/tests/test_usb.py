#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Run the built MIPS CDC driver against a USB controller/FIFO model.

The controller model is adapted from H1Debug tests/h1_debug_device.py.
It is self contained and uses the selected 9588 build's real JSON profile.
No serial port or physical device is accessed. Firmware timing/wakeup and
signature services are explicit MIPS stubs; this is not full firmware evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import unittest
from pathlib import Path

from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN
from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.mips_const import (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2,
    UC_MIPS_REG_A3, UC_MIPS_REG_V0, UC_MIPS_REG_RA, UC_MIPS_REG_SP,
    UC_MIPS_REG_PC, UC_MIPS_REG_CP0_STATUS)

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(os.environ.get("BBK9588_BUILD_DIR", str(ROOT / "build/9588-debug")))


def integer(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def relocate(raw, records, old_base, base):
    image = bytearray(raw)
    for offset, kind, value in records:
        word = struct.unpack_from("<I", image, offset)[0]
        value = (value + base - old_base) & 0xFFFFFFFF
        if kind == 1:
            word = value
        elif kind == 2:
            word = (word & 0xFC000000) | ((value >> 2) & 0x3FFFFFF)
        elif kind == 3:
            word = (word & 0xFFFF0000) | (((value + 0x8000) >> 16) & 0xFFFF)
        elif kind == 4:
            word = (word & 0xFFFF0000) | (value & 0xFFFF)
        else:
            raise ValueError(f"unsupported relocation: {kind}")
        struct.pack_into("<I", image, offset, word)
    return bytes(image)


class USBModel:
    """Actual relocated resident image, with explicit firmware/MMIO models."""
    def __init__(self, build=BUILD, base=0x80807FE0, *, start=True):
        self.build = Path(build)
        relocation = json.loads((self.build / "relocations.json").read_text(encoding="utf-8"))
        self.report = json.loads((self.build / "build.json").read_text(encoding="utf-8"))
        # Read exactly the generated header used for this binary, not a
        # default profile that might since have changed.
        import re
        self.macros = {name: int(value, 16) for name, value in re.findall(
            r"^#define\s+(FW_[A-Z0-9_]+)\s+0x([0-9a-fA-F]+)u",
            (self.build / "profile_generated.h").read_text(encoding="ascii"), re.M)}
        self.base, self.size = base, relocation["memory_size"]
        delta = base - relocation["base"]
        self.symbols = {name: value + delta for name, value in relocation["symbols"].items() if value}
        self.u = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        self.u.mem_map(0, self.macros["FW_SDRAM_END"] & 0x1FFFFFFF)
        self.u.mem_map(0x10000000, 0x20000)
        self.u.mem_map(self.macros["FW_UDC_BASE"] & 0x1FFFFFFF, 4096)
        self.u.mem_map(self.macros["FW_LCD_BASE"] & 0x1FFFFFFF, 4096)
        self.u.reg_write(UC_MIPS_REG_CP0_STATUS, 0x10000000)
        self.put(base, relocate((self.build / "resident.bin").read_bytes(),
                 relocation["records"], relocation["base"], base))
        self.put(base + 8, struct.pack("<III", base, base, self.size))
        self.tick_address = self.macros["FW_RUNTIME_BASE"] - 0x10000
        self.put(self.tick_address, bytes(4))
        self._install_firmware_stubs()
        self.cable = False
        self.index = self.events = 0
        self.imr, self.gpio_mask, self.gpio_flags = 0x100, 0x100, 0
        self.ui = {0: 0, 1: 0x60, 6: 3, 8: 2, 0xB: 5, 0xE: 0}
        self.ep = {i: {0x10: 64, 0x14: 64 if i == 1 else 0,
                      0x12: 0, 0x16: 0, 0x18: 0} for i in range(3)}
        self.rx = {i: bytearray() for i in range(3)}
        self.tx = {i: bytearray() for i in range(3)}
        self.device_in_toggle = {1: 0, 2: 0}
        self.device_out_toggle = {1: 0, 2: 0}
        self.bulk_csr_writes = []
        self.gated_udc_reads = self.gated_udc_writes = 0
        self.packet_sizes = []
        self.pending, self.available, self.held = bytearray(), bytearray(), bytearray()
        self.closed = False
        self.u.hook_add(UC_HOOK_MEM_READ, self.mmio_read, begin=0x10000000, end=0x1001FFFF)
        self.u.hook_add(UC_HOOK_MEM_WRITE, self.mmio_write, begin=0x10000000, end=0x1001FFFF)
        udc = self.macros["FW_UDC_BASE"] & 0x1FFFFFFF
        self.udc_read_hook = self.u.hook_add(UC_HOOK_MEM_READ, self.usb_read, begin=udc, end=udc + 4095)
        self.udc_write_hook = self.u.hook_add(UC_HOOK_MEM_WRITE, self.usb_write, begin=udc, end=udc + 4095)
        if start:
            result = self.call("resident_init", base)
            if result:
                raise RuntimeError(f"modeled resident init failed: {result}")
            self.put(base + 24, struct.pack("<I", 1))
            self.bus_reset()
            self.control(0, 9, 1)
            self.finish_control()
            self.pump()  # Let the transport observe the new USB session.

    def _install_firmware_stubs(self):
        # Execute real MIPS stubs instead of Python PC jumps from code hooks;
        # this preserves branch delay slots and exercises the normal call ABI.
        self.stub_return("fw_matches", 1)
        self.stub_return("fw_wake_install", 0)
        self.stub_return("fw_wake_remove", 0)
        high, low = ((self.tick_address + 0x8000) >> 16) & 0xFFFF, self.tick_address & 0xFFFF
        self.put(self.symbols["fw_tick"], struct.pack("<3I", 0x3C020000 | high,
                 0x03E00008, 0x8C420000 | low))
        self.put(self.symbols["fw_delay"], struct.pack("<6I", 0x3C020000 | high,
                 0x8C430000 | low, 0x00641821, 0x03E00008, 0xAC430000 | low, 0))

    def stub_return(self, name, value):
        self.put(self.symbols[name], struct.pack("<2I", 0x03E00008, 0x24020000 | (value & 0xFFFF)))

    @property
    def ticks(self):
        return self.word(self.tick_address)

    @ticks.setter
    def ticks(self, value):
        self.put(self.tick_address, struct.pack("<I", value & 0xFFFFFFFF))

    def put(self, address, data):
        self.u.mem_write(address & 0x1FFFFFFF, bytes(data))

    def get(self, address, count):
        return bytes(self.u.mem_read(address & 0x1FFFFFFF, count))

    def word(self, address):
        return struct.unpack("<I", self.get(address, 4))[0]

    def call(self, name, *args):
        stack = self.macros["FW_RUNTIME_BASE"] - 0x20000
        ret = stack + 0x1000
        self.u.reg_write(UC_MIPS_REG_SP, stack)
        self.u.reg_write(UC_MIPS_REG_RA, ret)
        for register, value in zip((UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3), args):
            self.u.reg_write(register, value)
        if len(args) > 4:
            self.put(stack + 16, struct.pack("<" + "I" * (len(args) - 4), *args[4:]))
        self.u.emu_start(self.symbols[name], ret, count=400_000_000)
        if self.u.reg_read(UC_MIPS_REG_PC) != ret:
            raise RuntimeError(f"MIPS call did not return: {name}")
        return self.u.reg_read(UC_MIPS_REG_V0)

    def _physical(self, name):
        return self.macros[name] & 0x1FFFFFFF

    def mmio_read(self, uc, access, address, size, value, _):
        gpio = self._physical("FW_USB_CABLE_GPIO_BASE")
        values = {self._physical("FW_INTC_IMR"): self.imr,
                  gpio: self.macros["FW_USB_CABLE_GPIO_MASK"] if self.cable != bool(self.macros.get("FW_USB_CABLE_ACTIVE_LOW", 0)) else 0,
                  gpio + 0x20: self.gpio_mask, gpio + 0x80: self.gpio_flags}
        if address in values:
            self.put(address, values[address].to_bytes(size, "little"))

    def mmio_write(self, uc, access, address, size, value, _):
        gpio = self._physical("FW_USB_CABLE_GPIO_BASE")
        if address == self._physical("FW_INTC_IMSR"):
            self.imr |= value
        elif address == self._physical("FW_INTC_IMCR"):
            self.imr &= ~value
        elif address == gpio + 0x24:
            self.gpio_mask |= value
        elif address == gpio + 0x28:
            self.gpio_mask &= ~value
        elif address == gpio + 0x14:
            self.gpio_flags &= ~value

    def usb_read(self, uc, access, address, size, value, _):
        if self.word(self.macros["FW_CPM_CLKGR"]) & self.macros["FW_CPM_USB_GATE_MASK"]:
            self.gated_udc_reads += 1
        offset = address - self._physical("FW_UDC_BASE")
        if offset in (0x20, 0x24, 0x28):
            endpoint = (offset - 0x20) // 4
            value = self.rx[endpoint].pop(0) if self.rx[endpoint] else 0
        elif offset == 0xA:
            value, self.events = self.events, 0
        elif 0x10 <= offset <= 0x18:
            value = self.ep[self.index].get(offset, 0)
        else:
            value = self.ui.get(offset, 0)
        self.put(address, value.to_bytes(size, "little"))

    def usb_write(self, uc, access, address, size, value, _):
        if self.word(self.macros["FW_CPM_CLKGR"]) & self.macros["FW_CPM_USB_GATE_MASK"]:
            self.gated_udc_writes += 1
        offset = address - self._physical("FW_UDC_BASE")
        if offset in (0x20, 0x24, 0x28):
            self.tx[(offset - 0x20) // 4].append(value & 255)
        elif offset == 0xE:
            self.index = value & 15
            self.ui[offset] = value
        elif 0x10 <= offset <= 0x18:
            endpoint = self.ep[self.index]
            if offset == 0x12 and self.index == 0:
                if value & 0x40:
                    endpoint[offset] &= ~1
                    self.rx[0].clear()
                    endpoint[0x18] = 0
                if value & 0x80:
                    endpoint[offset] &= ~0x10
                if value & 2:
                    endpoint[offset] |= 2
                if value == 0:
                    endpoint[offset] = 0
            elif offset == 0x12:
                self.bulk_csr_writes.append((self.index, value))
                if value & 8:
                    assert endpoint[offset] & 1, "TX flush without ready packet"
                    self.tx[self.index].clear()
                if value & 0x40:
                    self.device_in_toggle[self.index] = 0
                endpoint[offset] = value & 1
            elif offset == 0x16:
                if value & 0x10:
                    assert endpoint[offset] & 1, "RX flush without ready packet"
                    self.rx[self.index].clear()
                    endpoint[0x18] = 0
                if value & 0x80:
                    self.device_out_toggle[self.index] = 0
                endpoint[offset] = value & 1
            else:
                endpoint[offset] = value
        else:
            self.ui[offset] = value

    def poll_usb(self):
        self.ticks += 1
        self.call("debug_usb_poll", self.symbols["usb"])

    def bus_reset(self):
        self.events = 4
        self.ui[6], self.ui[8] = 7, 2
        self.poll_usb()

    def control(self, request_type, request, value=0, index=0, length=0):
        assert not self.rx[0] and not self.ep[0][0x12] & 1
        self.rx[0] = bytearray(struct.pack("<BBHHH", request_type, request, value, index, length))
        self.ep[0][0x18], self.ep[0][0x12] = 8, 1
        self.poll_usb()

    def finish_control(self):
        result = bytes(self.tx[0])
        self.tx[0].clear()
        self.ep[0][0x12] &= ~2
        return result

    def pump(self):
        self.ticks += 1
        if self.pending and not self.ep[1][0x16] & 1:
            fragment = getattr(self, "fragment_bytes", 17)
            block = self.pending[:fragment]  # Deliberately fragment headers.
            del self.pending[:fragment]
            self.rx[1] = bytearray(block)
            self.ep[1][0x18], self.ep[1][0x16] = len(block), 1
        name = "transport_poll" if "transport_poll" in self.symbols else "transport_poll.part.0"
        self.call(name)
        if self.ep[1][0x12] & 1:
            block = bytes(self.tx[1])
            self.packet_sizes.append(len(block))
            self.tx[1].clear()
            self.ep[1][0x12] = 0
            self.held.extend(block)
            if len(block) < 64:
                self.available.extend(self.held)
                self.held.clear()
        # Same execution gate as resident_worker: the RUN reply must drain
        # before user code begins. Cooperative module polling stays in MIPS.
        if (self.word(self.symbols["exec_state"]) == 3 and
                not self.word(self.symbols["protocol"] + 1060) and
                not self.ep[1][0x12] & 1):
            self.call("rd_exec_invoke", self.symbols["module_api"])

    def reset_input_buffer(self):
        self.available.clear()

    def write(self, data):
        assert not self.pending and not self.available and not self.held
        self.pending.extend(data)
        return len(data)

    def read(self, count):
        for _ in range(2000):
            if self.available:
                break
            self.pump()
        data = bytes(self.available[:count])
        del self.available[:count]
        return data

    def close(self):
        self.closed = True


class USBTests(unittest.TestCase):
    def test_rejects_firmware_before_register_changes(self):
        model = USBModel(start=False)
        model.stub_return("fw_matches", 0)
        self.assertEqual(model.call("debug_usb_open", model.symbols["usb"]), 1)
        self.assertEqual((model.imr, model.gpio_mask), (0x100, 0x100))

    def test_rejects_connected_cable(self):
        model = USBModel(start=False)
        model.cable = True
        self.assertEqual(model.call("debug_usb_open", model.symbols["usb"]), 2)
        self.assertEqual((model.imr, model.gpio_mask), (0x100, 0x100))

    def test_rejects_enumerated_native_usb_with_power_input_low(self):
        model = USBModel(start=False)
        model.ui[0] = 7
        self.assertEqual(model.call("debug_usb_open", model.symbols["usb"]), 2)
        self.assertEqual((model.imr, model.gpio_mask), (0x100, 0x100))

    def test_gated_clock_skips_stale_address_until_clock_is_enabled(self):
        model = USBModel(start=False)
        model.ui[0] = 7
        model.put(model.macros["FW_CPM_CLKGR"], struct.pack("<I", model.macros["FW_CPM_USB_GATE_MASK"]))
        self.assertEqual(model.call("debug_usb_open", model.symbols["usb"]), 0)
        self.assertEqual(model.gated_udc_reads, 0)


    def test_checked_cable_polarity_controls_open_rejection(self):
        model=USBModel(start=False)
        for connected in (False,True):
            model.cable=connected
            self.assertEqual(model.call("debug_usb_cable_connected"),int(connected))
        self.assertIn(model.macros["FW_USB_CABLE_ACTIVE_LOW"],(0,1))

    def test_descriptor_identity(self):
        model = USBModel()
        model.control(0x80, 6, 0x100, length=18)
        descriptor = model.finish_control()
        self.assertEqual(len(descriptor), 18)
        self.assertEqual(descriptor[8:12], bytes.fromhex("c4 07") + struct.pack("<H", {"JZ4740":0xa4a9,"JZ4720":0xa4ab}[model.report["soc"]]))
        soc=model.report["soc"]
        for index, expected in ((2, "9588 USB Debug" if soc=="JZ4740" else "9588 USB Debug JZ4720"), (3, "9588DBG-"+soc)):
            model.control(0x80, 6, 0x300 + index, length=128)
            self.assertEqual(model.finish_control()[2:].decode("utf-16le"), expected)

    def test_dtr_epoch_preserves_bulk_data_toggles(self):
        model = USBModel()
        model.device_in_toggle[1] = model.device_out_toggle[1] = 1
        model.control(0x21, 0x22, 1)
        model.finish_control()
        model.control(0x21, 0x22, 0)
        model.finish_control()
        self.assertEqual((model.device_in_toggle[1], model.device_out_toggle[1]), (1, 1))
        self.assertEqual(model.word(model.symbols["usb"] + 248), 2)

    def test_reset_reinitializes_without_ownership_fault(self):
        model = USBModel()
        model.bus_reset()
        self.assertEqual((model.ui[6], model.ui[8]), (0, 0))
        self.assertEqual(model.word(model.symbols["usb"] + 216), 0)
        self.assertEqual(model.word(model.symbols["usb"] + 196), 2)

    def test_bulk_io_and_zero_packet(self):
        model = USBModel()
        scratch = model.macros["FW_RUNTIME_BASE"] + 0x8000
        model.put(scratch, b"abc")
        self.assertEqual(model.call("debug_usb_write", model.symbols["usb"], scratch, 3), 1)
        self.assertEqual(model.tx[1], b"abc")
        self.assertEqual(model.call("debug_usb_write", model.symbols["usb"], scratch, 3), 0)
        model.tx[1].clear()
        model.ep[1][0x12] = 0
        self.assertEqual(model.call("debug_usb_write", model.symbols["usb"], scratch, 0), 1)
        model.rx[1] = bytearray(b"xyz")
        model.ep[1][0x18], model.ep[1][0x16] = 3, 1
        self.assertEqual(model.call("debug_usb_read", model.symbols["usb"], scratch, 2), 0xFFFFFFFF)
        self.assertEqual(model.call("debug_usb_read", model.symbols["usb"], scratch, 64), 3)
        self.assertEqual(model.get(scratch, 3), b"xyz")

    def test_ownership_loss_retains_first_diagnostic(self):
        model = USBModel()
        model.gpio_mask &= ~model.macros["FW_USB_CABLE_GPIO_MASK"]
        model.poll_usb()
        self.assertEqual(model.word(model.symbols["usb"] + 216), 1)
        scratch = model.macros["FW_RUNTIME_BASE"] + 0x8000
        model.call("debug_usb_diagnostics", model.symbols["usb"], scratch)
        words = struct.unpack("<56I", model.get(scratch, 224))
        self.assertEqual((words[0], words[1], words[3], words[4]), (1, 56, 1, 1))
        self.assertTrue(words[33] & 16)  # First fault's cable IRQ mask bit.

    def test_close_restores_original_registers_and_other_irq_bits(self):
        model = USBModel()
        self.assertEqual(model.imr, 0x100 | model.macros["FW_USB_IRQ_MASK"])
        self.assertEqual(model.gpio_mask, 0x100 | model.macros["FW_USB_CABLE_GPIO_MASK"])
        model.call("debug_usb_close", model.symbols["usb"])
        self.assertEqual((model.imr, model.gpio_mask), (0x100, 0x100))
        self.assertEqual((model.ui[6], model.ui[8], model.ui[0xB]), (3, 2, 5))
        self.assertEqual(model.get(model.symbols["usb"] + 52, 2), b"\0\0")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--build-dir", type=Path)
    options, remaining = parser.parse_known_args()
    if options.build_dir:
        BUILD = options.build_dir.resolve()
        USBModel.__init__.__defaults__ = (BUILD, 0x80807FE0)
    unittest.main(argv=[__file__, *remaining])
