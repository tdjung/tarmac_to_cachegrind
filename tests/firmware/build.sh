#!/bin/sh
# Rebuild the branch-coverage test firmware for both target cores.
# Requires arm-none-eabi-gcc. Outputs are committed so tests do not need gcc.
set -e
cd "$(dirname "$0")"
for t in m4 m35p; do
  case $t in m4) cpu=cortex-m4;; m35p) cpu=cortex-m35p;; esac
  arm-none-eabi-gcc -mcpu=$cpu -mthumb -O2 -g -ffreestanding -nostdlib \
    -fno-builtin -Wl,--build-id=none -T link_$t.ld startup.c branchy.c shapes.S -o branchy_$t.elf
done
