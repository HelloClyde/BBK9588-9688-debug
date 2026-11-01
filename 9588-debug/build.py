#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the 9588 resident debugger and its native BDA launcher.

Relocation conversion is adapted from H1Debug's build_h1_debug.py and was
modified on 2026-10-31 for checked 9588 profiles and the 9588 BDA container.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parent
SDK_ROOT = Path(os.environ.get("BDA_SDK_ROOT", ROOT / "sdk")).resolve()
SDK_INCLUDE = SDK_ROOT / "sdk" / "include"
SDK_COMMIT = "ef3bf3b7328a3a8fa9ba48593c63285300725fb7"
TARGET = "9588"
SOCS = ("JZ4740", "JZ4730", "JZ4720")
IDENTITIES = {soc: f"9588DEBUG/1 {soc} RESIDENT" for soc in SOCS}
IDENTITY = IDENTITIES["JZ4740"]
USB_CONTROLLERS = {"JZ4740": "jz4740-musb", "JZ4730": "jz4730-fifo", "JZ4720": "jz4740-musb"}
USB_PIDS = {"JZ4740": 0xA4A9, "JZ4730": 0xA4AA, "JZ4720": 0xA4AB}


def configure_sdk(path: Path | None = None) -> None:
    global SDK_ROOT, SDK_INCLUDE
    SDK_ROOT = (path or SDK_ROOT).resolve()
    SDK_INCLUDE = SDK_ROOT / "sdk" / "include"
    if not (SDK_ROOT / "bda_packer/build.py").is_file() or not (SDK_INCLUDE / "bda_dialogs.h").is_file():
        raise ValueError("fixed SDK missing; use --sdk-root PATH or BDA_SDK_ROOT pointing to the public SDK checkout")
    sys.path.insert(0, str(SDK_ROOT))
    from bda_packer.build import ENTRY_VA as sdk_entry
    if sdk_entry != ENTRY_VA:
        raise ValueError("SDK launcher entry differs from the checked 9588 BDA ABI")


LINK_BASE = 0x81000000
ENTRY_VA = 0x81C00020
MAX_MEMORY = 0x200000
DEFAULT_PROFILE = SOURCE / "profiles/9588-jz4740.json"
SOC_PROFILES = {soc: SOURCE / f"profiles/9588-{soc.lower()}.json" for soc in SOCS}
LEGACY_NOTICE_PROFILE = SOURCE / "profiles/9588-legacy-notice.json"


