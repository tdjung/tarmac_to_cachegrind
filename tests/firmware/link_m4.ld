MEMORY { FLASH (rx) : ORIGIN = 0x00000000, LENGTH = 256K  RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 64K }
SECTIONS {
  .text : { KEEP(*(.vectors)) *(.text*) *(.rodata*) } > FLASH
  .data : { __data_start = .; *(.data*) __data_end = .; } > RAM AT > FLASH
  __data_load = LOADADDR(.data);
  .bss (NOLOAD) : { __bss_start = .; *(.bss*) *(COMMON) __bss_end = .; } > RAM
  __stack_top = ORIGIN(RAM) + LENGTH(RAM);
}
