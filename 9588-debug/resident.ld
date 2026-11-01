/* SPDX-License-Identifier: GPL-3.0-only
 * Adapted from H1Debug resident.ld for BBK 9588, 2026-10-31.
 */
ENTRY(rd_control)
SECTIONS {
  . = 0x81000000;
  .resident_header : { KEEP(*(.resident_header)) }
  .text : { *(.text*) }
  .rodata : { *(.rodata*) }
  .data : { *(.data*) *(.sdata*) }
  .got : { *(.got*) }
  .image_tail : { BYTE(0); . = ALIGN(32); }
  __image_end = .;
  .bss (NOLOAD) : {
    __bss_start = .;
    *(.bss*) *(.sbss*) *(COMMON)
    . = ALIGN(32);
    __bss_end = .;
  }
  ASSERT(rd_control == 0x81000000, "resident control must lead image")
  ASSERT(__bss_end <= 0x81200000, "resident allocation exceeds 2 MiB")
  ASSERT(SIZEOF(.got) == 0, "resident must not use the global offset table")
  /DISCARD/ : { *(.comment*) *(.note*) *(.MIPS.abiflags*) *(.reginfo*) *(.eh_frame*) *(.pdr*) }
}
