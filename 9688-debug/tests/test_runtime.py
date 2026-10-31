#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Exercise real resident wake/TCB code with explicit RTOS call boundaries.

BBK9688_BUILD_DIR selects either chip's built image through test_device's
factory. Firmware scheduler execution is replaced by an observed ABI stub;
these checks do not claim a complete RTOS boot or physical device test.
"""
from __future__ import annotations

import json
import struct
import unittest

from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE
from unicorn.mips_const import (
    UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3,
    UC_MIPS_REG_CP0_STATUS, UC_MIPS_REG_SP, UC_MIPS_REG_RA, UC_MIPS_REG_PC,
)

from test_device import USBModel
from test_usb import relocate


PRIORITY = 6
STACK_BYTES = 32768


def runtime_model():
    """Restore resident wake code and load the launcher's retained task code."""
    model = USBModel(start=False)
    relocation = json.loads((model.build / "relocations.json").read_text(encoding="utf-8"))
    image = relocate((model.build / "resident.bin").read_bytes(), relocation["records"],
                     relocation["base"], model.base)
    for name in ("fw_wake_install", "fw_wake_remove"):
        offset = model.symbols[name] - model.base
        model.put(model.symbols[name], image[offset:offset + 8])
    # Task discovery/creation is only called by the foreground launcher, so
    # --gc-sections correctly removes it from the resident. Execute the actual
    # launcher copy at its original BDA address for these tests.
    model.put(model.report["entry_va"], (model.build / "launcher.bin").read_bytes())
    for name in ("fw_find_resident", "fw_task_slot_empty", "fw_task_create"):
        model.symbols[name] = model.report["launcher_symbols"][name]
    slot = model.macros["FW_TICK_IRQ_SLOT"]
    model.put(slot, struct.pack("<2I", model.macros["FW_TICK_IRQ_HANDLER"], 0))
    return model


def write_word(model, address, value):
    model.put(address, struct.pack("<I", value))


