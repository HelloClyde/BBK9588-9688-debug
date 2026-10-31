#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Real JZ4730 MIPS UDC code with the dedicated 32-bit/FIFO controller model.

The model follows JZ4730 PM chapter21, including hardware standard requests,
shared RX packet FIFO, per-IN FIFO RAM, TCFM/TZERO and 32-byte EP0. It does not
simulate analog USB timing or prove hardware ACK/error recovery behavior.
"""
from __future__ import annotations

import argparse
import os
import struct
import unittest
from pathlib import Path

from test_usb import USBModel as BaseModel

BUILD = Path(os.environ.get("BBK9688_BUILD_DIR", str(Path(__file__).resolve().parents[2] / "build/9688-debug-jz4730")))


class USBModel(BaseModel):
    def __init__(self, build=None, base=0x80807FE0, *, start=True):
        super().__init__(Path(build) if build is not None else BUILD, base, start=False)
        if "jz4730" not in self.report["profile"].lower():
            raise ValueError("JZ4730 model requires the JZ4730 build")
        self.u.mem_map(self._physical("FW_USB_SBA_CTRL") & ~0xFFF, 4096)
        # Unicorn2.1.4 duplicates a callee's first instruction when an MMIO
        # store in a JAL delay slot hits UC_HOOK_MEM_WRITE. Genuine MMIO
        # callbacks avoid that engine bug without changing the MIPS image.
        udc = self._physical("FW_UDC_BASE")
        self.u.hook_del(self.udc_read_hook)
        self.u.hook_del(self.udc_write_hook)
        self.u.mem_unmap(udc, 4096)
        self.u.mmio_map(udc, 4096,
            lambda uc, offset, size, data: self.usb_read(uc, 0, udc + offset, size, 0, data), None,
            lambda uc, offset, size, value, data: self.usb_write(uc, 0, udc + offset, size, value, data), None)
        self.regs = {offset: 0 for offset in range(0, 0x524, 4)}
        # Non-default native configurations make restoration a real check.
        self.regs.update({0x400: 0x17, 0x404: 0, 0x410: 0x30, 0x418: 0,
                          0x500: 0xFFFF, 0x008: 8, 0x00C: 32, 0x028: 16,
                          0x02C: 64, 0x048: 16, 0x04C: 64,
                          0x020: 0x30, 0x040: 0x20, 0x2A0: 0x20, 0x2AC: 64})
        self.native_regs = dict(self.regs)
        self.shared_rx = bytearray()
        self.rx_endpoint = None
        self.in_build = {ep: bytearray() for ep in range(5)}
        self.in_packet = {ep: None for ep in range(5)}
        self.in_endpoint = None
        self.zero_armed = False
        self.zero_out_acknowledgements = 0
        self.transmitted = {ep: [] for ep in range(5)}
        self.hardware_reply = b""
        self.hardware_address = 0
        self.frame_number = 0
        self.inject_in_during_fill = False
        self.inject_in_on_confirm = False
        self.mmio_trace = []
        if start:
            if self.call("resident_init", base):
                raise RuntimeError("JZ4730 modeled initialization failed")
            self.put(base + 24, struct.pack("<I", 1))
            self.bus_reset()
            self.control(0, 9, 1)
            self.finish_control()
            self.pump()

    def mmio_read(self, uc, access, address, size, value, _):
        gpio = self._physical("FW_USB_CABLE_GPIO_BASE")
        values = {self._physical("FW_INTC_IMR"): self.imr,
                  gpio: self.macros["FW_USB_CABLE_GPIO_MASK"] if self.cable else 0,
                  gpio + 0x24: self.gpio_mask, gpio + 0x28: self.gpio_flags}
        if address in values:
            self.put(address, values[address].to_bytes(size, "little"))

    def mmio_write(self, uc, access, address, size, value, _):
        gpio = self._physical("FW_USB_CABLE_GPIO_BASE")
        if address == self._physical("FW_INTC_IMSR"):
            self.imr |= value
        elif address == self._physical("FW_INTC_IMCR"):
            self.imr &= ~value
        elif address == gpio + 0x24:
            self.gpio_mask = value
        elif address == gpio + 0x28:
            self.gpio_flags &= ~value

    def fifo_endpoint(self, offset):
        start = 0x840
        for ep in range(5):
            words = self.regs[0x20 * ep + 8]
            if words and offset == start:
                return ep
            start += words * 4
        return None

    def usb_read(self, uc, access, address, size, value, _):
        offset = address - self._physical("FW_UDC_BASE")
        if self.word(self.macros["FW_CPM_CLKGR"]) & self.macros["FW_CPM_USB_GATE_MASK"]:
            self.gated_udc_reads += 1
        self.mmio_trace.append(("r", offset, size))
        if offset == 0x800:
            if len(self.shared_rx) < size:
                raise AssertionError("read beyond the shared OUT packet FIFO")
            data = bytes(self.shared_rx[:size])
            del self.shared_rx[:size]
            value = int.from_bytes(data, "little")
        elif offset == 0x41C:
            self.zero_out_acknowledgements += 1
            value = 0
        elif offset == 0x408:
            value = self.regs[offset] | ((self.frame_number & 0x3FFF) << 18)
        else:
            value = self.regs.get(offset, 0)
        return value

    def usb_write(self, uc, access, address, size, value, _):
        offset = address - self._physical("FW_UDC_BASE")
        self.mmio_trace.append(("w", offset, size))
        endpoint = self.fifo_endpoint(offset)
        if endpoint is not None:
            if self.in_packet[endpoint] is not None:
                raise AssertionError("write overwrote an unconsumed IN FIFO")
            self.in_endpoint = endpoint
            self.in_build[endpoint].extend(value.to_bytes(size, "little"))
            if self.inject_in_during_fill:
                self.regs[0x20 * endpoint + 4] |= 0x40
                self.regs[0x414] |= 1 << endpoint
        elif offset == 0x420:
            self.zero_armed = True
        elif offset == 0x41C:
            ep = self.in_endpoint
            if ep is None:
                raise AssertionError("TCFM without selecting an endpoint FIFO")
            data = bytes(self.in_build[ep])
            self.in_build[ep].clear()
            if self.zero_armed:
                data = b""
            self.zero_armed = False
            if len(data) > self.regs[ep * 0x20 + 12]:
                raise AssertionError("packet exceeds the endpoint max packet size")
            self.in_packet[ep] = data
            if self.inject_in_on_confirm:
                self.regs[ep * 0x20 + 4] |= 0x40
                self.regs[0x414] |= 1 << ep
        elif offset in (0x40C, 0x414):
            self.regs[offset] &= ~value  # W1C pending registers.
        elif offset in (0, 0x20, 0x40, 0x60, 0x80):
            ep = offset // 0x20
            if value & 2:
                self.in_build[ep].clear()
                self.in_packet[ep] = None
            self.regs[offset] = value
        elif offset in (4, 0x24, 0x44, 0x64, 0x84):
            self.regs[offset] = value
        elif offset in (0x204, 0x2A4, 0x2C4, 0x2E4):
            self.regs[offset] = value
            if not value & 0x30:
                if self.shared_rx:
                    raise AssertionError("OUT acknowledged before reading its full payload")
                self.rx_endpoint = None
        else:
            self.regs[offset] = value

    def enqueue_out(self, ep, data, *, setup=False):
        if self.rx_endpoint is not None:
            raise AssertionError("shared RX FIFO has an unconsumed packet")
        if len(data) > self.regs[0x200 + 0x20 * ep + 12]:
            raise AssertionError("OUT packet exceeds max packet size")
        self.shared_rx = bytearray(data)
        self.rx_endpoint = ep
        self.regs[0x204 + 0x20 * ep] = (len(data) << 11) | (0x20 if setup else 0x10)
        self.regs[0x414] |= 1 << (16 + ep)

    def host_in(self, ep):
        data = self.in_packet[ep]
        self.in_packet[ep] = None
        self.regs[ep * 0x20 + 4] |= 0x40
        self.regs[0x414] |= 1 << ep
        if data is not None:
            self.transmitted[ep].append(data)
        return data

    def poll_usb(self):
        self.ticks += 1
        self.frame_number += 1
        self.call("debug_usb_poll", self.symbols["usb"])

    def bus_reset(self):
        self.regs[0x408] = 0
        self.regs[0x40C] |= 8
        self.hardware_address = 0
        self.device_in_toggle = {1: 0, 2: 0}
        self.device_out_toggle = {5: 0}
        self.shared_rx.clear()
        self.rx_endpoint = None
        self.poll_usb()

    def control(self, request_type, request, value=0, index=0, length=0):
        self.hardware_reply = b""
        if request_type == 0 and request == 5:
            self.hardware_address = value
            return
        if request_type == 0 and request == 9:
            self.regs[0x408] = (self.regs[0x408] & ~15) | value
            self.regs[0x40C] |= 1
            self.device_in_toggle = {1: 0, 2: 0}
            self.device_out_toggle = {5: 0}
            self.poll_usb()
            return
        if request_type == 1 and request == 11:
            self.regs[0x408] = (self.regs[0x408] & ~0xFF0) | (index << 4) | (value << 8)
            self.regs[0x40C] |= 2
            self.poll_usb()
            return
        if request_type == 0x80 and request == 8:
            self.hardware_reply = bytes([self.regs[0x408] & 15])
            return
        if request_type == 0x81 and request == 10:
            self.hardware_reply = bytes([(self.regs[0x408] >> 8) & 15])
            return
        if request_type in (0x80, 0x81, 0x82) and request == 0:
            if request_type == 0x82:
                control = (index & 15) * 0x20 + (0 if index & 0x80 else 0x200)
                self.hardware_reply = bytes([self.regs[control] & 1, 0])
            else:
                self.hardware_reply = b"\0\0"
            return
        if request_type == 2 and request in (1, 3) and value == 0:
            ep = index & 15
            control = ep * 0x20 + (0 if index & 0x80 else 0x200)
            self.regs[control] = (self.regs[control] & ~1) | int(request == 3)
            if request == 1:
                (self.device_in_toggle if index & 0x80 else self.device_out_toggle)[ep] = 0
            return
        self.enqueue_out(0, struct.pack("<BBHHH", request_type, request, value, index, length), setup=True)
        self.poll_usb()

    def finish_control(self):
        result = bytearray(self.hardware_reply)
        self.hardware_reply = b""
        for _ in range(16):
            if self.in_packet[0] is None:
                break
            result.extend(self.host_in(0))
            self.poll_usb()
        else:
            raise AssertionError("EP0 control response failed to terminate")
        return bytes(result)

    def pump(self):
        self.ticks += 1
        self.frame_number += 1
        data = self.host_in(2)
        if data is not None:
            self.packet_sizes.append(len(data))
            self.held.extend(data)
            if len(data) < 64:
                self.available.extend(self.held)
                self.held.clear()
        if self.pending and self.rx_endpoint is None:
            fragment = getattr(self, "fragment_bytes", 17)
            block = self.pending[:fragment]
            del self.pending[:fragment]
            self.enqueue_out(5, block)
        name = "transport_poll" if "transport_poll" in self.symbols else "transport_poll.part.0"
        self.call(name)
        if (self.word(self.symbols["exec_state"]) == 3 and
                not self.word(self.symbols["protocol"] + 1060) and self.in_packet[2] is None):
            self.call("rd_exec_invoke", self.symbols["module_api"])


class JZ4730USBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (BUILD / "resident.bin").is_file():
            raise unittest.SkipTest("build --chip jz4730 before testing its UDC")

    def test_actual_profile_and_endpoint_ram_layout(self):
        model = USBModel()
        self.assertEqual(model.macros["FW_USB_IRQ_MASK"], 1 << 12)
        self.assertEqual(model.macros["FW_USB_CABLE_GPIO_BASE"], 0xB0010060)
        self.assertEqual(model.macros["FW_USB_CABLE_GPIO_MASK"], 1 << 25)
        self.assertEqual([model.regs[ep * 0x20 + 8] for ep in range(5)], [8, 4, 16, 0, 0])
        self.assertLessEqual(sum(model.regs[ep * 0x20 + 8] for ep in range(5)), 64)
        self.assertEqual((model.regs[0x504], model.regs[0x50C], model.regs[0x518]),
                         (0x01000080, 0x020008D2, 0x020008C5))

    def test_descriptors_enumerate_32_byte_ep0_and_fixed_endpoint_directions(self):
        model = USBModel()
        model.control(0x80, 6, 0x100, length=18)
        device = model.finish_control()
        self.assertEqual((device[7], device[8:12]), (32, bytes.fromhex("c4 07 a8 a4")))
        model.control(0x80, 6, 0x200, length=255)
        configuration = model.finish_control()
        self.assertEqual(len(configuration), 67)
        self.assertEqual([len(p) for p in model.transmitted[0][-3:]], [32, 32, 3])
        endpoints = []
        offset = 0
        while offset < len(configuration):
            if configuration[offset + 1] == 5:
                endpoints.append(configuration[offset + 2])
            offset += configuration[offset]
        self.assertEqual(endpoints, [0x81, 0x05, 0x82])
        model.control(0x80, 6, 0x303, length=255)
        self.assertEqual(model.finish_control()[2:].decode("utf-16le"), "9688DBG-V232-4730")

    def test_hardware_standard_requests_do_not_enter_software_ep0(self):
        model = USBModel()
        before = model.word(model.symbols["usb"] + 200)
        model.control(0, 5, 7)
        self.assertEqual(model.hardware_address, 7)
        model.control(0x80, 8, length=1)
        self.assertEqual(model.finish_control(), b"\x01")
        model.control(1, 11, 0, index=1)
        model.control(0x81, 10, index=1, length=1)
        self.assertEqual(model.finish_control(), b"\0")
        model.control(2, 3, 0, index=0x82)
        model.control(0x82, 0, index=0x82, length=2)
        self.assertEqual(model.finish_control(), b"\x01\0")
        model.device_in_toggle[2] = 1
        model.control(2, 1, 0, index=0x82)
        self.assertEqual(model.device_in_toggle[2], 0)
        self.assertEqual(model.word(model.symbols["usb"] + 200), before)

    def test_cdc_line_coding_control_status_and_dtr_epoch(self):
        model = USBModel()
        model.control(0x21, 0x20, length=7)
        line = bytes.fromhex("00 4b 00 00 00 00 08")
        model.enqueue_out(0, line)
        model.poll_usb()
        self.assertEqual(model.finish_control(), b"")
        model.control(0xA1, 0x21, length=7)
        self.assertEqual(model.finish_control(), line)
        model.device_in_toggle[2] = model.device_out_toggle[5] = 1
        model.control(0x21, 0x22, 1)
        model.finish_control()
        model.control(0x21, 0x22, 0)
        model.finish_control()
        self.assertEqual((model.device_in_toggle[2], model.device_out_toggle[5]), (1, 1))
        self.assertGreaterEqual(model.word(model.symbols["usb"] + 248), 4)

    def test_shared_out_fifo_byte_tails_zero_and_capacity(self):
        model = USBModel()
        address = model.macros["FW_RUNTIME_BASE"] + 0x8000
        for size in (1, 3, 4, 7, 17, 63, 64):
            data = bytes(range(size))
            model.enqueue_out(5, data)
            model.poll_usb()
            self.assertEqual(model.call("debug_usb_read", model.symbols["usb"], address, size - 1), 0xFFFFFFFF)
            self.assertEqual(model.call("debug_usb_read", model.symbols["usb"], address, 64), size)
            self.assertEqual(model.get(address, size), data)
        model.enqueue_out(5, b"")
        model.poll_usb()
        self.assertGreater(model.zero_out_acknowledgements, 0)
        self.assertIsNone(model.rx_endpoint)

    def test_simultaneous_out_endpoint_flags_are_rejected_without_reading_fifo(self):
        model = USBModel()
        model.enqueue_out(5, b"bulk")
        model.regs[0x414] |= 1 << 16
        model.poll_usb()
        self.assertEqual(model.word(model.symbols["usb"] + 216), 1)
        self.assertEqual(model.word(model.symbols["usb"] + 220), 1024)
        self.assertEqual(model.shared_rx, b"bulk")

    def test_bulk_in_length_zero_and_no_overwrite_until_consumed(self):
        model = USBModel()
        address = model.macros["FW_RUNTIME_BASE"] + 0x8000
        for size in (0, 1, 3, 7, 63, 64):
            data = bytes(range(size))
            model.put(address, data)
            self.assertEqual(model.call("debug_usb_write", model.symbols["usb"], address, size), 1)
            self.assertEqual(model.call("debug_usb_write", model.symbols["usb"], address, size), 0)
            self.assertEqual(model.host_in(2), data)
            self.assertEqual(model.call("debug_usb_tx_idle", model.symbols["usb"]), 1)

    def test_in_tokens_before_and_during_unconfirmed_fill_are_not_completion(self):
        for phase in ("stale", "fill", "confirm"):
            with self.subTest(phase=phase):
                model = USBModel()
                address = model.macros["FW_RUNTIME_BASE"] + 0x8000
                model.put(address, b"abc")
                if phase == "stale":
                    model.host_in(2)
                model.inject_in_during_fill = phase == "fill"
                model.inject_in_on_confirm = phase == "confirm"
                self.assertEqual(model.call("debug_usb_write", model.symbols["usb"], address, 3), 1)
                self.assertEqual(model.call("debug_usb_tx_idle", model.symbols["usb"]), 0)
                self.assertEqual(model.in_packet[2], b"abc")
                self.assertEqual(model.host_in(2), b"abc")
                self.assertEqual(model.call("debug_usb_tx_idle", model.symbols["usb"]), 1)

    def test_reset_deconfiguration_and_stop_restore_native_configuration(self):
        model = USBModel()
        model.control(0, 9, 0)
        self.assertEqual(model.get(model.symbols["usb"] + 53, 1), b"\0")
        model.bus_reset()
        self.assertEqual(model.word(model.symbols["usb"] + 196), 2)
        model.call("debug_usb_close", model.symbols["usb"])
        for offset in (0x400, 0x404, 0x410, 0x418, 0x500, 8, 12, 0x28, 0x2C, 0x48, 0x4C):
            self.assertEqual(model.regs[offset], model.native_regs[offset])
        self.assertEqual((model.imr, model.gpio_mask), (0x100, 0x100))

    def test_power_configured_native_and_unknown_firmware_rejected(self):
        for condition, expected in (("power", 2), ("configured", 2), ("firmware", 1)):
            with self.subTest(condition=condition):
                model = USBModel(start=False)
                if condition == "power":
                    model.cable = True
                elif condition == "configured":
                    model.regs[0x408] = 1
                else:
                    model.stub_return("fw_matches", 0)
                self.assertEqual(model.call("debug_usb_open", model.symbols["usb"]), expected)
                self.assertEqual((model.imr, model.gpio_mask), (0x100, 0x100))

    def test_stale_configuration_is_not_read_while_clock_is_gated(self):
        model = USBModel(start=False)
        model.regs[0x408] = 1
        model.put(model.macros["FW_CPM_CLKGR"], struct.pack("<I", model.macros["FW_CPM_USB_GATE_MASK"]))
        self.assertEqual(model.call("debug_usb_open", model.symbols["usb"]), 0)
        self.assertEqual(model.gated_udc_reads, 0)

    def test_diagnostic_does_not_read_udc_after_clock_ownership_loss(self):
        model = USBModel()
        model.put(model.macros["FW_CPM_CLKGR"], struct.pack("<I", model.macros["FW_CPM_USB_GATE_MASK"]))
        model.poll_usb()
        self.assertEqual(model.word(model.symbols["usb"] + 216), 1)
        self.assertEqual(model.gated_udc_reads, 0)
        model.call("debug_usb_close", model.symbols["usb"])
        self.assertEqual(model.gated_udc_reads, 0)
        self.assertEqual((model.imr, model.gpio_mask), (0x100, 0x100))

    def test_ownership_loss_preserves_first_fault_diagnostic(self):
        model = USBModel()
        model.gpio_mask &= ~model.macros["FW_USB_CABLE_GPIO_MASK"]
        model.poll_usb()
        address = model.macros["FW_RUNTIME_BASE"] + 0x8000
        model.call("debug_usb_diagnostics", model.symbols["usb"], address)
        words = struct.unpack("<56I", model.get(address, 224))
        self.assertEqual((words[0], words[1], words[3], words[4]), (3, 56, 1, 1))
        self.assertTrue(words[33] & 16)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--build-dir", type=Path)
    options, remaining = parser.parse_known_args()
    if options.build_dir:
        BUILD = options.build_dir.resolve()
    unittest.main(argv=[__file__, *remaining])
