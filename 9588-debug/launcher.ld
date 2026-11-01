/* SPDX-License-Identifier: GPL-3.0-only */
ENTRY(bda_main)
SECTIONS {
  . = 0x81c00020;
  .entry : { KEEP(*(.text.bda_main)) }
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
  ASSERT(bda_main == 0x81c00020, "9588 BDA entry moved")
  ASSERT(__bss_end <= 0x81e00000, "launcher exceeds 2 MiB loading window")
  ASSERT(SIZEOF(.got) == 0, "launcher must not use the global offset table")
  /DISCARD/ : { *(.comment*) *(.note*) *(.MIPS.abiflags*) *(.reginfo*) *(.eh_frame*) *(.pdr*) }
}
