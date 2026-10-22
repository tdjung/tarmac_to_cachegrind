#!/usr/bin/env python3
"""Run test firmware under QEMU and write Tarmac-style traces plus ground truth.

This is a test-data generator, not part of the converter. It runs a Cortex-M
ELF one instruction per translation block, so every executed instruction is
logged with the register state *before* it executes, then writes:

  <stem>.es.log          ES dialect, condition failures marked CCFAIL
  <stem>.es-noccfail.log ES dialect from a generator that never marks CCFAIL
  <stem>.it.log          IT/IS dialect
  <stem>.it-if.log       IT/IS dialect with an IF duplicate after some records
  <stem>.truth.json      per-branch-site jump/fall counts decided from flags

Ground truth never looks at the next PC: a site's outcome is its condition
evaluated against NZCV (or the compared register for CBZ/CBNZ). That makes it
an independent check on the converter's next-PC resolution.

QEMU has no Cortex-M35P model. M35P is ARMv8-M Mainline like the M33, so the
M35P build runs on mps2-an505 (Cortex-M33): same instruction set.

Usage: qemu_to_tarmac.py ELF {m4|m35p} OUT_DIR
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import tarmac_to_cachegrind as converter  # noqa: E402

MACHINES = {"m4": ("mps2-an386", "cortex-m4"), "m35p": ("mps2-an505", "cortex-m33")}
TRACE = re.compile(r"^Trace \d+: \S+ \[[0-9a-f]+/([0-9a-f]+)/")
REG = re.compile(r"R(\d\d)=([0-9a-f]{8})")
XPSR = re.compile(r"^XPSR=([0-9a-f]{8})")
EXC = re.compile(r"^Taking exception \d+ \[IRQ\]")
EXC_NUM = re.compile(r"pending (?:nonsecure |secure )?exception (\d+)")
CONDITIONS = ("eq", "ne", "cs", "cc", "mi", "pl", "vs", "vc", "hi", "ls", "ge", "lt", "gt", "le")
ALIASES = {"hs": "cs", "lo": "cc"}


def condition_passes(cond, xpsr):
    n, z, c, v = (xpsr >> 31) & 1, (xpsr >> 30) & 1, (xpsr >> 29) & 1, (xpsr >> 28) & 1
    return {
        "eq": z, "ne": not z, "cs": c, "cc": not c, "mi": n, "pl": not n,
        "vs": v, "vc": not v, "hi": c and not z, "ls": not c or z,
        "ge": n == v, "lt": n != v, "gt": not z and n == v, "le": z or n != v,
    }[cond]


def it_condition(xpsr):
    """Condition of the current instruction if it is inside an IT block.

    ITSTATE[1:0] = XPSR[26:25], ITSTATE[7:2] = XPSR[15:10]; the current
    condition is ITSTATE[7:4] while ITSTATE[3:0] is non-zero.
    """
    it = ((xpsr >> 25) & 0x3) | (((xpsr >> 10) & 0x3F) << 2)
    if it & 0xF == 0:
        return None
    code = (it >> 4) & 0xF
    return CONDITIONS[code] if code < 14 else None


def suffix_condition(mnemonic):
    base = mnemonic.split(".")[0].lower()
    for cond in list(CONDITIONS) + list(ALIASES):
        if base.endswith(cond) and len(base) > len(cond):
            return ALIASES.get(cond, cond)
    return None


def listing(elf):
    """pc -> (opcode hex as Tarmac prints it, mnemonic, operands)."""
    out = subprocess.run(["arm-none-eabi-objdump", "-d", "-z", str(elf)], check=True,
                         stdout=subprocess.PIPE, universal_newlines=True).stdout
    table = {}
    for line in out.splitlines():
        m = re.match(r"^\s*([0-9a-f]+):\s+((?:[0-9a-f]{4}\s?){1,2})\s+(\S+)\s*(.*)$", line)
        if m and not m.group(3).startswith("."):
            table[int(m.group(1), 16)] = ("".join(m.group(2).split()), m.group(3), m.group(4))
    return table


def run(elf, core, log):
    machine, cpu = MACHINES[core]
    subprocess.run(["qemu-system-arm", "-M", machine, "-cpu", cpu, "-nographic",
                    "-monitor", "none", "-serial", "none",
                    "-semihosting-config", "enable=on,target=native", "-kernel", str(elf),
                    "-accel", "tcg,one-insn-per-tb=on", "-d", "exec,cpu,int,nochain",
                    "-D", str(log)], check=True, timeout=120)


def records(log):
    """Yield ('insn', pc, regs, xpsr) and ('exc', number) in execution order."""
    pc = regs = None
    pending_exc = False
    with open(log) as f:
        for line in f:
            m = TRACE.match(line)
            if m:
                pc, regs = int(m.group(1), 16), {}
                continue
            if pc is not None:
                for r, val in REG.findall(line):
                    regs[int(r)] = int(val, 16)
                x = XPSR.match(line)
                if x:
                    yield ("insn", pc, regs, int(x.group(1), 16))
                    pc = None
                    continue
            if EXC.match(line):
                pending_exc = True
                continue
            e = EXC_NUM.search(line)
            if e and pending_exc:
                yield ("exc", int(e.group(1)))
                pending_exc = False


def main(argv):
    elf, core, out_dir = Path(argv[1]), argv[2], Path(argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)
    table = listing(elf)
    sites = converter.analyze_branches(
        subprocess.run(["arm-none-eabi-objdump", "-d", "-z", "--no-show-raw-insn", str(elf)],
                       check=True, stdout=subprocess.PIPE, universal_newlines=True).stdout).sites
    truth = {"jump": {}, "fall": {}}
    stem = out_dir / elf.stem
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "qemu.log"
        run(elf, core, log)
        es, es_plain, it, it_if = ([] for _ in range(4))
        t = 1000
        for index, record in enumerate(records(log)):
            t += 1
            if record[0] == "exc":
                line = "{} tic ES EXC [{}] SysTick".format(t, record[1])
                es.append(line)
                es_plain.append(line)
                continue
            _, pc, regs, xpsr = record
            if pc not in table:
                continue
            opcode, mnemonic, operands = table[pc]
            site = sites.get(pc)
            cond = it_condition(xpsr)
            if cond is None and site is not None and site.kind == "bcc":
                cond = suffix_condition(mnemonic)  # only b<cc> is conditional outside IT
            failed = cond is not None and not condition_passes(cond, xpsr)
            if site is not None:
                if site.kind == "cbz":
                    reg = int(re.match(r"r(\d+)", operands).group(1))
                    zero = regs.get(reg, 0) == 0
                    taken = zero if mnemonic.startswith("cbz") else not zero
                else:
                    taken = not failed
                key = "0x{:x}".format(pc)
                bucket = truth["jump" if taken else "fall"]
                bucket[key] = bucket.get(key, 0) + 1
            text = "{} {}".format(mnemonic.upper(), operands).strip()
            state = "T16" if len(opcode) == 4 else "T32"
            es.append("{} tic ES ({:08x}:{}) T thrd: {}{}".format(
                t, pc, opcode, "CCFAIL " if failed else "", text))
            es_plain.append("{} tic ES ({:08x}:{}) T thrd: {}".format(t, pc, opcode, text))
            event = "IS" if failed else "IT"
            it_line = "{} ps {} ({:08x}:{:08x}) {:08x} {} {} {}".format(
                t * 100, event, pc, index, pc, opcode, state, text)
            it.append(it_line)
            it_if.append(it_line)
            if index % 7 == 0 and not failed:
                it_if.append(it_line.replace(" IT (", " IF (", 1))
            if index % 11 == 0:
                for lines in (es, es_plain, it, it_if):
                    lines.append("                   R R0 {:08x}".format(regs.get(0, 0)))
    for suffix, lines, header in (("es", es, "Tarmac Text Rev 3t"), ("es-noccfail", es_plain, "Tarmac Text Rev 3t"),
                                  ("it", it, None), ("it-if", it_if, None)):
        with open("{}.{}.log".format(stem, suffix), "w") as f:
            if header:
                f.write(header + "\n")
            f.write("\n".join(lines) + "\n")
    with open("{}.truth.json".format(stem), "w") as f:
        json.dump(truth, f, indent=1, sort_keys=True)
        f.write("\n")
    print("{}: {} instructions, {} sites hit".format(
        stem.name, len(it), len(set(truth["jump"]) | set(truth["fall"]))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
