#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Execute one universal BDA payload against both checked local firmware images.

Only the launcher's allocator, task-creation and notice boundaries are replaced
for the allocation test. The firmware and its guards remain unchanged. No MMIO
is mapped, so accidental USB/controller access fails the model immediately.
This does not emulate a V2.1 firmware or claim physical device compatibility.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import unittest
from pathlib import Path

from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE
from unicorn.mips_const import (
    UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3,
    UC_MIPS_REG_CP0_STATUS, UC_MIPS_REG_SP, UC_MIPS_REG_RA,
    UC_MIPS_REG_PC, UC_MIPS_REG_V0,
)

SOURCE = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get("BBK9688_UNIVERSAL_BUILD_DIR", SOURCE.parent / "build/9688-debug"))
SOCS = ("JZ4730", "JZ4740")


def number(value):
    return int(value, 0) if isinstance(value, str) else int(value)


class LauncherModel:
    sentinel = 0x81BF0000
    heap = 0x80807FE1  # Deliberately unaligned allocator result.

    def __init__(self, build, report, profile=None, firmware=None):
        self.build, self.report, self.profile = build, report, profile
        self.symbols = report["launcher_symbols"]
        self.macros = {key: number(value) for key, value in profile["macros"].items()} if profile else {}
        self.u = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        self.u.mem_map(0, 0x2000000)
        self.entry = report["entry_va"]
        self.payload = (build / report["binary"]).read_bytes()[0x95F8:]
        self.put(self.entry, self.payload)
        if firmware is not None:
            base = number(profile["load_base"])
            self.put(base, firmware)
            seed = self.macros["FW_BDA_SEED"] - base
            self.put(self.macros["FW_RUNTIME_BASE"], firmware[seed:seed + 32])
        self.boundaries = {}
        self.calls = []
        self.u.hook_add(UC_HOOK_CODE, self._observe)

    def put(self, address, data):
        self.u.mem_write(address & 0x1FFFFFFF, data)

    def get(self, address, size):
        return bytes(self.u.mem_read(address & 0x1FFFFFFF, size))

    def word(self, address):
        return struct.unpack("<I", self.get(address, 4))[0]

    def set_word(self, address, value):
        self.put(address, struct.pack("<I", value))

    def string(self, address):
        out = bytearray()
        for offset in range(512):
            byte = self.get(address + offset, 1)[0]
            if not byte:
                return out.decode("ascii")
            out.append(byte)
        raise AssertionError("unterminated diagnostic string")

    def _observe(self, uc, address, size, _):
        if not self.entry <= address < self.entry + len(self.payload):
            raise AssertionError(f"unexpected external execution at {address:#x}")
        if address in self.boundaries:
            name = self.boundaries[address]
            args = tuple(uc.reg_read(register) for register in
                         (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3))
            self.calls.append((name, args, self.string(args[0]) if name == "notice" else None))

    def stub(self, name, result=0, *, error_argument=False):
        address = self.symbols[name]
        self.boundaries[address] = name
        # Observe at a real entry and let MIPS execute JR and its delay slot.
        # Changing PC in a hook would invalidate the call/delay-slot test.
        if error_argument:
            instructions = (0x03E00008, 0x00A01021)  # jr ra; move v0,a1
        elif result < 0x8000:
            instructions = (0x03E00008, 0x24020000 | result)
        else:
            instructions = (0x3C020000 | (result >> 16), 0x34420000 | (result & 0xFFFF),
                            0x03E00008, 0)
        self.put(address, struct.pack("<" + "I" * len(instructions), *instructions))

    def call(self, name, *args):
        self.u.reg_write(UC_MIPS_REG_CP0_STATUS, 0x10000000)
        self.u.reg_write(UC_MIPS_REG_SP, 0x81BE0000)
        self.u.reg_write(UC_MIPS_REG_RA, self.sentinel)
        for register, value in zip((UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3), args):
            self.u.reg_write(register, value)
        self.u.emu_start(self.symbols[name], self.sentinel, count=12000000)
        if self.u.reg_read(UC_MIPS_REG_PC) != self.sentinel:
            raise AssertionError(f"{name} did not return within the instruction budget")
        return self.u.reg_read(UC_MIPS_REG_V0)


class UniversalLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = BUILD.resolve()
        manifest = cls.build / "build.json"
        if not manifest.is_file():
            raise unittest.SkipTest("build the universal debugger first")
        cls.report = json.loads(manifest.read_text(encoding="utf-8"))
        if cls.report.get("mode") != "universal":
            raise unittest.SkipTest("BBK9688_UNIVERSAL_BUILD_DIR must contain a universal build")
        cls.profiles = {soc: json.loads((SOURCE / "profiles" / f"9688-v232-{soc.lower()}.json").read_text(
            encoding="utf-8")) for soc in SOCS}

    def model(self, soc):
        variable = "BBK9688_FIRMWARE_" + soc
        path = os.environ.get(variable)
        if not path:
            raise unittest.SkipTest(f"set {variable} to its checked local firmware")
        supplied = Path(path).read_bytes()
        profile = self.profiles[soc]
        expected = profile["firmware"]["sha256"]
        if hashlib.sha256(supplied).hexdigest() == expected:
            raw = supplied
        elif len(supplied) >= 64 and hashlib.sha256(supplied[64:]).hexdigest() == expected:
            raw = supplied[64:]
        else:
            self.fail(f"{variable} does not match its local firmware profile")
        return LauncherModel(self.build, self.report, profile, raw)

    def legacy_model(self):
        """The real device backup is used only to verify refusal plus a notice."""
        path = os.environ.get("BBK9688_FIRMWARE_LEGACY")
        if not path:
            raise unittest.SkipTest("set BBK9688_FIRMWARE_LEGACY to the original device firmware backup")
        profile = json.loads((SOURCE / "profiles/9688-legacy-notice.json").read_text(encoding="utf-8"))
        supplied = Path(path).read_bytes()
        self.assertEqual(len(supplied), profile["firmware"]["size"])
        self.assertEqual(hashlib.sha256(supplied).hexdigest(), profile["firmware"]["sha256"])
        # This exact backup contains the normal 64-byte kernel wrapper.
        # It contributes no RTOS, allocator or USB bindings to the launcher.
        seed = next(guard for guard in profile["notice_guards"] if guard["name"] == "bda_seed")
        model_profile = {"load_base": "0x80004000", "macros": {
            "FW_RUNTIME_BASE": profile["runtime_base"], "FW_BDA_SEED": seed["address"]}}
        return LauncherModel(self.build, self.report, model_profile, supplied[64:]), profile

    def test_one_bda_payload_selects_both_real_firmware_profiles(self):
        bda = (self.build / self.report["binary"]).read_bytes()
        self.assertEqual(hashlib.sha256(bda).hexdigest(), self.report["sha256"])
        self.assertEqual(bda[0x95F8:], (self.build / "launcher.bin").read_bytes())
        self.assertEqual(set(self.report["supported_socs"]), set(SOCS))
        for soc in SOCS:
            with self.subTest(soc=soc):
                model = self.model(soc)
                self.assertEqual(model.call("fw_matches"), 1)
                self.assertEqual(model.call("fw_selected_soc"), int(soc[2:]))
                self.assertEqual(model.call("fw_match_error"), 0)
                self.assertEqual(model.call("fw_notice_safe"), 1)
                self.assertEqual(model.calls, [])

    def test_each_guard_rejects_one_byte_damage_with_a_diagnostic(self):
        for soc in SOCS:
            model = self.model(soc)
            for index, guard in enumerate(self.profiles[soc]["guards"], 1):
                with self.subTest(soc=soc, guard=guard["name"]):
                    address = number(guard["address"])
                    original = model.get(address, 1)
                    model.put(address, bytes([original[0] ^ 1]))
                    self.assertEqual(model.call("fw_matches"), 0)
                    self.assertEqual(model.call("fw_selected_soc"), 0)
                    self.assertEqual(model.call("fw_match_error"), (int(soc[2:]) << 16) | index)
                    self.assertEqual(model.call("fw_notice_safe"), 1)
                    model.put(address, original)
            self.assertEqual(model.call("fw_matches"), 1)

    def test_runtime_tables_are_checked_before_selecting_a_resident(self):
        for soc in SOCS:
            model = self.model(soc)
            runtime = model.macros["FW_RUNTIME_BASE"]
            for index in range(5):
                with self.subTest(soc=soc, table=index):
                    address = runtime + 4 + index * 4
                    original = model.word(address)
                    model.set_word(address, original ^ 4)
                    self.assertEqual(model.call("fw_matches"), 0)
                    self.assertEqual(model.call("fw_selected_soc"), 0)
                    self.assertNotEqual(model.call("fw_match_error"), 0)
                    model.set_word(address, original)

    def test_unknown_zero_ram_returns_without_any_external_call(self):
        model = LauncherModel(self.build, self.report)
        self.assertEqual(model.call("fw_notice_safe"), 0)
        self.assertEqual(model.call("bda_main"), 1)
        self.assertEqual(model.call("fw_selected_soc"), 0)
        self.assertEqual(model.call("fw_match_error"), 0xFF00)
        self.assertEqual(model.calls, [])

    def test_damaged_gui_cannot_be_used_to_display_an_error(self):
        for soc in SOCS:
            for mutation in ("runtime_pointer", "slot", "code"):
                with self.subTest(soc=soc, mutation=mutation):
                    model = self.model(soc)
                    if mutation == "runtime_pointer":
                        address = model.macros["FW_RUNTIME_BASE"] + 4
                    else:
                        guard = self.profiles[soc]["notice_guards"][0 if mutation == "slot" else 1]
                        address = number(guard["address"])
                    original = model.get(address, 1)
                    model.put(address, bytes([original[0] ^ 1]))
                    self.assertEqual(model.call("fw_notice_safe"), 0)
                    self.assertEqual(model.call("bda_main"), 1)
                    self.assertEqual(model.call("fw_selected_soc"), 0)
                    self.assertEqual(model.calls, [])

    def test_guard_failure_reports_its_index_without_private_calls(self):
        for soc in SOCS:
            with self.subTest(soc=soc):
                model = self.model(soc)
                address = number(self.profiles[soc]["guards"][0]["address"])
                original = model.get(address, 1)
                model.put(address, bytes([original[0] ^ 1]))
                model.stub("notice", error_argument=True)
                self.assertEqual(model.call("bda_main"), 1)
                self.assertEqual(len(model.calls), 1)
                name, args, message = model.calls[0]
                self.assertEqual(name, "notice")
                self.assertIn("Unsupported firmware", message)
                self.assertEqual(args[1], (int(soc[2:]) << 16) | 1)

    def test_real_legacy_backup_can_only_display_the_unsupported_notice(self):
        model, _ = self.legacy_model()
        self.assertEqual(model.call("fw_notice_safe"), 1)
        self.assertEqual(model.call("fw_matches"), 0)
        self.assertEqual(model.call("fw_selected_soc"), 0)
        model.stub("notice", error_argument=True)
        self.assertEqual(model.call("bda_main"), 1)
        self.assertEqual(model.call("fw_selected_soc"), 0)
        self.assertEqual(len(model.calls), 1)
        name, args, message = model.calls[0]
        self.assertEqual(name, "notice")
        self.assertIn("Unsupported firmware", message)
        self.assertIn("required", message)
        self.assertEqual(args[1], 0xFF00)

    def test_each_legacy_notice_guard_rejects_one_byte_damage(self):
        model, profile = self.legacy_model()
        for guard in profile["notice_guards"]:
            with self.subTest(guard=guard["name"]):
                address = number(guard["address"])
                original = model.get(address, 1)
                model.put(address, bytes([original[0] ^ 1]))
                self.assertEqual(model.call("fw_notice_safe"), 0)
                self.assertEqual(model.call("bda_main"), 1)
                self.assertEqual(model.call("fw_selected_soc"), 0)
                self.assertEqual(model.calls, [])
                model.put(address, original)
        self.assertEqual(model.call("fw_notice_safe"), 1)

    def test_real_entry_copies_and_relocates_only_the_selected_resident(self):
        for soc in SOCS:
            with self.subTest(soc=soc):
                model = self.model(soc)
                model.stub("notice", error_argument=True)
                model.stub("fw_alloc", model.heap)
                model.stub("fw_free")
                model.stub("fw_task_create", 7)
                self.assertEqual(model.call("bda_main"), 7)
                self.assertEqual(model.call("fw_selected_soc"), int(soc[2:]))
                component = next(item for item in self.report["components"] if item["soc"] == soc)
                folder = self.build / component["directory"]
                manifest = json.loads((folder / "build.json").read_text(encoding="utf-8"))
                raw = (folder / "resident.bin").read_bytes()
                base = (model.heap + 31) & ~31
                memory = component["resident_memory_bytes"]
                header = struct.unpack("<20I", model.get(base, 80))
                self.assertEqual(header[:6], (0x38444239, 1, model.heap, base, memory, 6))
                self.assertEqual(header[6:9], (0, 0, 0))
                for word, name in zip(header[9:12], ("resident_init", "resident_worker", "resident_shutdown")):
                    self.assertEqual(word, base + manifest["symbols"][name] - manifest["resident_link_base"])
                self.assertEqual(header[12] - header[13], 32768 - 4)
                runtime = model.macros["FW_RUNTIME_BASE"]
                self.assertEqual(header[14:19], tuple(model.word(runtime + 4 + 4 * i) for i in range(5)))
                self.assertEqual(header[19], int(component["image_id"], 16))
                # The BSS is zeroed at the selected image boundary, not stored in the BDA.
                self.assertEqual(model.get(base + len(raw), memory - len(raw)), bytes(memory - len(raw)))
                reloc = json.loads((folder / "relocations.json").read_text(encoding="utf-8"))
                relocated_words = {offset for offset, _, _ in reloc["records"]}
                for offset in range(80, len(raw), 4):
                    if offset not in relocated_words:
                        self.assertEqual(model.get(base + offset, min(4, len(raw) - offset)), raw[offset:offset + 4])
                for offset, kind, target in reloc["records"]:
                    if offset < 80:
                        continue  # The launcher intentionally fills the control header.
                    word = model.word(base + offset)
                    destination = target + base - reloc["base"]
                    if kind == 1:
                        self.assertEqual(word, destination)
                    elif kind == 2:
                        self.assertEqual((word & 0x03FFFFFF) << 2, destination & 0x0FFFFFFF)
                    elif kind == 3:
                        self.assertEqual(word & 0xFFFF, ((destination + 0x8000) >> 16) & 0xFFFF)
                    else:
                        self.assertEqual(word & 0xFFFF, destination & 0xFFFF)
                names = [item[0] for item in model.calls]
                self.assertEqual(names, ["notice", "fw_alloc", "fw_task_create", "fw_free", "notice"])
                self.assertIn(soc, model.calls[0][2])
                self.assertEqual(model.calls[1][1][0], memory + 31)
                self.assertEqual(model.calls[2][1][0], base)
                self.assertEqual(model.calls[3][1][0], model.heap)
                self.assertEqual(model.calls[4][1][1], 7)
                self.assertIn("Unable to create resident task", model.calls[4][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
