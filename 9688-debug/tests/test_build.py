# SPDX-License-Identifier: GPL-3.0-only
"""Check native MIPS fixups against an independently linked destination image."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("debug9688_build", SOURCE / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

ASSEMBLY = r"""
.set noreorder
.section .resident_header,"aw"
.globl rd_control
rd_control:
    .word target
    .word worker
    .word mmio
.equ mmio,0xb3040000
.text
.globl worker
worker:
    lui $t0,%hi(target+0x8000)
    addiu $t0,$t0,%lo(target+0x8000)
    lui $t1,%hi(target)
    lui $t2,%hi(target)
    addiu $t1,$t1,%lo(target)
    addiu $t2,$t2,%lo(target+4)
    jal helper
    nop
    beq $a0,$zero,worker
    nop
    jr $ra
    nop
helper:
    jr $ra
    nop
.bss
.balign 32
.globl target
target:
    .space 0x10020
"""


class RelocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.gcc, cls.objcopy = build.find_toolchain()
        except RuntimeError as error:
            raise unittest.SkipTest(str(error))
        cls.temporary = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temporary.name)
        cls.assembly = cls.folder / "fixture.S"
        cls.assembly.write_text(ASSEMBLY, encoding="ascii")
        cls.flags = ["-EL", "-march=mips32", "-mabi=32", "-msoft-float", "-mno-abicalls", "-G0", "-fno-pic"]
        cls.elf, cls.raw = build.compile_link([cls.assembly], "fixture", SOURCE / "resident.ld", cls.folder,
                                             cls.gcc, cls.objcopy, cls.flags, True)
        cls.records, cls.symbols = build.relocation_records(cls.elf, cls.raw)
        cls.memory_size = cls.symbols["__bss_end"] - build.LINK_BASE

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temporary"):
            cls.temporary.cleanup()

    def test_fixups_equal_independent_links_with_hi16_carry(self):
        self.assertEqual({kind for _, kind, _ in self.records}, {1, 2, 3, 4})
        for index, base in enumerate((0x80801260, 0x8181FFC0, 0x81A00000)):
            with self.subTest(base=hex(base)):
                script = self.folder / f"shifted-{index}.ld"
                script.write_text((SOURCE / "resident.ld").read_text(encoding="ascii").replace(
                    "0x81000000", f"0x{base:08x}").replace("0x81200000", f"0x{base + build.MAX_MEMORY:08x}"),
                    encoding="ascii")
                _, expected = build.compile_link([self.assembly], f"shifted-{index}", script, self.folder,
                                                  self.gcc, self.objcopy, self.flags)
                self.assertEqual(build.relocate_image(self.raw, self.records, base, self.memory_size), expected)

    def test_bss_is_reserved_but_not_stored(self):
        self.assertEqual(self.symbols["rd_control"], build.LINK_BASE)
        self.assertEqual(self.symbols["__image_end"] - build.LINK_BASE, len(self.raw))
        self.assertGreaterEqual(self.symbols["__bss_end"] - self.symbols["__bss_start"], 0x10020)
        self.assertLess(len(self.raw), 0x1000)
        self.assertGreater(self.memory_size, 0x10000)

    def test_absolute_mmio_is_unchanged(self):
        patched = build.relocate_image(self.raw, self.records, 0x80801260, self.memory_size)
        self.assertEqual(struct.unpack_from("<I", patched, 8)[0], 0xB3040000)
        self.assertNotIn(8, [offset for offset, _, _ in self.records])

    def test_reject_unsupported_elf_relocation(self):
        from elftools.elf.elffile import ELFFile
        with self.elf.open("rb") as stream:
            image = ELFFile(stream)
            relocation = image.get_section_by_name(".rel.text")
            self.assertIsNotNone(relocation)
            offset = relocation["sh_offset"] + 4
        damaged = bytearray(self.elf.read_bytes())
        damaged[offset] = 9  # R_MIPS_GOT16: cannot be handled as a plain address.
        path = self.folder / "unsupported.elf"
        path.write_bytes(damaged)
        with self.assertRaisesRegex(ValueError, "unsupported MIPS relocation"):
            build.relocation_records(path, self.raw)

    def test_reject_bad_runtime_records(self):
        for records in ([(1, 1, build.LINK_BASE)], [(-4, 1, build.LINK_BASE)],
                        [(len(self.raw), 1, build.LINK_BASE)], [(0, 1, 0xB3040000)],
                        [(0, 9, build.LINK_BASE)], [(0, 1, build.LINK_BASE), (0, 1, build.LINK_BASE)]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                build.validate_records(self.raw, records, self.memory_size)
        for base in (0x80801261, 0x7FFFFFF0, 0x82000000):
            with self.subTest(base=base), self.assertRaises(ValueError):
                build.relocate_image(self.raw, self.records, base, self.memory_size)


class RuntimeIdentityTests(unittest.TestCase):
    def test_canonical_identity_covers_raw_memory_chip_and_relocations(self):
        raw = bytes(range(32))
        records = [(4, 1, build.LINK_BASE + 8), (12, 4, build.LINK_BASE + 28)]
        expected = build.runtime_fingerprint("JZ4740", raw, records, 64)
        self.assertEqual(expected, build.runtime_fingerprint("JZ4740", raw, list(reversed(records)), 64))
        for soc, image, fixups, memory in (
                ("JZ4730", raw, records, 64),
                ("JZ4740", b"X" + raw[1:], records, 64),
                ("JZ4740", raw, [(4, 1, build.LINK_BASE + 12), records[1]], 64),
                ("JZ4740", raw, records, 96)):
            with self.subTest(soc=soc, memory=memory, fixups=fixups):
                self.assertNotEqual(expected, build.runtime_fingerprint(soc, image, fixups, memory))
        with self.assertRaises(ValueError):
            build.runtime_fingerprint("JZ0000", raw, records, 64)

    def test_exact_known_fingerprint_reuses_compatibility_id(self):
        for soc, known in build.KNOWN_RUNTIMES.items():
            with self.subTest(soc=soc):
                self.assertEqual(build.select_runtime_identity(soc, known["fingerprint"]),
                                 (known["image_id"], True))
                changed = "12345678" + known["fingerprint"][8:]
                self.assertEqual(build.select_runtime_identity(soc, changed), (0x12345678, False))
        self.assertEqual(build.select_runtime_identity("JZ4740", "0" * 64), (1, False))

    def test_known_fingerprint_cannot_reuse_id_for_different_final_bytes(self):
        for soc, known in build.KNOWN_RUNTIMES.items():
            with self.subTest(soc=soc), self.assertRaisesRegex(ValueError, "refuse compatible ID"):
                build.validate_runtime_identity(soc, known["fingerprint"], b"changed runtime")
        # Future code can produce its own independently derived identity.
        build.validate_runtime_identity("JZ4740", "12345678" + "0" * 56, b"new runtime")


class ContainerAndProfileTests(unittest.TestCase):
    def test_bda_header_and_payload(self):
        payload = bytes.fromhex("0800e00300000000")  # jr ra; nop
        data = build.pack_bda(payload)
        key = 0x44525744
        self.assertEqual(struct.unpack_from("<I", data, 0x10)[0] ^ key, len(data) - 4)
        self.assertEqual(struct.unpack_from("<I", data, 0x14)[0] ^ key, 0x95F8)
        self.assertEqual(data[0x95F8:], payload)
        self.assertEqual(data[0x2C:0x3C].split(b"\0")[0].decode("gbk"), "9688调试")
        for soc in ("JZ4740", "JZ4730"):
            title = "9688调试" + soc[2:]
            tagged = build.pack_bda(payload, title)
            self.assertEqual(tagged[0x2C:0x3C].split(b"\0")[0].decode("gbk"), title)

    def test_profile_header_rejects_unchecked_input(self):
        profile = {"id": "test", "runtime_base": "0x81c00000", "macros": {"FW_TASK_TABLE": "0x80400000"},
                   "guards": [{"address": "0x80004000", "length": 16, "crc32": "0x12345678"}]}
        header = build.profile_header(profile)
        self.assertIn("#define FW_TASK_TABLE 0x80400000u", header)
        self.assertIn("0x12345678u", header)
        for key, value in (("guards", []), ("runtime_base", "0x83c00000"), ("macros", {"bad\nname": 1})):
            changed = json.loads(json.dumps(profile))
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                build.profile_header(changed)

    def test_supplied_firmware_identity_and_crc_guards(self):
        raw = bytes(range(256))
        wrapped = bytes(64) + raw
        profile = {"load_base": "0x80004000", "firmware": {"size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(), "load_base": "0x80004000"},
                   "packed_firmware": {"size": len(wrapped), "sha256": hashlib.sha256(wrapped).hexdigest()},
                   "guards": [{"name": "test", "address": "0x80004004", "length": 12,
                               "crc32": zlib.crc32(raw[4:16])}]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "firmware.bin"
            for data, form in ((raw, "raw"), (wrapped, "64-byte-wrapper")):
                path.write_bytes(data)
                report = build.verify_firmware(path, profile)
                self.assertEqual(report["format"], form)
                self.assertEqual(report["guard_count"], 1)
                self.assertTrue(report["guards"][0]["matched"])
            path.write_bytes(raw[:-1] + b"x")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                build.verify_firmware(path, profile)
            path.write_bytes(raw)
            profile["guards"][0]["crc32"] = 0
            with self.assertRaisesRegex(ValueError, "guard mismatch"):
                build.verify_firmware(path, profile)

    def test_chip_identity_output_and_source_selection(self):
        profile, _, legacy = build.configuration()
        self.assertEqual(build.profile_soc(profile), "JZ4740")
        self.assertEqual(legacy, (SOURCE.parent / "build/9688-debug").resolve())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "4730.json"
            p = {"soc": "JZ4730", "firmware_version": "V2.32", "guards": [
                {"address": "0x80004000", "length": 4, "crc32": 1}]}
            path.write_text(json.dumps(p))
            selected, _, isolated = build.configuration("jz4730", path)
            self.assertEqual(isolated.name, "9688-debug-jz4730")
            header = build.profile_header(selected)
            self.assertIn('#define RD_SOC_NAME "JZ4730"', header)
            self.assertIn('#define RD_IDENTITY "9688DEBUG/1 V2.32 JZ4730 RESIDENT"', header)
            self.assertIn("#define RD_SOC_JZ4730 1u", header)
            self.assertIn("#define RD_SOC_JZ4740 0u", header)
            with self.assertRaisesRegex(ValueError, "conflicts"):
                build.configuration("jz4740", path)
            with self.assertRaisesRegex(ValueError, "unsupported"):
                build.profile_header(dict(p, soc="JZ9999"))
        self.assertEqual(build.resident_sources_for_soc("JZ4730")[2].name, "usb_jz4730.c")
        self.assertEqual(build.resident_sources_for_soc("JZ4740")[2].name, "usb.c")

    def test_each_chip_rejects_the_other_firmware(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, profiles = {}, {}
            for index, soc in enumerate(("JZ4740", "JZ4730")):
                raw = bytes([index]) * 128 + bytes(range(128))
                path = Path(temporary) / (soc + ".bin")
                path.write_bytes(raw)
                paths[soc] = path
                profiles[soc] = {"soc": soc, "load_base": "0x80004000", "firmware": {
                    "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}, "guards": [
                        {"address": "0x80004004", "length": 12, "crc32": zlib.crc32(raw[4:16])}]}
            for soc, profile in profiles.items():
                self.assertEqual(build.verify_firmware(paths[soc], profile)["guard_count"], 1)
                other = "JZ4730" if soc == "JZ4740" else "JZ4740"
                with self.assertRaisesRegex(ValueError, "SHA256"):
                    build.verify_firmware(paths[other], profile)

    def test_universal_launcher_bindings_keep_both_checked_profiles(self):
        profiles = [json.loads(path.read_text(encoding="utf-8")) for path in build.SOC_PROFILES.values()]
        header = build.launcher_profile_header(profiles)
        self.assertIn("#define RD_LAUNCHER_PROFILE_COUNT 2u", header)
        for profile in profiles:
            self.assertIn(f".soc={profile['soc'][2:]}u", header)
            self.assertIn(f".fw_task_table=0x{build.number(profile['macros']['FW_TASK_TABLE']):08x}u", header)
            self.assertIn(f".notice_guard_count={len(profile.get('notice_guards', []))}u", header)
        self.assertIn("#define FW_TASK_TABLE (rd_selected_profile->fw_task_table)", header)
        with self.assertRaisesRegex(ValueError, "distinct"):
            build.launcher_profile_header([profiles[0], profiles[0]])

    def test_universal_images_do_not_share_ids_or_relocation_tables(self):
        components = [{"resident": bytes([index + 1]) * 8, "records": [(0, 1, build.LINK_BASE)],
                       "report": {"soc": soc, "resident_memory_bytes": 4096,
                                  "image_id": f"{index + 1:08x}"}}
                      for index, soc in enumerate(("JZ4730", "JZ4740"))]
        header = build.universal_image_header(components)
        self.assertIn("#define RD_IMAGES_COUNT 2u", header)
        for index, component in enumerate(components):
            soc = component["report"]["soc"][2:]
            self.assertIn(f"{{{soc}u,0x81000000u,8u,4096u,0x{index+1:08x}u,1u,rd_image_{index},rd_relocations_{index}}}", header)

    def test_legacy_notice_fingerprint_does_not_add_a_supported_runtime(self):
        profiles = [json.loads(path.read_text(encoding="utf-8")) for path in build.SOC_PROFILES.values()]
        legacy = {"runtime_base": "0x81c00000", "gui_table": "0x80200000", "notice_guards": [
            {"address": "0x802002b8", "length": 4, "crc32": "0x12345678"}]}
        header = build.launcher_profile_header(profiles, [legacy])
        self.assertIn("#define RD_LAUNCHER_PROFILE_COUNT 2u", header)
        self.assertIn("#define RD_EXTRA_NOTICE_PROFILE_COUNT 1u", header)
        self.assertIn("{0x81c00000u,0x80200000u,1u,rd_extra_notice_guards_0}", header)
        self.assertIn("0x12345678u", header)
        for mutation in ({"notice_guards": []}, {"runtime_base": "0x81e00000"},
                         {"gui_table": "0xb3040000"}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                build.launcher_profile_header(profiles, [dict(legacy, **mutation)])

    def test_legacy_firmware_check_only_verifies_gui_notice_guards(self):
        raw = bytes(range(256))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.bin"
            for wrapped in (False, True):
                data = bytes(64) + raw if wrapped else raw
                profile = {"firmware": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
                           "notice_guards": [{"address": "0x80004008", "length": 16,
                                              "crc32": zlib.crc32(raw[8:24])}]}
                path.write_bytes(data)
                report = build.verify_notice_firmware(path, profile)
                self.assertEqual(report["format"], "64-byte-wrapper" if wrapped else "raw")
                self.assertTrue(report["notice_only"])
                self.assertFalse(report["private_runtime_supported"])
                profile["notice_guards"][0]["crc32"] ^= 1
                with self.assertRaisesRegex(ValueError, "uniquely"):
                    build.verify_notice_firmware(path, profile)
                path.write_bytes(b"damaged")
                with self.assertRaisesRegex(ValueError, "SHA256/size"):
                    build.verify_notice_firmware(path, profile)


class RealFirmwareProfileTests(unittest.TestCase):
    def test_profiles_are_distinct_and_have_explicit_chip_identity(self):
        profiles = [json.loads(path.read_text(encoding="utf-8")) for path in build.SOC_PROFILES.values()]
        self.assertEqual({build.profile_soc(profile) for profile in profiles}, {"JZ4730", "JZ4740"})
        self.assertEqual(len({profile["firmware"]["sha256"] for profile in profiles}), 2)
        for profile in profiles:
            header = build.profile_header(profile)
            self.assertIn(f'#define RD_SOC_NAME "{profile["soc"]}"', header)
            self.assertTrue(profile["guards"])

    def test_optional_real_firmware_guards_and_cross_chip_rejection(self):
        """Set BBK9688_FIRMWARE_JZ4730/JZ4740 to local raw or packed images."""
        paths = {soc: os.environ.get("BBK9688_FIRMWARE_" + soc) for soc in build.SOC_PROFILES}
        if not all(paths.values()):
            self.skipTest("set both BBK9688_FIRMWARE_JZ4730 and BBK9688_FIRMWARE_JZ4740 for local firmware checks")
        profiles = {soc: json.loads(path.read_text(encoding="utf-8")) for soc, path in build.SOC_PROFILES.items()}
        for soc, path in paths.items():
            with self.subTest(soc=soc):
                report = build.verify_firmware(Path(path), profiles[soc])
                self.assertEqual(report["guard_count"], len(profiles[soc]["guards"]))
                other = "JZ4730" if soc == "JZ4740" else "JZ4740"
                with self.assertRaisesRegex(ValueError, "SHA256"):
                    build.verify_firmware(Path(path), profiles[other])


class BuiltResidentTests(unittest.TestCase):
    def test_real_resident_matches_three_independent_links(self):
        outputs = [SOURCE.parent / "build" / name for name in
                   ("9688-debug", "9688-debug-jz4740", "9688-debug-jz4730",
                    "9688-debug/components/jz4730", "9688-debug/components/jz4740")]
        outputs = [output for output in outputs if (output / "resident.elf").is_file()]
        if not outputs:
            self.skipTest("run 9688-debug/build.py first to validate the real resident")
        gcc, objcopy = build.find_toolchain()
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for output in outputs:
                raw = (output / "resident.bin").read_bytes()
                records, symbols = build.relocation_records(output / "resident.elf", raw)
                memory_size = symbols["__bss_end"] - build.LINK_BASE
                report = json.loads((output / "build.json").read_text(encoding="utf-8"))
                objects = ([output / name for name in report["resident_objects"]] if "resident_objects" in report
                           else sorted(output.glob("resident-*.o")))
                self.assertTrue(objects)
                for index, base in enumerate((0x80801260, 0x8181FFC0, 0x81A00000)):
                    with self.subTest(output=output.name, base=hex(base)):
                        script = folder / f"resident-{index}.ld"
                        script.write_text((SOURCE / "resident.ld").read_text(encoding="ascii").replace(
                            "0x81000000", hex(base)).replace("0x81200000", hex(base + build.MAX_MEMORY)), encoding="ascii")
                        elf = folder / f"resident-{index}.elf"
                        binary = elf.with_suffix(".bin")
                        build.run([gcc, "-EL", "-march=mips32", "-mabi=32", "-msoft-float", "-mno-abicalls", "-G0",
                                   "-fno-pic", "-nostdlib", "-Wl,--build-id=none", "-Wl,--gc-sections",
                                   f"-Wl,-T,{script}", "-o", elf, *objects, "-lgcc"])
                        build.run([objcopy, "-O", "binary", elf, binary])
                        self.assertEqual(build.relocate_image(raw, records, base, memory_size), binary.read_bytes())


if __name__ == "__main__":
    unittest.main()