def install_tcb(model):
    tcb = 0x80700000
    model.put(tcb, bytes(0x70))
    write_word(model, tcb + 4, model.base)
    write_word(model, tcb + 8, model.word(model.base + 52))
    write_word(model, tcb + 12, STACK_BYTES // 4)
    model.put(tcb + 0x35, bytes([PRIORITY]))
    write_word(model, model.macros["FW_TASK_TABLE"] + 4 * PRIORITY, tcb)
    return tcb


class RuntimeTests(unittest.TestCase):
    def test_real_wake_install_remove_and_irq_state(self):
        model = runtime_model()
        slot = model.macros["FW_TICK_IRQ_SLOT"]
        native = model.macros["FW_TICK_IRQ_HANDLER"]
        before = model.u.reg_read(UC_MIPS_REG_CP0_STATUS) | 1
        model.u.reg_write(UC_MIPS_REG_CP0_STATUS, before)
        self.assertEqual(model.call("fw_wake_install", model.base), 0)
        self.assertEqual(model.word(slot), model.symbols["tick_irq"])
        self.assertEqual(model.word(slot + 4), 0)
        self.assertEqual(model.u.reg_read(UC_MIPS_REG_CP0_STATUS), before)
        self.assertEqual(model.call("fw_wake_install", model.base), 1)
        self.assertEqual(model.word(slot), model.symbols["tick_irq"])
        self.assertEqual(model.call("fw_wake_remove"), 0)
        self.assertEqual(model.word(slot), native)
        self.assertEqual(model.call("fw_wake_remove"), 0)
        self.assertEqual(model.u.reg_read(UC_MIPS_REG_CP0_STATUS), before)

    def test_real_wake_refuses_existing_owner_or_argument(self):
        for occupied in ("handler", "argument"):
            with self.subTest(occupied=occupied):
                model = runtime_model()
                slot = model.macros["FW_TICK_IRQ_SLOT"]
                write_word(model, slot + (4 if occupied == "argument" else 0), 0x80100000)
                before = model.get(slot, 8)
                self.assertEqual(model.call("fw_wake_install", model.base), 1)
                self.assertEqual(model.get(slot, 8), before)
        for stolen in ("handler", "argument"):
            with self.subTest(stolen=stolen):
                model = runtime_model()
                slot = model.macros["FW_TICK_IRQ_SLOT"]
                self.assertEqual(model.call("fw_wake_install", model.base), 0)
                write_word(model, slot + (4 if stolen == "argument" else 0), 0x80100000)
                before = model.get(slot, 8)
                self.assertEqual(model.call("fw_wake_remove"), 1)
                self.assertEqual(model.get(slot, 8), before)
                write_word(model, slot, model.symbols["tick_irq"])
                write_word(model, slot + 4, 0)
                self.assertEqual(model.call("fw_wake_remove"), 0)

    def test_real_resident_lookup_checks_tcb_owner_stack_and_priority(self):
        model = runtime_model()
        table_entry = model.macros["FW_TASK_TABLE"] + 4 * PRIORITY
        self.assertEqual(model.call("fw_task_slot_empty"), 1)
        self.assertEqual(model.call("fw_find_resident"), 0)
        tcb = install_tcb(model)
        self.assertEqual(model.call("fw_task_slot_empty"), 0)
        self.assertEqual(model.call("fw_find_resident"), model.base)
        mutations = ((4, struct.pack("<I", model.base + 4)),
                     (8, struct.pack("<I", model.word(model.base + 52) + 4)),
                     (12, struct.pack("<I", STACK_BYTES // 4 - 1)),
                     (0x35, bytes([PRIORITY + 1])))
        for offset, damaged in mutations:
            with self.subTest(offset=hex(offset)):
                original = model.get(tcb + offset, len(damaged))
                model.put(tcb + offset, damaged)
                self.assertEqual(model.call("fw_find_resident"), 0)
                model.put(tcb + offset, original)
                self.assertEqual(model.call("fw_find_resident"), model.base)
        for pointer in (1, 0xB3040000, model.macros["FW_RUNTIME_BASE"] - 0x60):
            with self.subTest(pointer=hex(pointer)):
                write_word(model, table_entry, pointer)
                self.assertEqual(model.call("fw_find_resident"), 0)
        write_word(model, table_entry, 0)
        self.assertEqual(model.call("fw_task_slot_empty"), 1)

    def test_real_task_create_passes_nine_o32_arguments(self):
        model = runtime_model()
        create = model.macros["FW_TASK_CREATE_EXT"]
        # External RTOS boundary: jr ra; addiu v0,zero,7. Observe registers
        # and caller argument stack without jumping over a MIPS delay slot.
        model.put(create, struct.pack("<2I", 0x03E00008, 0x24020007))
        model.put(model.macros["FW_OS_RUNNING"], b"\x01")
        model.put(model.macros["FW_SCHED_LOCK"], b"\0")
        model.put(model.macros["FW_INT_NESTING"], b"\0")
        seen = []

        def observe(uc, address, size, _):
            first = [uc.reg_read(register) for register in
                     (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3)]
            stack = uc.reg_read(UC_MIPS_REG_SP)
            seen.append(first + list(struct.unpack("<5I", model.get(stack + 16, 20))))

        hook = model.u.hook_add(UC_HOOK_CODE, observe, begin=create, end=create)
        try:
            self.assertEqual(model.call("fw_task_create", model.base), 7)
            self.assertEqual(seen, [[model.word(model.base + 40), model.base, model.word(model.base + 48),
                                    PRIORITY, 0x3936, model.word(model.base + 52), STACK_BYTES // 4,
                                    model.base, 0]])
            seen.clear()
            install_tcb(model)
            self.assertEqual(model.call("fw_task_create", model.base), 0xFFFFFFFF)
            self.assertEqual(seen, [])
            write_word(model, model.macros["FW_TASK_TABLE"] + 4 * PRIORITY, 0)
            for name, value in (("FW_OS_RUNNING", 0), ("FW_SCHED_LOCK", 1), ("FW_INT_NESTING", 1)):
                with self.subTest(condition=name):
                    original = model.get(model.macros[name], 1)
                    model.put(model.macros[name], bytes([value]))
                    self.assertEqual(model.call("fw_task_create", model.base), 0xFFFFFFFE)
                    self.assertEqual(seen, [])
                    model.put(model.macros[name], original)
        finally:
            model.u.hook_del(hook)


class MMIOCallRegressionTests(unittest.TestCase):
    def test_mmio_store_in_jal_delay_slot_does_not_reenter_callee(self):
        """Unicorn 2.1.4 MEM_WRITE hooks replay this callee's first instruction.

        Native mmio_map callbacks preserve the delay slot and call boundary.
        The JZ4730 model uses those callbacks for its dedicated UDC page.
        """
        u = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        u.mem_map(0, 0x2000000)
        writes = []
        u.mmio_map(0x13040000, 4096, lambda uc, offset, size, _: 0, None,
                   lambda uc, offset, size, value, _: writes.append((offset, size, value)), None)
        u.reg_write(UC_MIPS_REG_CP0_STATUS, 0x10000000)
        base, callee, stack, sentinel = 0x80801000, 0x80801040, 0x81BE0000, 0x81BE1000
        # caller: save RA; jal callee / sw zero,UDC+500; restore RA/stack.
        # callee: allocate 16 bytes; jr ra / release 16 bytes.
        instructions = [0x27BDFFE0, 0xAFBF001C, 0x3C08B304,
                        0x0C000000 | ((callee >> 2) & 0x03FFFFFF), 0xAD000500,
                        0x8FBF001C, 0x03E00008, 0x27BD0020] + [0] * 8 + [
                            0x27BDFFF0, 0x03E00008, 0x27BD0010]
        u.mem_write(base & 0x1FFFFFFF, struct.pack("<" + "I" * len(instructions), *instructions))
        u.reg_write(UC_MIPS_REG_SP, stack)
        u.reg_write(UC_MIPS_REG_RA, sentinel)
        entered = []

        def observe(uc, address, size, _):
            if address == callee:
                entered.append(uc.reg_read(UC_MIPS_REG_SP))

        u.hook_add(UC_HOOK_CODE, observe)
        u.emu_start(base, sentinel, count=100)
        self.assertEqual(u.reg_read(UC_MIPS_REG_PC), sentinel)
        self.assertEqual(u.reg_read(UC_MIPS_REG_SP), stack)
        self.assertEqual(entered, [stack - 32])
        self.assertEqual(writes, [(0x500, 4, 0)])


if __name__ == "__main__":
    unittest.main()
