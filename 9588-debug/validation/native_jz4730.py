#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Execute original BBK 9588 JZ4730 V1.30 instructions in a bounded MIPS model.

This validation utility contains no original ROM image or copyrighted firmware
payload. Supply your own matching C200knl.bin or raw payload. Only addresses,
register constants, expected results and fingerprints are distributed here.
There is no physical USB, serial, block-device or hardware access.

When installed as 9588-debug/validation/native_jz4730_public.py, the default
profile is ../profiles/9588-jz4730.json. --profile overrides that location.
An explicit --output is required; source directories receive no implicit report.

Example:
  python native_jz4730_public.py C200knl.bin --output /tmp/jz4730-native.json

Requires the Unicorn Python package. MMIO is simple register storage, not an
implementation of USB host ACK timing, GPIO wiring or LCD electrical behavior.
Successful execution must not be represented as physical-device validation.
"""
from pathlib import Path
import argparse
import hashlib
import json
import struct
import zlib
from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN
from unicorn.mips_const import UC_MIPS_REG_SP, UC_MIPS_REG_RA, UC_MIPS_REG_V0, UC_MIPS_REG_A0, UC_MIPS_REG_CP0_STATUS, UC_MIPS_REG_PC

BASE = 0x80004000
STOP = 0x81BFF000


class Model:
    def __init__(self, raw):
        self.u = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        self.u.mem_map(0, 0x02000000)
        self.u.mem_write(0x4000, raw)
        self.u.reg_write(UC_MIPS_REG_CP0_STATUS, 0x10000000)
        self.regs = {}
        self.writes = []
        # Pure mmio_map callbacks avoid Unicorn 2.1.4's MEM_WRITE-hook/JAL
        # delay-slot bug. No target instruction is changed for that issue.
        for address, size in [(0x10000000, 0x20000), (0x13000000, 0x1000),
                              (0x13010000, 0x1000), (0x13040000, 0x1000),
                              (0x13050000, 0x1000)]:
            self.u.mmio_map(address, size, self.read_mmio, address,
                            self.write_mmio, address)
        for irq in range(177):
            self.put(0x80479634 + irq * 8, 0x80004D74)

    def read_mmio(self, uc, offset, size, base):
        addr = base + offset
        word = self.regs.get(addr & ~3, 0)
        return (word >> ((addr & 3) * 8)) & ((1 << (size * 8)) - 1)

    def write_mmio(self, uc, offset, size, value, base):
        addr = base + offset
        aligned = addr & ~3
        shift = (addr & 3) * 8
        mask = ((1 << (size * 8)) - 1) << shift
        self.regs[aligned] = (self.regs.get(aligned, 0) & ~mask) | ((value << shift) & mask)
        self.writes.append((hex(addr | 0xA0000000), size, hex(value)))

    def put(self, address, value):
        self.u.mem_write(address & 0x1FFFFFFF, struct.pack('<I', value))

    def get(self, address):
        return struct.unpack('<I', self.u.mem_read(address & 0x1FFFFFFF, 4))[0]

    def stub(self, address, result=0):
        # Explicit external boundary: jr ra; addiu v0, zero, result.
        self.u.mem_write(address & 0x1FFFFFFF, struct.pack('<II', 0x03E00008, 0x24020000 | (result & 0xFFFF)))

    def call(self, address, arg=0):
        self.u.reg_write(UC_MIPS_REG_SP, 0x81B00000)
        self.u.reg_write(UC_MIPS_REG_RA, STOP)
        self.u.reg_write(UC_MIPS_REG_A0, arg)
        self.u.emu_start(address, STOP, count=2500000)
        assert self.u.reg_read(UC_MIPS_REG_PC) == STOP, hex(self.u.reg_read(UC_MIPS_REG_PC))
        assert self.u.reg_read(UC_MIPS_REG_SP) == 0x81B00000
        return self.u.reg_read(UC_MIPS_REG_V0)


def run(source, profile_path):
    data = source.read_bytes()
    raw = data[64:] if len(data) == 4673208 else data
    expected_sha = 'c7a4ba34d5a4c006f88b7e9e0c0a991b2b1e7a1ec8896859f0cc3dfb18e44270'
    if len(raw) != 4673144 or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('input is not the checked BBK 9588 JZ4730 V1.30 raw payload or its 64-byte wrapper')
    profile = json.loads(profile_path.read_text(encoding='utf-8'))
    if (profile.get('target') != '9588' or profile.get('soc') != 'JZ4730' or
            profile.get('firmware_version') != 'V1.30' or
            profile.get('firmware', {}).get('sha256') != expected_sha or
            profile.get('usb_controller') != 'jz4730-fifo'):
        raise ValueError('profile does not identify the exact checked 9588 JZ4730 V1.30 payload')
    for g in profile['guards'] + profile['notice_guards']:
        a, n = int(g['address'], 0), int(g['length'], 0)
        assert zlib.crc32(raw[a-BASE:a-BASE+n]) & 0xFFFFFFFF == int(g['crc32'], 0), g['name']
    result = {'raw_sha256': hashlib.sha256(raw).hexdigest(),
              'raw_size': len(raw), 'raw_crc32': hex(zlib.crc32(raw) & 0xFFFFFFFF),
              'scope': 'Native MIPS instructions on explicit register-storage MMIO. Does not emulate physical USB ACK, GPIO wiring, LCD electrical timing, or full RTOS boot.',
              'guard_count': len(profile['guards']) + len(profile['notice_guards'])}

    # Execute actual native UDC init and IRQ registration; only printf stubbed.
    m = Model(raw)
    m.stub(0x8000518C)
    assert m.call(0x801890E0) == 0
    udc = lambda offset: m.regs.get(0x13040000 + offset, 0)
    assert udc(0x400) == 0x17
    assert udc(0x008) == 8 and udc(0x00C) == 32
    assert udc(0x2A0) == 0x20 and udc(0x2AC) == 64
    assert m.get(0x80479694) == 0x8018A04C
    assert m.get(0x80479698) == 0
    result['usb_init'] = {'entry': '0x801890e0', 'stubs': ['printf@0x8000518c'],
                          'irq_register': 'real native 0x80004a34',
                          'irq_slot': '0x80479694', 'irq_handler': hex(m.get(0x80479694)),
                          'irq_argument': m.get(0x80479698),
                          'writes': m.writes,
                          'ep5_type': hex(udc(0x2A0)), 'ep5_max_packet': udc(0x2AC)}

    # Native GP89 query and wrapper; exact active-low return values.
    m = Model(raw)
    polarity = []
    for value in (0, 0x02000000):
        m.regs[0x10010060] = value
        query = m.call(0x8002917C)
        wrapped = m.call(0x8002ABF4)
        assert (query, wrapped) == ((1, 0x80) if value == 0 else (0, 0))
        polarity.append({'raw_gpio_bits': hex(value), 'query': query, 'wrapper': wrapped})
    result['gp89_polarity'] = polarity

    # Run native board setup with deliberately nontrivial register contents.
    m = Model(raw)
    for addr in (0x10010000, 0x10010004, 0x10010014, 0x10010020, 0x13000000):
        m.regs[addr] = 0xFFFFFFFF
    m.call(0x80004310)
    assert m.regs[0x10010004] & 0x20000000
    assert not (m.regs[0x10010014] & 0x0C000000)
    assert not (m.regs[0x10010020] & 0x20000000)
    assert not (m.regs[0x13000000] & 0x80)
    result['board_setup'] = {'entry': '0x80004310', 'sba': hex(m.regs[0x13000000]),
                             'pullup_data': hex(m.regs[0x10010000]),
                             'pullup_dir': hex(m.regs[0x10010004]),
                             'pullup_function': hex(m.regs[0x10010014]),
                             'pullup_irq_enable': hex(m.regs[0x10010020])}
    m.regs[0x10010000] = 0xA5A55A5A
    m.call(0x8018AD10)
    connected = m.regs[0x10010000]
    m.call(0x8018AD48)
    disconnected = m.regs[0x10010000]
    assert connected == (0xA5A55A5A | 0x20000000)
    assert disconnected == (0xA5A55A5A & ~0x20000000)
    result['pullup_native'] = {'connect_entry': '0x8018ad10', 'disconnect_entry': '0x8018ad48',
                               'initial': '0xa5a55a5a', 'connected': hex(connected),
                               'disconnected': hex(disconnected)}

    # Actual GPIO setup and real IRQ registration. GP89 IRQ is 48+89.
    m = Model(raw)
    m.call(0x80028194)
    slot = 0x80479634 + 137 * 8
    assert m.get(slot) == 0x800294F4 and m.get(slot + 4) == 89
    assert not (m.regs.get(0x10010064, 0) & 0x02000000)
    result['gp89_setup'] = {'entry': '0x80028194', 'irq_slot': hex(slot),
                            'irq_handler': hex(m.get(slot)), 'irq_argument': m.get(slot+4),
                            'writes': m.writes}

    # Real LCD init/memset/descriptor/IRQ registration. Clock utility stubs are
    # explicit board-utility boundaries; they do not replace descriptor logic.
    m = Model(raw)
    m.stub(0x80004150)
    m.stub(0x8000755C)
    assert m.call(0x8000727C) == 0
    desc = [m.get(0x80479FE0 + i*4) for i in range(12)]
    assert desc[:8] == [0x00479FF0, 0x01F81000, 0xDEADBEAF, 0x10000080,
                        0x00479FF0, 0x01F82000, 0xBEAFBEAF, 0xC0009600]
    assert m.regs[0x13050040] == 0x00479FF0
    assert m.get(0x80479724) == 0x80007E28
    assert m.call(0x80007240) == 240
    assert m.call(0x8000724C) == 320
    assert m.call(0x80007258) == 16
    result['lcd_init'] = {'entry': '0x8000727c', 'descriptor_entry': '0x80007414',
                          'stubs': ['clock utility@0x80004150', 'panel clock/setup@0x8000755c'],
                          'memset': 'real native 0x80006410', 'irq_register': 'real native 0x80004a34',
                          'width': 240, 'height': 320, 'bits': 16,
                          'descriptors': [hex(v) for v in desc],
                          'dma_da0': hex(m.regs[0x13050040]),
                          'irq_slot': '0x80479724', 'irq_handler': hex(m.get(0x80479724))}

    # Logical GUI rotation is separate from the LCD scanout dimensions and
    # the model-specific panel orientation code returned by GUI+0x738.
    m = Model(raw)
    m.put(0x803B27D0, 0x81A10000)
    m.put(0x803B2814, 0x81BFE000)
    m.stub(0x81BFE000)
    m.stub(0x800D59C0)
    rotations = []
    for rotation in (0, 1):
        m.call(0x800D0EB0, rotation)
        width, height = m.get(0x804A8E88), m.get(0x804A8E8C)
        assert (width, height) == ((240, 320) if rotation == 0 else (320, 240))
        assert m.get(0x804A8E84) == rotation
        rotations.append({'logical_rotation': rotation, 'width': width, 'height': height})
    assert m.call(0x8001923C) == 0x130
    result['gui_orientation'] = {'entry': '0x800d0eb0', 'stubs': ['GUI backend callback', 'clip/setup@0x800d59c0'],
                                  'geometry': rotations, 'panel_getter': '0x8001923c',
                                  'panel_orientation_code': '0x130'}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('firmware', type=Path, help='privately supplied original C200knl.bin or exact raw payload')
    parser.add_argument('--profile', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'profiles' / '9588-jz4730.json',
                        help='checked JZ4730 profile (default: ../profiles/9588-jz4730.json)')
    parser.add_argument('--output', required=True, type=Path, help='explicit destination JSON report')
    args = parser.parse_args()
    if not __debug__:
        parser.error('run without Python -O: native model assertions are required')
    if args.output.resolve() in (args.firmware.resolve(), args.profile.resolve(), Path(__file__).resolve()):
        parser.error('--output must not overwrite firmware, profile or this utility')
    try:
        report = run(args.firmware, args.profile)
    except (ValueError, OSError, AssertionError) as exc:
        parser.error(str(exc) or 'native MIPS validation assertion failed')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f'PASS: {report["guard_count"]} CRC guards, native USB/IRQ12, GP89 polarity, '
          f'PA29/SBA, GPIO IRQ137, LCD240x320/16, GUI rotation. Report: {args.output}')
