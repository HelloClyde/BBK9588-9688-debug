#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Execute the built 9688 dispatcher, parser, EXEC, LCD and launcher in MIPS.

USB/FIFO and firmware services are explicit models from test_usb. Optional
--firmware checks the real signature function against the supplied raw or
64-byte wrapped local firmware; original firmware is never bundled here.
No serial port or physical device is accessed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from unicorn import UC_HOOK_MEM_READ
import test_usb
from test_usb import USBModel as MUSBModel, relocate

SOURCE = Path(__file__).resolve().parents[1]
FIRMWARE = os.environ.get("BBK9688_FIRMWARE")
HEADER = struct.Struct("<4sBBHHH")
BUILD = test_usb.BUILD


def USBModel(build=None, base=0x80807FE0, *, start=True):
    folder = Path(build) if build is not None else BUILD
    report = json.loads((folder / "build.json").read_text(encoding="utf-8"))
    if "jz4730" in report["profile"].lower():
        from test_usb_jz4730 import USBModel as Model
    else:
        Model = MUSBModel
    return Model(folder, base, start=start)


def expected_identity(model):
    soc = "JZ4730" if "jz4730" in model.report["profile"].lower() else "JZ4740"
    return f"9688DEBUG/1 V2.32 {soc} RESIDENT".encode("ascii")


def packet(opcode, sequence, payload=b"", status=0, version=1):
    data = HEADER.pack(b"96DB", version, opcode, sequence, len(payload), status) + payload
    return data + struct.pack("<I", zlib.crc32(data))


def rpc(model, opcode, payload=b"", sequence=123):
    model.write(packet(opcode, sequence, payload))
    header = model.read(12)
    if len(header) != 12:
        raise AssertionError(f"missing reply header: {header.hex()}")
    magic, version, response, returned, count, status = HEADER.unpack(header)
    tail = model.read(count + 4)
    if (magic, version, response, returned) != (b"96DB", 1, opcode | 128, sequence):
        raise AssertionError("response header mismatch")
    if len(tail) != count + 4 or zlib.crc32(header + tail[:-4]) != struct.unpack("<I", tail[-4:])[0]:
        raise AssertionError("response CRC/length mismatch")
    return status, tail[:-4]


def dispatch(model, opcode, payload=b""):
    scratch = model.macros["FW_RUNTIME_BASE"] + 0x8000
    output, status = scratch + 1024, scratch + 2048
    model.put(scratch, payload)
    model.put(status, struct.pack("<I", 0xFFFFFFFF))
    count = model.call("dispatch", opcode, scratch, len(payload), output, status)
    return model.word(status), model.get(output, count)


