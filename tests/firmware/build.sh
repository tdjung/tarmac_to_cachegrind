#!/bin/sh
# Rebuild the branch-coverage test firmware for both target cores.
# Requires arm-none-eabi-gcc. Outputs are committed so tests do not need gcc.
set -e
cd "$(dirname "$0")"
# BRANCH_ONLY=inline rebuilds just the inline fixture, leaving the committed
# branch ELFs (which the committed traces were recorded from) untouched.
[ "$BRANCH_ONLY" = inline ] || for t in m4 m35p; do
  case $t in m4) cpu=cortex-m4;; m35p) cpu=cortex-m35p;; esac
  arm-none-eabi-gcc -mcpu=$cpu -mthumb -O2 -g -ffreestanding -nostdlib \
    -fno-builtin -Wl,--build-id=none -T link_$t.ld startup.c branchy.c shapes.S -o branchy_$t.elf
done
# Inline attribution fixture, linked so code and data do not share addresses.
arm-none-eabi-gcc -mcpu=cortex-m4 -mthumb -O2 -g -ffreestanding -nostdlib \
  -Wl,--build-id=none -Wl,-e,func_A -Wl,-Ttext=0x1000 -Wl,-Tbss=0x20000000 inline.c -o inline_m4.elf
