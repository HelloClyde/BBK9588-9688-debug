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
spec = importlib.util.spec_from_file_location("debug9588_build", SOURCE / "build.py")
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

class BuiltResidentTests(unittest.TestCase):
    def test_real_resident_matches_three_independent_links(self):
        outputs = [Path(os.environ.get("BBK9588_BUILD_DIR", SOURCE.parent / "build/9588-debug"))]
        outputs = [output for output in outputs if (output / "resident.elf").is_file()]
        if not outputs:
            self.skipTest("run 9588-debug/build.py first to validate the real resident")
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

class TargetAndFirmwareTests(unittest.TestCase):
    def test_foreign_or_incomplete_profile_is_rejected(self):
        for profile in ({"target":"9688","soc":"JZ4740","verified":True},
                        {"target":"9588","soc":"JZ4730","verified":True},
                        {"target":"9588","soc":"JZ4740","verified":False}):
            with self.subTest(profile=profile),self.assertRaises(ValueError):
                build.profile_header(profile)

    def test_profile_set_rejects_missing_duplicate_and_ambiguous_selectors(self):
        profiles=[p for _,_,p in build.registered_profiles()]
        with self.assertRaises(ValueError):
            build.validate_profile_set(profiles[:-1])
        with self.assertRaises(ValueError):
            build.validate_profile_set([profiles[0],profiles[0],profiles[2]])
        copied=json.loads(json.dumps(profiles))
        for name in ("FW_RUNTIME_BASE","FW_GUI_TABLE","FW_FS_TABLE","FW_SYS_TABLE","FW_MEM_TABLE","FW_RES_TABLE"):
            copied[1]["macros"][name]=copied[0]["macros"][name]
        copied[1]["guards"]=copied[0]["guards"]
        copied[1]["notice_guards"]=copied[0].get("notice_guards",[])
        with self.assertRaisesRegex(ValueError,"ambiguous"):
            build.validate_profile_set(copied)
        for p in profiles:
            wrong=json.loads(json.dumps(p))
            wrong["usb_controller"]="wrong-controller"
            with self.assertRaisesRegex(ValueError,"USB controller"):
                build.profile_header(wrong)

    def test_each_soc_has_its_own_identity_pid_and_controller(self):
        for soc,path,profile in build.registered_profiles():
            with self.subTest(soc=soc):
                header=build.profile_header(profile)
                self.assertIn(f'#define RD_SOC_ID {soc[2:]}u',header)
                self.assertIn(f'#define RD_IDENTITY "9588DEBUG/1 {soc} RESIDENT"',header)
                self.assertIn(f'#define RD_SOC_{soc} 1u',header)
                self.assertEqual(build.resident_sources_for_soc(soc)[2].name,
                                 "usb_jz4730.c" if soc=="JZ4730" else "usb.c")

    def test_public_sdk_bda_header_checksum_and_entry(self):
        build.configure_sdk()
        payload=bytes.fromhex("0800e00300000000")
        data=build.pack_bda(payload,"9588调试")
        self.assertEqual(data[0x95f8:],payload)
        self.assertEqual(data[0x2c:0x3c].split(bytes(1))[0].decode("gbk"),"9588调试")
        key=0x44525744
        self.assertEqual(struct.unpack_from("<I",data,0x14)[0]^key,0x95f8)

    def test_raw_wrapped_hash_and_crc_rejection(self):
        # Synthetic bytes exercise container/hash/guard validation, never
        # register a fabricated device profile as a supported firmware.
        raw=bytes(range(128))
        profile={"load_base":"0x80004000","firmware":{"size":len(raw),
            "sha256":hashlib.sha256(raw).hexdigest(),"load_base":"0x80004000"},
            "guards":[{"address":"0x80004004","length":16,"crc32":zlib.crc32(raw[4:20])}]}
        wrapped=bytes(64)+raw
        profile["packed_firmware"]={"size":len(wrapped),"sha256":hashlib.sha256(wrapped).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/"fixture.bin"
            for image,form in ((raw,"raw"),(wrapped,"64-byte-wrapper")):
                path.write_bytes(image)
                self.assertEqual(build.verify_firmware(path,profile)["format"],form)
            path.write_bytes(raw)
            wrong=json.loads(json.dumps(profile));wrong["guards"][0]["crc32"]^=1
            with self.assertRaisesRegex(ValueError,"CRC guard mismatch"):
                build.verify_firmware(path,wrong)
            path.write_bytes(b"9688"+raw[4:])
            with self.assertRaisesRegex(ValueError,"SHA256"):
                build.verify_firmware(path,profile)

    def test_runtime_identity_is_target_specific_without_9688_legacy_ids(self):
        raw=bytes(range(32));records=[(4,1,build.LINK_BASE+12)]
        fingerprint=build.runtime_fingerprint("JZ4740",raw,records,64)
        old=hashlib.sha256(b"BBK-debug-runtime-identity-v1"+bytes(1)+struct.pack(
            "<IIII",4740,build.LINK_BASE,len(raw),64)+raw+struct.pack("<III",*records[0])).hexdigest()
        self.assertNotEqual(fingerprint,old)
        self.assertEqual(build.select_runtime_identity("JZ4740",fingerprint),(int(fingerprint[:8],16) or 1,False))


if __name__ == "__main__":
    unittest.main()