def native_screen(model, bits=16):
    width, height = 240, 320
    pixel_bytes = 2 if bits == 16 else 4
    total = width * height * pixel_bytes
    descriptor, source = 0x100000, 0x1200000
    pixels = bytes((i * 7 + 13) % 251 for i in range(total))
    for name, value in (("FW_LCD_WIDTH", width), ("FW_LCD_HEIGHT", height), ("FW_LCD_BITS", bits)):
        model.put(model.macros[name], struct.pack("<I", value))
    model.put(model.macros["FW_LCD_BASE"] + 0x30, struct.pack("<I", 8))
    model.put(model.macros["FW_LCD_BASE"] + 0x40, struct.pack("<I", descriptor))
    model.put(descriptor, struct.pack("<4I", descriptor, source, 0, total // 4))
    model.put(source, pixels)
    return descriptor, source, pixels


def firmware_bytes(model):
    if not FIRMWARE:
        raise unittest.SkipTest("pass --firmware or BBK9688_FIRMWARE for local firmware guard checks")
    supplied = Path(FIRMWARE).read_bytes()
    expected = model.report["firmware"]["sha256"]
    if hashlib.sha256(supplied).hexdigest() == expected:
        return supplied
    if hashlib.sha256(supplied[64:]).hexdigest() == expected:
        return supplied[64:]
    raise AssertionError("supplied firmware SHA256 does not match the built profile")


class DeviceTests(unittest.TestCase):
    def test_fragmented_frames_and_crc_resynchronization(self):
        model = USBModel()
        model.fragment_bytes = 1
        status, identity = rpc(model, 1)
        self.assertEqual((status, identity), (0, expected_identity(model)))
        damaged = bytearray(packet(1, 10))
        damaged[-1] ^= 0x80
        model.write(b"noise" + damaged + packet(1, 11))
        response = model.read(128)
        self.assertEqual(response, packet(0x81, 11, identity))
        status, data = rpc(model, 2)
        self.assertEqual(status, 0)
        self.assertGreaterEqual(struct.unpack("<16I", data)[6], 1)

    def test_bad_version_reserved_status_and_oversized_payload_are_dropped(self):
        model = USBModel()
        bad_version = packet(1, 20, version=2)
        bad_status = packet(1, 21, status=1)
        oversized = packet(1, 22, bytes(513))
        model.write(bad_version + bad_status + oversized + packet(1, 23))
        expected = packet(0x81, 23, expected_identity(model))
        self.assertEqual(model.read(128), expected)

    def test_invalid_opcode_and_payload_do_not_mutate_ram(self):
        model = USBModel()
        before = model.get(model.base, 80)
        for opcode, payload in ((127, b""), (1, b"x"), (2, b"x"), (9, b"x"), (3, b""), (4, bytes(4))):
            with self.subTest(opcode=opcode, payload=payload):
                self.assertEqual(dispatch(model, opcode, payload), (1, b""))
        self.assertEqual(model.get(model.base, 80), before)

    def test_ram_bounds_and_self_write_guard(self):
        model = USBModel()
        scratch = model.macros["FW_RUNTIME_BASE"] + 0xD000
        self.assertEqual(dispatch(model, 4, struct.pack("<I", scratch) + b"abc"), (0, b""))
        self.assertEqual(dispatch(model, 3, struct.pack("<II", scratch, 3)), (0, b"abc"))
        for address, size in ((0x7FFFFFFF, 1), (0xB3040000, 1), (0xFFFFFFFF, 4),
                              (model.macros["FW_SDRAM_END"] - 1, 2), (scratch, 0), (scratch, 513)):
            with self.subTest(address=hex(address), size=size):
                self.assertEqual(dispatch(model, 3, struct.pack("<II", address, size)), (1, b""))
        end = model.macros["FW_SDRAM_END"]
        model.put(end - 4, b"tail")
        self.assertEqual(dispatch(model, 3, struct.pack("<II", end - 4, 4)), (0, b"tail"))
        before = model.get(model.base, 80)
        self.assertEqual(dispatch(model, 4, struct.pack("<I", model.base + 4) + b"bad"), (1, b""))
        self.assertEqual(dispatch(model, 4, struct.pack("<I", model.base - 1) + b"cross"), (1, b""))
        self.assertEqual(model.get(model.base, 80), before)

    def test_exact_packet_response_finishes_with_zero_length_packet(self):
        model = USBModel()
        address = model.macros["FW_RUNTIME_BASE"] + 0x9000
        model.put(address, bytes(range(48)))
        self.assertEqual(rpc(model, 3, struct.pack("<II", address, 48)), (0, bytes(range(48))))
        self.assertEqual(model.packet_sizes[-2:], [64, 0])

    def test_runtime_table_call_checks_table_index_offset_and_function(self):
        model = USBModel()
        invalid = [(5, 0), (0, 1), (0, 0x900)]
        for index, offset in invalid:
            self.assertEqual(dispatch(model, 20, struct.pack("<6I", index, offset, 0, 0, 0, 0)), (1, b""))
        table, function = 0x80700000, 0x80701000
        model.put(model.base + 56, struct.pack("<I", table))
        model.put(table, struct.pack("<I", function))
        model.put(function, struct.pack("<3I", 0x00851021, 0x03E00008, 0))  # v0=a0+a1
        self.assertEqual(dispatch(model, 20, struct.pack("<6I", 0, 0, 12, 30, 0, 0)), (0, struct.pack("<I", 42)))
        model.put(table, struct.pack("<I", 0xB3040000))
        self.assertEqual(dispatch(model, 20, struct.pack("<6I", 0, 0, 0, 0, 0, 0)), (1, b""))

    def test_exec_crc_sequential_upload_and_state_guards(self):
        model = USBModel()
        code = struct.pack("<2I", 0x03E00008, 0x24020007)
        for size, memory, entry in ((0, 8, 0), (65537, 65537, 0), (8, 7, 0), (8, 8, 8), (8, 8, 1)):
            self.assertEqual(dispatch(model, 27, struct.pack("<4I", size, memory, entry, 0)), (1, b""))
        self.assertEqual(dispatch(model, 27, struct.pack("<4I", 8, 32, 0, 0)), (0, b""))
        self.assertEqual(dispatch(model, 28, struct.pack("<I", 1) + code), (1, b""))
        self.assertEqual(dispatch(model, 29), (1, b""))
        self.assertEqual(dispatch(model, 28, struct.pack("<I", 0) + code[:4]), (0, b""))
        self.assertEqual(dispatch(model, 28, struct.pack("<I", 0) + code[:4]), (1, b""))
        self.assertEqual(dispatch(model, 28, struct.pack("<I", 4) + code[4:]), (0, b""))
        self.assertEqual(dispatch(model, 29), (3, b""))
        self.assertEqual(dispatch(model, 30, struct.pack("<II", 0, 80)), (1, b""))
        self.assertEqual(dispatch(model, 27, struct.pack("<4I", 8, 32, 0, zlib.crc32(code))), (0, b""))
        self.assertEqual(dispatch(model, 28, struct.pack("<I", 0) + code), (0, b""))
        self.assertEqual(dispatch(model, 29), (0, b""))
        self.assertEqual(dispatch(model, 30, struct.pack("<II", 0, 0)), (1, b""))
        self.assertEqual(dispatch(model, 30, struct.pack("<II", 0, 401)), (1, b""))
        self.assertEqual(dispatch(model, 30, struct.pack("<II", 0, 80)), (0, b""))
        self.assertEqual(dispatch(model, 27, struct.pack("<4I", 8, 8, 0, zlib.crc32(code))), (2, b""))
        self.assertEqual(dispatch(model, 32), (0, b""))
        state = struct.unpack("<9I", dispatch(model, 31)[1])
        self.assertEqual((state[1], state[2], state[3]), (5, 0xFFFFFFFF, 1))

    def test_compiled_compute_exec_returns_294_and_output(self):
        model = USBModel()
        spec = importlib.util.spec_from_file_location("debug9688_device_client", SOURCE / "client.py")
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        info = struct.unpack("<10I", dispatch(model, 26)[1])
        with tempfile.TemporaryDirectory() as directory:
            metadata, image = client.compile_module(SOURCE / "examples/compute.c", Path(directory), info[1], info[2])
        request = struct.pack("<4I", len(image), metadata["memory_bytes"], metadata["entry_offset"], zlib.crc32(image))
        self.assertEqual(dispatch(model, 27, request), (0, b""))
        for offset in range(0, len(image), 508):
            self.assertEqual(dispatch(model, 28, struct.pack("<I", offset) + image[offset:offset + 508]), (0, b""))
        self.assertEqual(dispatch(model, 29), (0, b""))
        self.assertEqual(dispatch(model, 30, struct.pack("<II", 0, 80) + b"abc"), (0, b""))
        model.call("rd_exec_invoke", model.symbols["module_api"])
        state = struct.unpack("<9I", dispatch(model, 31)[1])
        self.assertEqual((state[1], state[2], state[3], state[7]), (5, 294, 0, 4))
        self.assertEqual(dispatch(model, 33, struct.pack("<II", 0, 4)), (0, struct.pack("<3I", 0, 4, 294)))

    def test_real_host_client_to_mips_device_end_to_end(self):
        model = USBModel()
        spec = importlib.util.spec_from_file_location("debug9688_end_to_end_client", SOURCE / "client.py")
        client_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client_module)
        client = client_module.Client(model)
        self.assertEqual(client.handshake(), expected_identity(model).decode())
        self.assertEqual(client.status()["resident_base"], model.base)
        address = model.macros["FW_RUNTIME_BASE"] + 0xD000
        data = bytes(i % 251 for i in range(1111))
        self.assertEqual(client.write(address, data)["bytes"], len(data))
        self.assertEqual(client.read(address, len(data)), data)
        descriptor, source, pixels = native_screen(model)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            screen = client.screenshot(directory / "screen.png")
            self.assertEqual((screen["width"], screen["height"], screen["raw_sha256"]),
                             (240, 320, hashlib.sha256(pixels).hexdigest()))
            state = client.exec_run(SOURCE / "examples/compute.c", directory / "module", args=b"abc")
            self.assertEqual((state["result"], state["output_hex"]), (294, "26010000"))
            self.assertTrue((directory / "module/execution.json").is_file())
        client.close()
        self.assertTrue(model.closed)

    def test_native_lcd_240x320_rgb565_and_32_bit(self):
        for bits, pixel_bytes, format_code in ((16, 2, 1), (18, 4, 2), (32, 4, 2)):
            with self.subTest(bits=bits):
                model = USBModel()
                descriptor, source, pixels = native_screen(model, bits)
                status, data = dispatch(model, 6)
                self.assertEqual(status, 0)
                token, width, height, stride, fmt, total, address, tick = struct.unpack("<8I", data)
                self.assertEqual((width, height, stride, fmt, total, address),
                                 (240, 320, 240 * pixel_bytes, format_code, len(pixels), source | 0xA0000000))
                model.put(source, bytes(16))
                status, block = dispatch(model, 7, struct.pack("<3I", token, 0, 16))
                self.assertEqual((status, block[12:]), (0, pixels[:16]))
                self.assertEqual(dispatch(model, 7, struct.pack("<3I", token + 1, 0, 16)), (2, b""))
                self.assertEqual(dispatch(model, 7, struct.pack("<3I", token, total, 16)), (1, b""))
                self.assertEqual(dispatch(model, 7, struct.pack("<3I", token, 0, 501)), (1, b""))
                self.assertEqual(dispatch(model, 8, struct.pack("<I", token)), (0, b""))
                self.assertEqual(dispatch(model, 7, struct.pack("<3I", token, 0, 16)), (2, b""))

    def test_lcd_rejects_dimensions_palette_short_frame_and_changed_descriptor(self):
        for change in ("dimensions", "palette", "length", "source", "chain"):
            with self.subTest(change=change):
                model = USBModel()
                descriptor, source, pixels = native_screen(model)
                if change == "dimensions":
                    model.put(model.macros["FW_LCD_WIDTH"], struct.pack("<I", 320))
                elif change == "palette":
                    model.put(descriptor + 12, struct.pack("<I", 0x10000000 | (len(pixels) // 4)))
                elif change == "length":
                    model.put(descriptor + 12, struct.pack("<I", len(pixels) // 4 - 1))
                elif change == "source":
                    model.put(descriptor + 4, struct.pack("<I", 0x02000000))
                else:
                    model.put(descriptor, struct.pack("<I", descriptor + 16))
                self.assertEqual(dispatch(model, 6), (2, b""))
        model = USBModel()
        descriptor, source, pixels = native_screen(model)
        def swap(uc, access, address, size, value, _):
            model.put(model.macros["FW_LCD_BASE"] + 0x40, struct.pack("<I", descriptor + 16))
        model.u.hook_add(UC_HOOK_MEM_READ, swap, begin=source, end=source)
        self.assertEqual(dispatch(model, 6), (3, b""))

    def test_real_firmware_guard_acceptance_and_single_byte_rejection(self):
        model = USBModel(start=False)
        raw = firmware_bytes(model)
        model.put(model.macros["FW_SDRAM_START"], raw)
        relocation = json.loads((model.build / "relocations.json").read_text(encoding="utf-8"))
        image = relocate((model.build / "resident.bin").read_bytes(), relocation["records"], relocation["base"], model.base)
        offset = model.symbols["fw_matches"] - model.base
        model.put(model.symbols["fw_matches"], image[offset:offset + 8])
        self.assertEqual(model.call("fw_matches"), 1)
        address = model.macros["FW_NATIVE_USB_INIT"]
        model.put(address, bytes([model.get(address, 1)[0] ^ 1]))
        self.assertEqual(model.call("fw_matches"), 0)

    def test_launcher_rejects_unknown_firmware_without_gui_or_allocator(self):
        model = USBModel(start=False)
        model.put(model.report["entry_va"], (model.build / "launcher.bin").read_bytes())
        model.symbols = model.report["launcher_symbols"]
        model.stub_return("fw_matches", 0)
        # Runtime service prefix remains all zero. A GUI or allocator call
        # would fail rather than being hidden behind a permissive stub.
        self.assertEqual(model.call("bda_main"), 1)
        self.assertEqual(model.get(model.macros["FW_RUNTIME_BASE"], 32), bytes(32))

    def test_real_other_chip_firmware_is_rejected_before_launch(self):
        model = USBModel(start=False)
        chip = "JZ4730" if "jz4730" in model.report["profile"].lower() else "JZ4740"
        other = "JZ4740" if chip == "JZ4730" else "JZ4730"
        path = os.environ.get("BBK9688_FIRMWARE_" + other)
        if not path:
            self.skipTest("supply the other chip's local firmware for runtime rejection")
        profile = json.loads((SOURCE / "profiles" / f"9688-v232-{other.lower()}.json").read_text(encoding="utf-8"))
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != profile["firmware"]["sha256"]:
            raw = raw[64:]
        self.assertEqual(hashlib.sha256(raw).hexdigest(), profile["firmware"]["sha256"])
        model.put(model.macros["FW_SDRAM_START"], raw)
        model.put(model.report["entry_va"], (model.build / "launcher.bin").read_bytes())
        model.symbols = model.report["launcher_symbols"]
        self.assertEqual(model.call("bda_main"), 1)
        self.assertEqual(model.get(model.macros["FW_RUNTIME_BASE"], 32), bytes(32))

    def test_launcher_rejects_unknown_priority6_task(self):
        model = USBModel(start=False)
        raw = firmware_bytes(model)
        model.put(model.macros["FW_SDRAM_START"], raw)
        model.put(model.report["entry_va"], (model.build / "launcher.bin").read_bytes())
        model.symbols = model.report["launcher_symbols"]
        # Preserve the checked native GUI slot and code; model only the
        # launcher's actual notice boundary, returning its error argument.
        model.put(model.symbols["notice"], struct.pack("<2I", 0x03E00008, 0x00A01021))
        model.put(model.macros["FW_TASK_TABLE"] + 6 * 4, struct.pack("<I", 0x80702000))
        model.put(0x80702000 + 0x35, b"\x06")
        self.assertEqual(model.call("bda_main"), 5)
        self.assertEqual(model.word(model.macros["FW_TASK_TABLE"] + 6 * 4), 0x80702000)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--firmware", type=Path)
    options, remaining = parser.parse_known_args()
    if options.build_dir:
        BUILD = options.build_dir.resolve()
    if options.firmware:
        FIRMWARE = options.firmware
    unittest.main(argv=[__file__, *remaining])