def run(arguments: list[object]) -> str:
    result = subprocess.run([str(argument) for argument in arguments], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("command failed: " + str(arguments[0]) + "\n" + result.stdout + result.stderr)
    if result.stderr:
        print(result.stderr, end="")
    return result.stdout


def number(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric firmware address")
    result = int(value, 0) if isinstance(value, str) else int(value)
    if not 0 <= result <= 0xFFFFFFFF:
        raise ValueError("profile integer is outside uint32")
    return result


def profile_soc(profile: dict) -> str:
    soc = str(profile.get("soc", "")).upper()
    if soc not in SOC_PROFILES:
        raise ValueError(f"unsupported 9588 SoC: {soc}")
    return soc


def configuration(chip: str | None = None, profile_path: Path | None = None,
                  output_dir: Path | None = None) -> tuple[dict, Path, Path]:
    """Resolve an explicit single-SoC developer build."""
    chosen = chip.upper() if chip else "JZ4740"
    if chosen not in SOC_PROFILES:
        raise ValueError(f"unsupported 9588 SoC: {chosen}")
    path = (profile_path or SOC_PROFILES[chosen]).resolve()
    if not path.is_file():
        raise ValueError(f"checked 9588 {chosen} firmware profile is missing: {path}")
    profile = json.loads(path.read_text(encoding="utf-8"))
    validate_target_profile(profile)
    soc = profile_soc(profile)
    if chip and soc != chosen:
        raise ValueError(f"--chip {chip} conflicts with profile SoC {soc}")
    output = (output_dir or ROOT / "build" / f"9588-debug-{soc.lower()}").resolve()
    return profile, path, output


def resident_sources_for_soc(soc: str) -> list[Path]:
    if soc not in SOC_PROFILES:
        raise ValueError(f"unsupported 9588 SoC: {soc}")
    usb = "usb_jz4730.c" if soc == "JZ4730" else "usb.c"
    return [SOURCE / name for name in ("resident.c", "firmware.c", usb, "protocol.c", "screen.c", "exec.c")]



def validate_target_profile(profile: dict) -> None:
    if profile.get("target") != TARGET:
        raise ValueError("profile target must be 9588; 9688 and H1 profiles are incompatible")
    soc = profile_soc(profile)
    if profile.get("usb_controller") != USB_CONTROLLERS[soc]:
        raise ValueError(f"{soc} requires its independently checked USB controller {USB_CONTROLLERS[soc]}")
    if profile.get("verified") is not True:
        raise ValueError("9588 profile is incomplete: verified must be true after independent firmware checks")
    if not isinstance(profile.get("firmware_version"), str) or not profile["firmware_version"].strip():
        raise ValueError("checked firmware_version is required")
    macros = profile.get("macros", {})
    if not isinstance(macros, dict):
        raise ValueError("profile.macros must be an object")
    required = set()
    for path in (*resident_sources_for_soc(soc), SOURCE / "launcher.c", *SOURCE.glob("*.h")):
        required.update(re.findall(r"\bFW_[A-Z0-9_]+\b", path.read_text(encoding="utf-8")))
    if not number(macros.get("FW_USB_CABLE_AUX_GPIO_MASK", 0)):
        required.discard("FW_USB_CABLE_AUX_ACTIVE_LOW")  # No auxiliary input exists on this board.
    missing = required - macros.keys()
    if missing:
        raise ValueError("9588 profile lacks checked bindings: " + ", ".join(sorted(missing)))
    for name in ("FW_USB_CABLE_ACTIVE_LOW", "FW_USB_CABLE_AUX_ACTIVE_LOW"):
        if name in macros and number(macros[name]) not in (0, 1):
            raise ValueError(f"{name} must be a checked 0/1 polarity")
    if soc=="JZ4730" and number(macros["FW_USB_PULLUP_MASK"]):
        mask=number(macros["FW_USB_PULLUP_MASK"])
        if mask&(mask-1) or number(macros["FW_USB_PULLUP_ACTIVE_LOW"]) not in (0,1):
            raise ValueError("checked dedicated pullup must identify one GPIO and explicit polarity")
        function_mask=number(macros["FW_USB_PULLUP_FUNC_MASK"])
        if not function_mask or number(macros["FW_USB_PULLUP_FUNC_VALUE"])&~function_mask:
            raise ValueError("pullup function value is outside its checked mask")
        for name in ("FW_USB_PULLUP_BASE","FW_USB_PULLUP_DIR_ADDR","FW_USB_PULLUP_FUNC_ADDR","FW_USB_PULLUP_IRQ_ADDR"):
            address=number(macros[name])
            if address&3 or not 0xb0010000<=address<0xb0011000:
                raise ValueError("pullup GPIO binding must be aligned in the checked GPIO controller")
    for top, macro in (("runtime_base", "FW_RUNTIME_BASE"), ("sdram_start", "FW_SDRAM_START"),
                       ("sdram_end", "FW_SDRAM_END")):
        if top not in profile or number(profile[top]) != number(macros[macro]):
            raise ValueError(f"explicit {top} must match {macro}")
    if number(profile["runtime_base"]) != ENTRY_VA - 32:
        raise ValueError("profile does not use the checked public 9588 BDA runtime ABI")
    if number(profile["sdram_start"]) >= number(profile["sdram_end"]):
        raise ValueError("invalid explicit SDRAM bounds")
    if not (1 <= number(macros["FW_SCREEN_WIDTH"]) <= 2048 and
            1 <= number(macros["FW_SCREEN_HEIGHT"]) <= 2048):
        raise ValueError("explicit checked screen dimensions are required")
    firmware = profile.get("firmware", {})
    if (not isinstance(firmware, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(firmware.get("sha256", ""))) or
            not number(firmware.get("size", 0)) or "load_base" not in firmware or "load_base" not in profile or
            number(firmware["load_base"]) != number(profile["load_base"])):
        raise ValueError("checked firmware SHA256, size and explicit load_base are required")
    if not isinstance(profile.get("guards"), list) or not profile["guards"]:
        raise ValueError("checked firmware CRC guards are required")



def profile_header(profile: dict) -> str:
    """Emit only independently checked 9588 bindings; no original firmware bytes."""
    validate_target_profile(profile)
    macros = profile.get("macros", {})
    if not isinstance(macros, dict):
        raise ValueError("profile.macros must be an object")
    lines = ["/* Generated by 9588-debug/build.py; do not edit. */", "#ifndef RD_PROFILE_GENERATED_H",
             "#define RD_PROFILE_GENERATED_H", "#include <stdint.h>"]
    for name, value in sorted(macros.items()):
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
            raise ValueError(f"invalid profile macro: {name!r}")
        lines.append(f"#define {name} 0x{number(value):08x}u")
    aliases = {
        "RD_FW_RUNTIME_BASE": profile["runtime_base"],
        "RD_FW_SDRAM_START": profile["sdram_start"],
        "RD_FW_SDRAM_END": profile["sdram_end"],
    }
    for name, value in aliases.items():
        if name not in macros:
            lines.append(f"#define {name} 0x{number(value):08x}u")
    if number(aliases["RD_FW_RUNTIME_BASE"]) != ENTRY_VA - 32:
        raise ValueError("profile native BDA runtime base is not 0x81c00000")
    if number(aliases["RD_FW_SDRAM_START"]) >= number(aliases["RD_FW_SDRAM_END"]):
        raise ValueError("invalid profile SDRAM bounds")
    soc = profile_soc(profile)
    identity = str(profile.get("identity", IDENTITIES[soc]))
    if identity != IDENTITIES[soc]:
        raise ValueError("profile identity must be the exact 9588 target and protocol identity")
    lines += [f"#define RD_PROFILE_NAME {json.dumps(str(profile.get('name', profile.get('id', '9588'))))}",
              f"#define RD_SOC_NAME {json.dumps(soc)}",
              f"#define RD_IDENTITY {json.dumps(identity)}",
              f"#define RD_USB_PRODUCT {json.dumps('9588 USB Debug ' + soc)}",
              f"#define RD_SOC_ID {int(soc[2:])}u",
              f"#define RD_SOC_JZ4720 {int(soc == 'JZ4720')}u",
              f"#define RD_USB_CONTROLLER_MUSB {int(USB_CONTROLLERS[soc] == 'jz4740-musb')}u",
              f"#define RD_USB_CONTROLLER_FIFO {int(USB_CONTROLLERS[soc] == 'jz4730-fifo')}u",
              f"#define RD_USB_PID 0x{USB_PIDS[soc]:04x}u",
              f"#define RD_SOC_JZ4730 {int(soc == 'JZ4730')}u",
              f"#define RD_SOC_JZ4740 {int(soc == 'JZ4740')}u",
              "struct rd_fw_guard { uint32_t address,length,crc32; };"]
    guards = profile.get("guards", [])
    if not isinstance(guards, list) or not guards:
        raise ValueError("profile must contain firmware CRC guards")
    lines.append(f"#define RD_FW_GUARDS_COUNT {len(guards)}u")
    lines.append("static const struct rd_fw_guard rd_fw_guards[] = {")
    for guard in guards:
        address, length, checksum = (number(guard[key]) for key in ("address", "length", "crc32"))
        if not length or length > 0x100000 or address + length > 0x100000000:
            raise ValueError("invalid firmware guard range")
        lines.append(f"    {{0x{address:08x}u,0x{length:x}u,0x{checksum:08x}u}},")
    lines += ["};"]
    notice_guards = profile.get("notice_guards", [])
    lines.append(f"#define RD_NOTICE_GUARDS_COUNT {len(notice_guards)}u")
    lines.append("static const struct rd_fw_guard rd_notice_guards[] = {")
    for guard in notice_guards:
        address, length, checksum = (number(guard[key]) for key in ("address", "length", "crc32"))
        if not length or length > 0x100000 or address + length > 0x100000000:
            raise ValueError("invalid notice guard range")
        lines.append(f"    {{0x{address:08x}u,0x{length:x}u,0x{checksum:08x}u}},")
    lines += ["};", "#endif", ""]
    return "\n".join(lines)



def validate_profile_set(profiles: list[dict], *, require_all: bool = True) -> None:
    expected = set(SOCS)
    socs = [profile_soc(profile) for profile in profiles]
    if len(set(socs)) != len(socs) or (require_all and set(socs) != expected) or not profiles:
        raise ValueError("universal build requires exactly one checked profile per supported SoC")
    selectors = set()
    hashes = set()
    for profile in profiles:
        profile_header(profile)
        tables = tuple(number(profile["macros"][name]) for name in
                       ("FW_RUNTIME_BASE", "FW_GUI_TABLE", "FW_FS_TABLE", "FW_SYS_TABLE", "FW_MEM_TABLE", "FW_RES_TABLE"))
        guards = tuple(sorted((number(g["address"]), number(g["length"]), number(g["crc32"]))
                              for g in [*profile["guards"], *profile.get("notice_guards", [])]))
        selector = (tables, guards)
        if selector in selectors or profile["firmware"]["sha256"] in hashes:
            raise ValueError("ambiguous universal firmware profiles: duplicate selection predicate or firmware identity")
        selectors.add(selector)
        hashes.add(profile["firmware"]["sha256"])


def registered_profiles() -> list[tuple[str, Path, dict]]:
    missing = [str(path) for path in SOC_PROFILES.values() if not path.is_file()]
    if missing:
        raise ValueError("universal build is missing checked profiles: " + ", ".join(missing))
    result = [(soc, path, json.loads(path.read_text(encoding="utf-8"))) for soc, path in SOC_PROFILES.items()]
    for soc, path, profile in result:
        if profile_soc(profile) != soc:
            raise ValueError(f"profile file {path.name} does not match registered SoC {soc}")
    validate_profile_set([profile for _, _, profile in result])
    return result


def launcher_profile_header(profiles: list[dict], extra_notice_profiles: list[dict] | None = None) -> str:
    """Runtime-only bindings; each embedded resident keeps its static profile."""
    validate_profile_set(profiles, require_all=False)
    names = sorted({name for profile in profiles for name in profile["macros"]})
    lines = ["/* Generated by 9588-debug/build.py; do not edit. */",
             "#ifndef RD_LAUNCHER_PROFILE_GENERATED_H", "#define RD_LAUNCHER_PROFILE_GENERATED_H",
             "#include <stdint.h>", "struct rd_fw_guard { uint32_t address,length,crc32; };",
             "struct rd_launcher_profile {", "    uint32_t soc,guard_count,notice_guard_count;",
             "    const struct rd_fw_guard *guards,*notice_guards;"]
    for name in names:
        if not re.fullmatch(r"FW_[A-Z0-9_]+", name):
            raise ValueError(f"invalid launcher macro: {name}")
        lines.append(f"    uint32_t {name.lower()};")
    lines += ["};"]
    for index, profile in enumerate(profiles):
        # Reuse profile validation without copying the native firmware bytes.
        profile_header(profile)
        for suffix, guards in (("guards", profile["guards"]), ("notice_guards", profile.get("notice_guards", []))):
            lines.append(f"static const struct rd_fw_guard rd_launcher_{suffix}_{index}[] = {{")
            for guard in guards:
                values = [number(guard[key]) for key in ("address", "length", "crc32")]
                lines.append("    {" + ",".join(f"0x{value:08x}u" for value in values) + "},")
            lines += ["};"]
    lines += [f"#define RD_LAUNCHER_PROFILE_COUNT {len(profiles)}u",
              "static const struct rd_launcher_profile rd_launcher_profiles[] = {"]
    for index, profile in enumerate(profiles):
        lines += ["    {", f"        .soc={profile_soc(profile)[2:]}u,",
                  f"        .guard_count={len(profile['guards'])}u, .guards=rd_launcher_guards_{index},",
                  f"        .notice_guard_count={len(profile.get('notice_guards', []))}u, .notice_guards=rd_launcher_notice_guards_{index},"]
        lines += [f"        .{name.lower()}=0x{number(profile['macros'].get(name, 0)):08x}u," for name in names]
        lines += ["    },"]
    lines += ["};", "struct rd_notice_profile {",
              "    uint32_t runtime_base,gui_table,count; const struct rd_fw_guard *guards;", "};"]
    extra_notice_profiles = extra_notice_profiles or []
    for index, profile in enumerate(extra_notice_profiles):
        runtime, gui = number(profile["runtime_base"]), number(profile["gui_table"])
        if runtime != ENTRY_VA - 32 or gui & 3 or not 0x80004000 <= gui < runtime:
            raise ValueError("invalid extra notice runtime or GUI table")
        guards = profile.get("notice_guards", [])
        if not guards:
            raise ValueError("extra notice profile must have checked guards")
        lines.append(f"static const struct rd_fw_guard rd_extra_notice_guards_{index}[] = {{")
        for guard in guards:
            address, length, checksum = (number(guard[key]) for key in ("address", "length", "crc32"))
            if not 0x80004000 <= address < runtime or not 0 < length <= 0x100000 or address + length > runtime:
                raise ValueError("invalid extra notice guard range")
            lines.append(f"    {{0x{address:08x}u,0x{length:x}u,0x{checksum:08x}u}},")
        lines += ["};"]
    lines += [f"#define RD_EXTRA_NOTICE_PROFILE_COUNT {len(extra_notice_profiles)}u"]
    if extra_notice_profiles:
        lines += ["static const struct rd_notice_profile rd_extra_notice_profiles[] = {"]
        for index, profile in enumerate(extra_notice_profiles):
            lines.append(f"    {{0x{number(profile['runtime_base']):08x}u,0x{number(profile['gui_table']):08x}u,"
                         f"{len(profile['notice_guards'])}u,rd_extra_notice_guards_{index}}},")
        lines += ["};"]
    lines += ["extern const struct rd_launcher_profile *rd_selected_profile;"]
    lines += [f"#define {name} (rd_selected_profile->{name.lower()})" for name in names]
    lines += ["#define RD_FW_RUNTIME_BASE FW_RUNTIME_BASE", "#define RD_FW_SDRAM_START FW_SDRAM_START",
              "#define RD_FW_SDRAM_END FW_SDRAM_END", "#endif", ""]
    return "\n".join(lines)


def verify_firmware(path: Path, profile: dict) -> dict:
    """Verify a supplied raw image or the profile's exact 64-byte wrapper."""
    supplied = path.read_bytes()
    digest = hashlib.sha256(supplied).hexdigest()
    expected = profile["firmware"]
    raw = supplied
    form = "raw"
    if digest != expected["sha256"]:
        packed = profile.get("packed_firmware", {})
        if len(supplied) < 64 or digest != packed.get("sha256") or len(supplied) != number(packed.get("size", 0)):
            raise ValueError("firmware SHA256 does not match the selected raw or wrapped profile")
        raw, form = supplied[64:], "64-byte-wrapper"
    raw_digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != number(expected["size"]) or raw_digest != expected["sha256"]:
        raise ValueError("raw firmware payload SHA256/size mismatch")
    load_base = number(expected.get("load_base", profile["load_base"]))
    checked = []
    for guard in profile["guards"]:
        address, length = number(guard["address"]), number(guard["length"])
        offset = address - load_base
        if offset < 0 or offset + length > len(raw):
            raise ValueError("firmware CRC guard lies outside the raw image")
        checksum = zlib.crc32(raw[offset:offset + length]) & 0xFFFFFFFF
        if checksum != number(guard["crc32"]):
            raise ValueError(f"firmware CRC guard mismatch: {guard.get('name', hex(address))}")
        checked.append({"name": guard.get("name", hex(address)), "address": address,
                        "length": length, "crc32": f"{checksum:08x}", "matched": True})
    notice_checked = []
    for guard in profile.get("notice_guards", []):
        address, length = number(guard["address"]), number(guard["length"])
        offset = address - load_base
        if offset < 0 or not length or offset + length > len(raw):
            raise ValueError("notice CRC guard lies outside the raw image")
        checksum = zlib.crc32(raw[offset:offset + length]) & 0xFFFFFFFF
        if checksum != number(guard["crc32"]):
            raise ValueError(f"notice CRC guard mismatch: {guard.get('name', hex(address))}")
        notice_checked.append({"name": guard.get("name", hex(address)), "address": address,
                               "length": length, "crc32": f"{checksum:08x}", "matched": True})
    return {"format": form, "supplied_sha256": digest, "supplied_size": len(supplied),
            "raw_sha256": raw_digest, "raw_size": len(raw), "guard_count": len(checked), "guards": checked,
            "notice_guard_count": len(notice_checked), "notice_guards": notice_checked}


def verify_notice_firmware(path: Path, profile: dict) -> dict:
    """Check a legacy GUI fingerprint without claiming private runtime support."""
    supplied = path.read_bytes()
    digest = hashlib.sha256(supplied).hexdigest()
    expected = profile["firmware"]
    if digest != expected["sha256"] or len(supplied) != number(expected["size"]):
        raise ValueError("legacy notice firmware SHA256/size mismatch")
    guards = profile.get("notice_guards", [])
    if not guards:
        raise ValueError("legacy notice firmware needs nonempty guards")
    base = number(expected.get("load_base", 0x80004000))
    matches = []
    # The original local backup may be a raw image or a 64-byte wrapper.
    # Select only the unique form matching every independently recorded CRC.
    for prefix, form in ((0, "raw"), (64, "64-byte-wrapper")):
        raw = supplied[prefix:]
        checked = []
        for guard in guards:
            address, length = number(guard["address"]), number(guard["length"])
            offset = address - base
            if offset < 0 or not length or offset + length > len(raw):
                break
            checksum = zlib.crc32(raw[offset:offset + length]) & 0xFFFFFFFF
            if checksum != number(guard["crc32"]):
                break
            checked.append({"name": guard.get("name", hex(address)), "address": address,
                            "length": length, "crc32": f"{checksum:08x}", "matched": True})
        if len(checked) == len(guards):
            matches.append({"format": form, "supplied_sha256": digest, "supplied_size": len(supplied),
                            "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_size": len(raw),
                            "notice_guard_count": len(checked), "notice_guards": checked,
                            "notice_only": True, "private_runtime_supported": False})
    if len(matches) != 1:
        raise ValueError("legacy notice guards do not uniquely match a raw or wrapped payload")
    return matches[0]


def relocation_records(elf_path: Path, raw: bytes, link_base: int = LINK_BASE) -> tuple[list[tuple[int, int, int]], dict[str, int]]:
    """Convert allocated MIPS ELF REL records to checked runtime fixups.

    Signed LO16 immediates carry into HI16. R_MIPS_32 and R_MIPS_26 point
    inside this allocation; absolute firmware/MMIO symbols stay unchanged.
    """
    from elftools.elf.elffile import ELFFile
    from elftools.elf.relocation import RelocationSection

    records: list[tuple[int, int, int]] = []
    with elf_path.open("rb") as stream:
        elf = ELFFile(stream)
        if elf.elfclass != 32 or not elf.little_endian or elf["e_machine"] != "EM_MIPS":
            raise ValueError("resident must be ELF32 little-endian MIPS")
        symbols = elf.get_section_by_name(".symtab")
        if symbols is None:
            raise ValueError("resident ELF has no symbol table")
        values = {symbol.name: symbol["st_value"] for symbol in symbols.iter_symbols() if symbol.name}
        if "__bss_end" not in values:
            raise ValueError("resident ELF has no memory boundary")
        memory_size = values["__bss_end"] - link_base
        if not 0 < len(raw) <= memory_size <= MAX_MEMORY:
            raise ValueError("resident image or memory size outside allocation bounds")
        for section in elf.iter_sections():
            if not isinstance(section, RelocationSection):
                continue
            target = elf.get_section(section["sh_info"])
            if not target["sh_flags"] & 2:
                continue
            if section["sh_type"] != "SHT_REL":
                raise ValueError("MIPS RELA relocations are unsupported")
            symtab = elf.get_section(section["sh_link"])
            pending: dict[int, list[tuple[int, int]]] = {}
            for rel in section.iter_relocations():
                kind, index = rel["r_info_type"], rel["r_info_sym"]
                if kind == 0:
                    continue
                symbol = symtab.get_symbol(index)
                shndx = symbol["st_shndx"]
                if shndx == "SHN_ABS":
                    continue
                if not isinstance(shndx, int) or not elf.get_section(shndx)["sh_flags"] & 2:
                    raise ValueError(f"unresolved/nonallocated relocation: {symbol.name} {shndx}")
                offset = rel["r_offset"] - link_base
                if offset < 0 or offset + 4 > len(raw) or offset % 4:
                    raise ValueError("relocation outside resident image")
                word = struct.unpack_from("<I", raw, offset)[0]
                if kind == 2:  # R_MIPS_32
                    records.append((offset, 1, word))
                elif kind == 4:  # R_MIPS_26
                    address = ((word & 0x03FFFFFF) << 2) | ((rel["r_offset"] + 4) & 0xF0000000)
                    records.append((offset, 2, address))
                elif kind == 5:  # R_MIPS_HI16: one LO may serve several HIs.
                    pending.setdefault(index, []).append((offset, word & 0xFFFF))
                elif kind == 6:  # R_MIPS_LO16
                    low = word & 0xFFFF
                    signed_low = low if low < 0x8000 else low - 0x10000
                    highs = pending.pop(index, [])
                    for high_offset, high in highs:
                        address = ((high << 16) + signed_low) & 0xFFFFFFFF
                        records.append((high_offset, 3, address))
                    # A register may be reused by later LO16 instructions. Only
                    # the low bits relocate, so select its first in-image alias.
                    low_alias = link_base + ((low - link_base) & 0xFFFF)
                    records.append((offset, 4, low_alias))
                elif kind == 10:  # PC16: both ends move by the same delta.
                    continue
                else:
                    raise ValueError(f"unsupported MIPS relocation {kind} at {offset:#x}")
            if pending:
                raise ValueError("unpaired HI16 relocations")
    validate_records(raw, records, memory_size, link_base)
    return sorted(records), values


def validate_records(raw: bytes, records: list[tuple[int, int, int]], memory_size: int,
                     link_base: int = LINK_BASE) -> None:
    if not 0 < len(raw) <= memory_size <= MAX_MEMORY:
        raise ValueError("invalid resident image or memory size")
    seen: set[int] = set()
    for offset, kind, value in records:
        if offset % 4 or offset < 0 or offset + 4 > len(raw):
            raise ValueError("relocation outside resident image")
        if offset in seen:
            raise ValueError("duplicate resident relocation")
        seen.add(offset)
        if kind not in (1, 2, 3, 4):
            raise ValueError("unknown runtime relocation type")
        if not link_base <= value <= link_base + memory_size:
            raise ValueError("relocation target outside resident allocation")
        if kind == 2 and value & 3:
            raise ValueError("unaligned jump relocation")


def relocate_image(raw: bytes, records: list[tuple[int, int, int]], base: int, memory_size: int,
                   link_base: int = LINK_BASE) -> bytes:
    """Host reference model, tested against independently linked ELF images."""
    validate_records(raw, records, memory_size, link_base)
    if base & 31 or not 0x80000000 <= base <= 0x82000000 - memory_size:
        raise ValueError("invalid destination RAM range or alignment")
    data = bytearray(raw)
    delta = base - link_base
    for offset, kind, target in records:
        value = (target + delta) & 0xFFFFFFFF
        word = struct.unpack_from("<I", raw, offset)[0]
        if kind == 1:
            word = value
        elif kind == 2:
            if (base + offset + 4) & 0xF0000000 != value & 0xF0000000:
                raise ValueError("jump crosses MIPS 256 MiB region")
            word = (word & 0xFC000000) | ((value >> 2) & 0x03FFFFFF)
        elif kind == 3:
            word = (word & 0xFFFF0000) | (((value + 0x8000) >> 16) & 0xFFFF)
        else:
            word = (word & 0xFFFF0000) | (value & 0xFFFF)
        struct.pack_into("<I", data, offset, word)
    return bytes(data)


def find_toolchain(directory: Path | None = None) -> tuple[Path, Path]:
    prefix = os.environ.get("BDA_TOOLCHAIN_PREFIX")
    if directory is None and prefix:
        for suffix in (".exe", ""):
            gcc, objcopy = Path(prefix + "gcc" + suffix), Path(prefix + "objcopy" + suffix)
            if gcc.is_file() and objcopy.is_file():
                return gcc.resolve(), objcopy.resolve()
    candidates = [directory] if directory else [SDK_ROOT / ".toolchain" / "bin"]
    for candidate in candidates:
        if candidate is None:
            continue
        for suffix in (".exe", ""):
            gcc, objcopy = candidate / ("mipsel-none-elf-gcc" + suffix), candidate / ("mipsel-none-elf-objcopy" + suffix)
            if gcc.is_file() and objcopy.is_file():
                return gcc.resolve(), objcopy.resolve()
    gcc_path = shutil.which("mipsel-none-elf-gcc")
    objcopy_path = shutil.which("mipsel-none-elf-objcopy")
    if directory is None and gcc_path and objcopy_path:
        return Path(gcc_path), Path(objcopy_path)
    raise RuntimeError("MIPS toolchain not found; run sdk/scripts/setup_toolchain.ps1 or use --toolchain PATH/TO/bin")


def runtime_fingerprint(soc: str, raw: bytes, records: list[tuple[int, int, int]], memory_size: int) -> str:
    if soc not in SOC_PROFILES:
        raise ValueError("unknown runtime SoC")
    payload = b"BBK9588-debug-runtime-identity-v1\0" + struct.pack(
        "<IIII", int(soc[2:]), LINK_BASE, len(raw), memory_size)
    payload += raw + b"".join(struct.pack("<III", *record) for record in sorted(records))
    return hashlib.sha256(payload).hexdigest()


def select_runtime_identity(soc: str, fingerprint: str) -> tuple[int, bool]:
    if soc not in SOC_PROFILES:
        raise ValueError("unsupported 9588 runtime SoC")
    return int(fingerprint[:8], 16) or 1, False


def compile_link(sources: list[Path], name: str, script: Path, output: Path, gcc: Path, objcopy: Path,
                 flags: list[object], reloc: bool = False) -> tuple[Path, bytes]:
    output.mkdir(parents=True, exist_ok=True)
    objects = []
    for index, source in enumerate(sources):
        obj = output / f"{name}-{index}-{source.stem}.o"
        run([gcc, *flags, "-c", source, "-o", obj])
        objects.append(obj)
    elf = output / (name + ".elf")
    raw = output / (name + ".bin")
    run([gcc, *flags, "-nostdlib", "-Wl,--build-id=none", "-Wl,--gc-sections",
         *(["-Wl,--emit-relocs"] if reloc else []), f"-Wl,-T,{script}",
         f"-Wl,-Map,{output / (name + '.map')}", "-o", elf, *objects, "-lgcc"])
    run([objcopy, "-O", "binary", elf, raw])
    return elf, raw.read_bytes()


def image_header(raw: bytes, records: list[tuple[int, int, int]], memory_size: int, image_id: int,
                 soc: str = "JZ4740") -> str:
    if soc not in SOC_PROFILES:
        raise ValueError(f"unsupported image SoC: {soc}")
    lines = ["/* Generated by 9588-debug/build.py; do not edit. */", "#include <stdint.h>",
             f"#define RD_IMAGE_LINK_BASE 0x{LINK_BASE:08x}u", f"#define RD_IMAGE_SIZE {len(raw)}u",
             f"#define RD_MEMORY_SIZE {memory_size}u", f"#define RD_IMAGE_ID 0x{image_id:08x}u",
             f"#define RD_RELOC_COUNT {len(records)}u", "static const uint8_t rd_image[] = {"]
    lines += [",".join(f"0x{byte:02x}" for byte in raw[index:index + 24]) + ","
              for index in range(0, len(raw), 24)]
    lines += ["};", "struct rd_reloc { uint32_t offset,type,value; };",
              "static const struct rd_reloc rd_relocations[] = {"]
    lines += [f"{{0x{offset:x}u,{kind}u,0x{value:08x}u}}," for offset, kind, value in records]
    lines += ["};", "struct rd_image_descriptor {",
              "    uint32_t soc,link_base,image_size,memory_size,image_id,reloc_count;",
              "    const uint8_t *data; const struct rd_reloc *relocations;", "};",
              "static const struct rd_image_descriptor rd_images[] = {",
              f"    {{{soc[2:]}u,RD_IMAGE_LINK_BASE,RD_IMAGE_SIZE,RD_MEMORY_SIZE,RD_IMAGE_ID,RD_RELOC_COUNT,rd_image,rd_relocations}},",
              "};", "#define RD_IMAGES_COUNT 1u", ""]
    return "\n".join(lines)


def universal_image_header(components: list[dict]) -> str:
    lines = ["/* Generated by 9588-debug/build.py; do not edit. */", "#include <stdint.h>",
             "struct rd_reloc { uint32_t offset,type,value; };",
             "struct rd_image_descriptor {",
             "    uint32_t soc,link_base,image_size,memory_size,image_id,reloc_count;",
             "    const uint8_t *data; const struct rd_reloc *relocations;", "};"]
    for index, component in enumerate(components):
        lines.append(f"static const uint8_t rd_image_{index}[] = {{")
        raw = component["resident"]
        lines += [",".join(f"0x{byte:02x}" for byte in raw[offset:offset + 24]) + ","
                  for offset in range(0, len(raw), 24)]
        lines += ["};", f"static const struct rd_reloc rd_relocations_{index}[] = {{"]
        lines += [f"{{0x{offset:x}u,{kind}u,0x{value:08x}u}}," for offset, kind, value in component["records"]]
        lines += ["};"]
    lines += ["static const struct rd_image_descriptor rd_images[] = {"]
    for index, component in enumerate(components):
        report = component["report"]
        lines.append(f"    {{{report['soc'][2:]}u,0x{LINK_BASE:08x}u,{len(component['resident'])}u,"
                     f"{report['resident_memory_bytes']}u,0x{report['image_id']}u,{len(component['records'])}u,"
                     f"rd_image_{index},rd_relocations_{index}}},")
    lines += ["};", f"#define RD_IMAGES_COUNT {len(components)}u", ""]
    return "\n".join(lines)


def pack_bda(payload: bytes, menu_title: str = "9588调试") -> bytes:
    from bda_packer.build import ENTRY_OFFSET, ICON_START, ICON_SIZES, build_icons
    from bda_packer.header import BdaHeaderFields, write_header, verify
    data = bytearray(ENTRY_OFFSET)
    resources = build_icons(None, (0, 0, 0))
    if len(resources) != ENTRY_OFFSET - ICON_START:
        raise ValueError("9588 SDK icon layout changed")
    data[ICON_START:ENTRY_OFFSET] = resources
    data += payload
    data += bytes((-len(data)) & 3)
    if len(menu_title.encode("gbk")) > 15:
        raise ValueError("BDA menu title exceeds 15 GBK bytes")
    write_header(data, BdaHeaderFields(category=9, file_size_minus_4=len(data)-4,
        entry_offset=ENTRY_OFFSET, icon_start=ICON_START,
        icon0_size=ICON_SIZES[0], icon1_size=ICON_SIZES[1],
        icon2_size=ICON_SIZES[2], icon3_size=ICON_SIZES[3]), menu_title)
    checked = verify(data)
    if (checked["title"] != menu_title or checked["category"] != 9 or not checked["checksum_ok"] or
            checked["file_size_minus_4"] != len(data)-4):
        raise ValueError("BDA container verification failed")
    return bytes(data)


def build_variant(profile: dict, profile_path: Path, output: Path, gcc: Path, objcopy: Path,
                  firmware_path: Path | None = None, *, emit_bda: bool = True) -> dict:
    soc = profile_soc(profile)
    output.mkdir(parents=True, exist_ok=True)
    generated = profile_header(profile)
    firmware_verification = verify_firmware(firmware_path, profile) if firmware_path else None
    (output / "profile_generated.h").write_text(generated, encoding="ascii")
    compiler = run([gcc, "--version"]).splitlines()[0]
    resident_sources = resident_sources_for_soc(soc)
    inputs = sorted(set(resident_sources + list(SOURCE.glob("*.h")) + list(SOURCE.glob("*.ld")) +
                        [SOURCE / "launcher.c", Path(__file__), profile_path,
                         *(SDK_ROOT / "bda_packer" / name for name in ("__init__.py", "build.py", "header.py", "vx_icon.py")),
                         *(SDK_INCLUDE / name for name in ("bda_types.h", "bda_dialogs.h", "bda_memory.h", "bda/detail/runtime.h"))]))
    def source_label(path: Path) -> str:
        if path.is_relative_to(ROOT):
            return path.relative_to(ROOT).as_posix()
        if path.is_relative_to(SDK_ROOT):
            return "sdk/" + path.relative_to(SDK_ROOT).as_posix()
        return "external-profile/" + path.name
    source_hashes = {source_label(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    flags: list[object] = ["-EL", "-march=mips32", "-mabi=32", "-msoft-float", "-mno-abicalls", "-G0", "-fno-pic",
                          "-O2", "-ffreestanding", "-fno-builtin", "-fno-stack-protector", "-ffunction-sections",
                          "-fdata-sections", "-fno-strict-aliasing", "-g0", "-Wall", "-Wextra", "-Werror",
                          "-Wno-misleading-indentation", f"-ffile-prefix-map={ROOT}=bbk9588-debug",
                          "-I", SOURCE, "-I", output, "-I", SDK_INCLUDE]
    zero_elf, zero_raw = compile_link(resident_sources, "resident", SOURCE / "resident.ld",
                                    output / "identity", gcc, objcopy, [*flags, "-DRD_BUILD_ID=0u"], True)
    zero_records, zero_symbols = relocation_records(zero_elf, zero_raw)
    zero_memory_size = zero_symbols["__bss_end"] - LINK_BASE
    fingerprint = runtime_fingerprint(soc, zero_raw, zero_records, zero_memory_size)
    image_id, reused_identity = select_runtime_identity(soc, fingerprint)
    flags = [*flags, f"-DRD_BUILD_ID=0x{image_id:08x}u"]
    elf, resident = compile_link(resident_sources, "resident", SOURCE / "resident.ld", output, gcc, objcopy, flags, True)
    resident_sha256 = hashlib.sha256(resident).hexdigest()
    records, symbols = relocation_records(elf, resident)
    memory_size = symbols["__bss_end"] - LINK_BASE
    relocation_sha256 = hashlib.sha256(b"".join(
        struct.pack("<III", *record) for record in sorted(records))).hexdigest()
    if symbols.get("rd_control") != LINK_BASE or symbols.get("__image_end") != LINK_BASE + len(resident):
        raise ValueError("resident header/image boundaries do not match raw output")
    (output / "resident_image.h").write_text(image_header(resident, records, memory_size, image_id, soc), encoding="ascii")
    launcher_elf, payload = compile_link([SOURCE / "launcher.c", SOURCE / "firmware.c"], "launcher",
                                         SOURCE / "launcher.ld", output, gcc, objcopy, flags)
    from elftools.elf.elffile import ELFFile
    with launcher_elf.open("rb") as stream:
        image = ELFFile(stream)
        launcher_symbols = {symbol.name: symbol["st_value"]
                            for symbol in image.get_section_by_name(".symtab").iter_symbols() if symbol.name}
        if image["e_entry"] != ENTRY_VA or launcher_symbols.get("bda_main") != ENTRY_VA:
            raise ValueError("launcher entry address mismatch")
    menu_title = "9588调试"
    bda = pack_bda(payload, menu_title)
    destination = output / "Debug9588.bda"
    if emit_bda:
        destination.write_bytes(bda)
    legacy_alias = output / "Debug9588.bda"
    if emit_bda:
        legacy_alias.write_bytes(bda)
    digest = hashlib.sha256(bda).hexdigest()
    report = {"version": "9588-debug-v1", "image_id": f"{image_id:08x}", "sha256": digest, "size": len(bda),
              "profile": profile.get("id", profile.get("name")), "soc": soc,
              "menu_title": menu_title, "binary": destination.name, "binary_aliases": [],
              "target": TARGET, "mode": "single", "usb_source": "usb_jz4730.c" if soc == "JZ4730" else "usb.c",
              "firmware": profile.get("firmware", {}), "profile_file": source_label(profile_path),
              "compiler": compiler, "entry_va": ENTRY_VA, "resident_link_base": LINK_BASE,
              "resident_image_bytes": len(resident), "resident_memory_bytes": memory_size,
              "resident_bss_bytes": symbols["__bss_end"] - symbols["__bss_start"], "relocations": len(records),
              "hardware_verified": False, "full_firmware_verified": False, "source_sha256": source_hashes,
              "sdk_commit": SDK_COMMIT,
              "runtime_identity": {"algorithm": "canonical-zero-id-v1", "fingerprint": fingerprint,
                  "legacy_compatible_identity": False, "resident_sha256": resident_sha256,
                  "zero_id_resident_sha256": hashlib.sha256(zero_raw).hexdigest()},
              "firmware_profile_verified": firmware_verification is not None,
              "firmware_verification": firmware_verification,
              "resident_objects": [f"resident-{index}-{source.stem}.o" for index, source in enumerate(resident_sources)],
              "symbols": symbols, "launcher_symbols": launcher_symbols,
              "stopped_memory_policy": "Allocation and sleeping worker retained until reboot; reused by the same build."}
    if not emit_bda:
        report.update({"component_only": True, "binary": None, "binary_aliases": [],
                       "launcher_payload_sha256": hashlib.sha256(payload).hexdigest()})
        report.pop("sha256")
        report.pop("size")
    (output / "build.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if emit_bda:
        destination.with_suffix(".build.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "relocations.json").write_text(json.dumps({"base": LINK_BASE, "memory_size": memory_size,
                                                         "records": records, "symbols": symbols}, indent=2) + "\n",
                                             encoding="utf-8")
    for path in ((destination, legacy_alias) if emit_bda else ()):
        path.with_suffix(".bda.sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    if emit_bda:
        (output / "verification.json").write_text(json.dumps({"bda_sha256": digest, "hardware_verified": False,
                                                              "full_firmware_verified": False,
                                                              "status": "built; verification pending"}, indent=2) + "\n",
                                                  encoding="utf-8")
    else:
        (output / "verification.json").unlink(missing_ok=True)
    if emit_bda:
        print(f"PASS {destination}: {len(bda)} bytes, resident {memory_size} bytes RAM, {len(records)} relocations")
        print(f"SHA256 {digest}")
    return {"resident": resident, "records": records, "report": report, "output": output, "profile": profile}


def build_universal(output: Path, gcc: Path, objcopy: Path,
                    firmware_paths: dict[str, Path | None]) -> dict:
    registered = registered_profiles()
    output.mkdir(parents=True, exist_ok=True)
    extra_notice_profiles = []
    components = []
    for soc, path, profile in registered:
        component = build_variant(profile, path, output / "components" / soc.lower(), gcc, objcopy,
                                  firmware_paths.get(soc), emit_bda=False)
        components.append(component)
    profiles = [component["profile"] for component in components]
    (output / "launcher_profile_generated.h").write_text(launcher_profile_header(profiles, extra_notice_profiles), encoding="ascii")
    (output / "resident_image.h").write_text(universal_image_header(components), encoding="ascii")
    source_hashes = {name: digest for component in components for name, digest in component["report"]["source_sha256"].items()}
    compiler = components[0]["report"]["compiler"]
    identity = json.dumps({"source_sha256": source_hashes, "compiler": compiler,
                           "mode": "universal-v2"}, sort_keys=True).encode()
    image_id = hashlib.sha256(identity).hexdigest()[:8]
    flags: list[object] = ["-EL", "-march=mips32", "-mabi=32", "-msoft-float", "-mno-abicalls", "-G0", "-fno-pic",
                          "-O2", "-ffreestanding", "-fno-builtin", "-fno-stack-protector", "-ffunction-sections",
                          "-fdata-sections", "-fno-strict-aliasing", "-g0", "-Wall", "-Wextra", "-Werror",
                          "-Wno-misleading-indentation", f"-ffile-prefix-map={ROOT}=bbk9588-debug",
                          "-DRD_UNIVERSAL_LAUNCHER=1", "-I", SOURCE, "-I", output, "-I", SDK_INCLUDE]
    elf, payload = compile_link([SOURCE / "launcher.c", SOURCE / "firmware.c"], "launcher",
                                SOURCE / "launcher.ld", output, gcc, objcopy, flags)
    from elftools.elf.elffile import ELFFile
    with elf.open("rb") as stream:
        image = ELFFile(stream)
        symbols = {symbol.name: symbol["st_value"] for symbol in image.get_section_by_name(".symtab").iter_symbols() if symbol.name}
        if image["e_entry"] != ENTRY_VA or symbols.get("bda_main") != ENTRY_VA:
            raise ValueError("universal launcher entry address mismatch")
    menu_title = "9588调试"
    bda = pack_bda(payload, menu_title)
    destination = output / "Debug9588.bda"
    destination.write_bytes(bda)
    digest = hashlib.sha256(bda).hexdigest()
    component_reports = [{"soc": component["report"]["soc"],
                          "profile_file": component["report"]["profile_file"],
                          "usb_controller": component["profile"]["usb_controller"],
                          "directory": component["output"].relative_to(output).as_posix(),
                          "image_id": component["report"]["image_id"],
                          "resident_image_bytes": len(component["resident"]),
                          "resident_memory_bytes": component["report"]["resident_memory_bytes"],
                          "relocations": len(component["records"]),
                          "firmware_verification": component["report"]["firmware_verification"]}
                         for component in components]
    report = {"version": "9588-debug-v2", "target": TARGET, "mode": "universal", "soc": "UNIVERSAL",
              "supported_socs": [profile_soc(profile) for profile in profiles],
              "profile": "9588-universal", "binary": destination.name,
              "extra_notice_profiles": [profile.get("name", "legacy firmware") for profile in extra_notice_profiles],
              "menu_title": menu_title, "image_id": image_id, "sha256": digest, "size": len(bda),
              "compiler": compiler, "entry_va": ENTRY_VA, "resident_link_base": LINK_BASE,
              "components": component_reports, "launcher_symbols": symbols, "source_sha256": source_hashes,
              "sdk_commit": SDK_COMMIT,
              "runtime_identity_policy": "canonical-zero-id-v1; target-specific fingerprints; no historical 9688 ID mapping",
              "firmware_profile_verified": all(component["report"]["firmware_profile_verified"] for component in components),
              "hardware_verified": False, "full_firmware_verified": False,
              "stopped_memory_policy": "Only the selected chip resident is allocated; retained until reboot and reused by the same build."}
    for path in (output / "build.json", destination.with_suffix(".build.json")):
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    destination.with_suffix(".bda.sha256").write_text(f"{digest}  {destination.name}\n", encoding="ascii")
    (output / "verification.json").write_text(json.dumps({"bda_sha256": digest, "hardware_verified": False,
                                                          "full_firmware_verified": False,
                                                          "status": "built; verification pending"}, indent=2) + "\n", encoding="utf-8")
    # Remove only obsolete generated top-level single-chip outputs from the
    # former default build. The checked source tree and component files remain.
    obsolete = ["resident.elf", "resident.bin", "resident.map", "relocations.json", "profile_generated.h",
                "Debug9588-JZ4740.bda", "Debug9588-JZ4740.bda.sha256", "Debug9588-JZ4740.build.json"]
    for name in obsolete:
        (output / name).unlink(missing_ok=True)
    for path in output.glob("resident-*.o"):
        path.unlink()
    print(f"PASS {destination}: {len(bda)} bytes; checked target profile selection")
    print(f"SHA256 {digest}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=Path)
    parser.add_argument("--toolchain", type=Path)
    parser.add_argument("--chip", choices=tuple(soc.lower() for soc in SOCS), help="explicit single-SoC developer build; default is universal")
    parser.add_argument("--profile", type=Path, help="override a single-SoC checked profile")
    parser.add_argument("--firmware", type=Path, help="local raw/wrapped ROM for an explicit single-SoC build")
    for soc in SOCS:
        parser.add_argument("--firmware-" + soc.lower(), type=Path, help="optional checked local ROM for the universal build")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--universal", action="store_true", help="default; retained for CLI compatibility")
    args = parser.parse_args()
    try:
        single = bool(args.chip or args.profile)
        firmware_paths = {soc: getattr(args, "firmware_" + soc.lower()) for soc in SOCS}
        if single:
            if args.universal or any(firmware_paths.values()):
                parser.error("single-SoC builds use --firmware, not --universal or --firmware-jzXXXX")
            profile, profile_path, output = configuration(args.chip, args.profile, args.output_dir)
        else:
            if args.firmware:
                parser.error("universal builds use --firmware-jz4740/4730/4720; --firmware requires --chip")
            registered_profiles()  # Reject missing/ambiguous profiles before creating outputs.
        configure_sdk(args.sdk_root)
        gcc, objcopy = find_toolchain(args.toolchain)
        if single:
            build_variant(profile, profile_path, output, gcc, objcopy, args.firmware)
        else:
            build_universal((args.output_dir or ROOT / "build/9588-debug").resolve(), gcc, objcopy, firmware_paths)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
