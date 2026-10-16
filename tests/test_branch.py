"""Branch coverage: site detection, outcome resolution, output and merging.

Ground-truth tests use committed Cortex-M4 and Cortex-M35P firmware
(tests/firmware) and traces produced by tests/firmware/qemu_to_tarmac.py from a
real QEMU run. The truth there is decided from CPU flags, never from the next
PC, so it independently checks the converter's next-PC resolution.
"""

from collections import Counter
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import tarmac_to_cachegrind as converter


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "tests" / "firmware"
TRACES = FIRMWARE / "traces"
CORES = ("m4", "m35p")
VARIANTS = ("es", "es-noccfail", "it", "it-if")


def arm_tool(*names):
    for name in names:
        found = shutil.which(name)
        if found and (name.startswith("arm-none-eabi") or name.startswith("llvm")):
            return found
    return None


OBJDUMP = arm_tool("arm-none-eabi-objdump", "llvm-objdump")
ADDR2LINE = arm_tool("arm-none-eabi-addr2line", "llvm-addr2line")
OBJDUMPS = [tool for tool in (arm_tool("arm-none-eabi-objdump"), arm_tool("llvm-objdump")) if tool]


def disassembly(core, tool=None):
    return subprocess.run([tool or OBJDUMP, "-d", "-z", "--no-show-raw-insn",
                           str(FIRMWARE / "branchy_{}.elf".format(core))],
                          check=True, stdout=subprocess.PIPE, universal_newlines=True).stdout


GNU_LISTING = """
Disassembly of section .text:

000000a0 <clamp>:
  a0:\tcmp\tr0, r1
  a2:\tblt.n\tac <clamp+0xc>
  a4:\tcmp\tr0, r2
  a6:\tit\tge
  a8:\tmovge\tr0, r2
  aa:\tbx\tlr
  ac:\tcmp\tr0, #0
  ae:\tit\teq
  b0:\tbxeq\tlr
  b2:\tcbz\tr1, b8 <clamp+0x18>
  b4:\tbeq.w\t200 <other>
  b8:\t.word\t0x00000001
  bc:\tbne.n\ta0 <clamp>
"""

LLVM_LISTING = """
Disassembly of section .text:

000000a0 <clamp>:
      a0:      \tcmp\tr0, r1
      a2:      \tblt\t0xac <clamp+0xc>        @ imm = #0x6
      a4:      \tcmp\tr0, r2
      a6:      \tit\tge
      a8:      \tmovge\tr0, r2
      aa:      \tbx\tlr
      ac:      \tcmp\tr0, #0x0
      ae:      \tit\teq
      b0:      \tbxeq\tlr
      b2:      \tcbz\tr1, 0xb8 <clamp+0x18>   @ imm = #0x2
      b4:      \tbeq.w\t0x200 <other>          @ imm = #0x148
      b8: 01 00 00 00  \t.word\t0x00000001
      bc:      \tbne\t0xa0 <clamp>             @ imm = #-0x20
"""


class ClassifyTests(unittest.TestCase):
    def test_conditional_branch_mnemonics(self):
        cases = {
            ("beq", "0x1a <f>"): ("bcc", 0x1A), ("bne.n", "1a <f+0x2>"): ("bcc", 0x1A),
            ("bls.w", "1a"): ("bcc", 0x1A), ("ble", "1a"): ("bcc", 0x1A),
            ("bhs", "1a"): ("bcc", 0x1A), ("blo", "1a"): ("bcc", 0x1A),
            ("bleq", "1a <g>"): ("call", 0x1A), ("bllt", "1a"): ("call", 0x1A),
            ("cbz", "r0, ac <f+0xc>"): ("cbz", 0xAC), ("cbnz", "r3, 0xac"): ("cbz", 0xAC),
            ("bxeq", "lr"): ("indirect", None), ("bxnsne", "lr"): ("indirect", None),
            ("blxgt", "r3"): ("indirect_call", None),
            ("popne", "{r4, pc}"): ("indirect", None), ("ldmiaeq.w", "sp!, {r4, pc}"): ("indirect", None),
            ("ldrne", "pc, [sp], #4"): ("indirect", None), ("movhi", "pc, r3"): ("indirect", None),
        }
        for (mnemonic, operands), expected in cases.items():
            with self.subTest(mnemonic=mnemonic):
                self.assertEqual(converter.classify_branch(mnemonic, operands), expected)

    def test_not_conditional_branches(self):
        for mnemonic, operands in (("b", "1a"), ("b.n", "1a"), ("bl", "1a"), ("blx", "r3"),
                                   ("bx", "lr"), ("blxns", "r3"), ("pop", "{r4, pc}"),
                                   ("popne", "{r4, r5}"), ("ldrne", "r0, [pc, #4]"),
                                   ("movhi", "r0, #1"), ("bics", "r0, r1"), ("bfc", "r0, #1, #2"),
                                   ("bkpt", "0x00ab"), ("movwhi", "r3, #57005")):
            with self.subTest(mnemonic=mnemonic):
                self.assertIsNone(converter.classify_branch(mnemonic, operands))


class AnalysisTests(unittest.TestCase):
    def test_gnu_and_llvm_listings_agree(self):
        gnu, llvm = converter.analyze_branches(GNU_LISTING), converter.analyze_branches(LLVM_LISTING)
        self.assertEqual(gnu.sites, llvm.sites)
        self.assertEqual(gnu.conditional, llvm.conditional)
        self.assertEqual(converter.instruction_pcs(GNU_LISTING), converter.instruction_pcs(LLVM_LISTING))
        self.assertNotIn(0xB8, converter.instruction_pcs(LLVM_LISTING))

    def test_sites_targets_fallthrough_and_it_blocks(self):
        analysis = converter.analyze_branches(GNU_LISTING, load_offset=0x1000)
        site = converter.BranchSite
        self.assertEqual(analysis.sites, {
            0x10A2: site(0x10A2, 0x10AC, 0x10A4, "bcc"),
            0x10B0: site(0x10B0, None, 0x10B2, "indirect"),
            0x10B2: site(0x10B2, 0x10B8, 0x10B4, "cbz"),
            # Data after a branch still ends it: the fallthrough is the next address.
            0x10B4: site(0x10B4, 0x1200, 0x10B8, "bcc"),
            # Last listed instruction has no known fallthrough.
            0x10BC: site(0x10BC, 0x10A0, None, "bcc"),
        })
        # movge is conditional but not a branch; bxeq in an IT block is a site.
        self.assertEqual(analysis.conditional, frozenset({0x10A8}))
        self.assertEqual(analysis.entries, frozenset({0x10A0}))

    def test_gnu_ascii_data_dump_is_not_an_instruction(self):
        listing = "   0:\tA...A...A.......\n  10:\t............F...\n  20:\tmovs\tr0, #1\n  22:\tnop\n"
        self.assertEqual(converter.instruction_pcs(listing), {0x20, 0x22})

    @unittest.skipUnless(len(OBJDUMPS) == 2, "needs arm-none-eabi-objdump and llvm-objdump")
    def test_real_firmware_same_with_both_objdumps(self):
        for core in CORES:
            gnu, llvm = (converter.analyze_branches(disassembly(core, tool)) for tool in OBJDUMPS)
            self.assertEqual(gnu.sites, llvm.sites)
            self.assertEqual(gnu.conditional, llvm.conditional)
            self.assertEqual(len(gnu.sites), 19)
            self.assertEqual(Counter(s.kind for s in gnu.sites.values()),
                             {"bcc": 14, "cbz": 2, "indirect": 2, "call": 1})


def outcomes_for(lines, listing=GNU_LISTING):
    outcomes = converter.BranchOutcomes(converter.analyze_branches(listing))
    counts, stats = converter.count_pcs(lines, outcomes=outcomes, allow_empty=True)
    outcomes.finish(counts, stats)
    return outcomes, stats


class ResolutionTests(unittest.TestCase):
    def test_jump_and_fall_by_next_pc(self):
        o, _ = outcomes_for(["IT 000000a2 d003 T16 BLT", "IT 000000ac 2800 T16 CMP",
                             "IT 000000a2 d003 T16 BLT", "IT 000000a4 4290 T16 CMP"])
        self.assertEqual((o.jump, o.fall, o.unresolved), ({0xA2: 1}, {0xA2: 1}, 0))

    def test_condition_failed_site_is_a_fall_in_both_dialects(self):
        o, _ = outcomes_for(["ES (000000a2:d003) T thrd: CCFAIL BLT.N ac",
                             "IS 000000a2 d003 T16 BLT ac", "IT 000000a4 4290 T16 CMP"])
        self.assertEqual((o.jump, o.fall, o.unresolved), ({}, {0xA2: 2}, 0))

    def test_condition_failed_successor_still_resolves(self):
        o, _ = outcomes_for(["IT 000000a6 bfa8 T16 IT", "IT 000000aa 4770 T16 BX",
                             "IT 000000a2 d003 T16 BLT", "IS 000000a4 4290 T16 CMP"])
        self.assertEqual(o.fall, {0xA2: 1})

    def test_duplicate_record_at_site_is_ignored(self):
        o, _ = outcomes_for(["IT (0:1) 000000a2 d003 T16 BLT", "IF (0:2) 000000a2 d003 T16 BLT",
                             "IT (0:3) 000000ac 2800 T16 CMP"])
        self.assertEqual((o.jump, o.unresolved), ({0xA2: 1}, 0))

    def test_exception_or_trace_end_is_unresolved_never_covered(self):
        o, _ = outcomes_for(["ES (000000a2:d003) T thrd: BLT.N ac", "ES EXC [15] SysTick",
                             "ES (000000ac:2800) T thrd: CMP", "ES (000000a2:d003) T thrd: BLT.N ac"])
        self.assertEqual((o.jump, o.fall, o.unresolved), ({}, {}, 2))

    def test_unrelated_successor_is_unresolved(self):
        o, _ = outcomes_for(["IT 000000a2 d003 T16 BLT", "IT 00000400 2800 T16 MOVS"])
        self.assertEqual((o.jump, o.fall, o.unresolved), ({}, {}, 1))

    def test_indirect_site_jump_fall_and_exception_entry(self):
        o, _ = outcomes_for([
            "IT 000000b0 4770 T16 BXEQ", "IT 00000400 2800 T16 MOVS",  # returned: jump
            "IT 000000b0 4770 T16 BXEQ", "IT 000000b2 b109 T16 CBZ",   # fall
            "IT 000000b8 2800 T16 MOVS",                                # cbz jump
            "IT 000000b0 4770 T16 BXEQ", "IT 000000a0 4288 T16 CMP",   # function entry: interrupt
        ])
        self.assertEqual(o.jump, {0xB0: 1, 0xB2: 1})
        self.assertEqual(o.fall, {0xB0: 1})
        self.assertEqual(o.unresolved, 1)

    def test_branchless_conditional_stats(self):
        _, stats = outcomes_for(["IT 000000a8 4610 T16 MOVGE", "IT 000000a8 4610 T16 MOVGE"])
        self.assertEqual(stats.conditional_nonbranch_instructions, 1)
        self.assertEqual(stats.conditional_nonbranch_one_sided, 1)
        _, stats = outcomes_for(["IT 000000a8 4610 T16 MOVGE", "IS 000000a8 4610 T16 MOVGE"])
        self.assertEqual(stats.conditional_nonbranch_one_sided, 0)

    def test_without_outcomes_counting_is_unchanged(self):
        lines = ["ES (000000a2:d003) T thrd: CCFAIL BLT.N ac", "IS 000000a2 d003 T16 BLT",
                 "IT 000000a4 4290 T16 CMP"]
        counts, stats = converter.count_pcs(lines)
        self.assertEqual(counts, {0xA4: 1})
        self.assertEqual(stats.conditional_skipped, 2)


@unittest.skipUnless(OBJDUMP, "needs an Arm-capable objdump (arm-none-eabi-objdump or llvm-objdump)")
class GroundTruthTests(unittest.TestCase):
    """Real execution on both target cores, in all four trace dialect variants."""

    def test_matches_flag_based_truth(self):
        for core in CORES:
            analysis = converter.analyze_branches(disassembly(core))
            truth = json.loads((TRACES / "branchy_{}.truth.json".format(core)).read_text())
            outcomes_by_variant = {}
            for variant in VARIANTS:
                with self.subTest(core=core, variant=variant):
                    outcomes = converter.BranchOutcomes(analysis)
                    with gzip.open(str(TRACES / "branchy_{}.{}.log.gz".format(core, variant)), "rt") as trace:
                        converter.count_pcs(trace, outcomes=outcomes)
                    missing = 0
                    for pc in analysis.sites:
                        for name, got in (("jump", outcomes.jump[pc]), ("fall", outcomes.fall[pc])):
                            want = truth[name].get(hex(pc), 0)
                            # Never more than truth, never covered when truth is not.
                            self.assertLessEqual(got, want, (hex(pc), name))
                            missing += want - got
                    # Every shortfall is accounted for as unresolved.
                    self.assertEqual(missing, outcomes.unresolved)
                    outcomes_by_variant[variant] = outcomes
            covered = {variant: {(pc, side) for pc in analysis.sites
                                 for side, counter in (("j", o.jump), ("f", o.fall)) if counter[pc]}
                       for variant, o in outcomes_by_variant.items()}
            expected = {(int(pc, 16), side[0]) for side in ("jump", "fall") for pc in truth[side]}
            for variant in VARIANTS:
                with self.subTest(core=core, variant=variant, check="coverage"):
                    self.assertEqual(covered[variant], expected)
            self.assertEqual(len(expected), 31)  # 7 of 38 outcomes deliberately uncovered


@unittest.skipUnless(OBJDUMP and ADDR2LINE, "needs Arm-capable objdump and addr2line")
class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def convert(self, core, variant, *options, output=None):
        output = output or self.dir / "{}.{}.out".format(core, variant)
        with patch("sys.stderr", io.StringIO()) as err:
            status = converter.main([str(TRACES / "branchy_{}.{}.log.gz".format(core, variant)),
                                     "--elf", str(FIRMWARE / "branchy_{}.elf".format(core)),
                                     "--objdump", OBJDUMP, "--addr2line", ADDR2LINE,
                                     "-o", str(output)] + list(options))
        self.assertEqual(status, 0, err.getvalue())
        return output.read_text(), err.getvalue()

    def test_single_log_format(self):
        text, err = self.convert("m4", "es")
        self.assertIn("events: Ir Bs Bj Bf\n", text)
        self.assertIn("branch coverage: 31/38 outcomes at 19 sites", err)
        lines = text.splitlines()
        jcnd = [i for i, line in enumerate(lines) if line.startswith("jcnd=")]
        self.assertEqual(len(jcnd), 17)  # direct sites only; bxeq/popne have no target
        for i in jcnd:
            jumped, executed = map(int, lines[i][5:].split()[0].split("/"))
            position, row = lines[i + 1].split(), lines[i + 2].split()
            self.assertEqual(len(position), 2)
            self.assertEqual(position, row[:2])
            self.assertEqual((int(row[3]), int(row[4]), int(row[5])), (1, jumped, executed - jumped))
        summary = lines[-1].split()[1:]
        self.assertEqual(summary[1], "19")
        counts, _, _, branch = converter.read_ir_profile(self.dir / "m4.es.out")
        self.assertEqual(len(branch.targets), 19)
        self.assertEqual(sum(1 for t in branch.targets.values() if t is None), 2)

    def test_no_branch_and_executed_only_keep_legacy_output(self):
        for option in ("--no-branch", "--executed-only"):
            with self.subTest(option=option):
                text, err = self.convert("m35p", "it", option,
                                         output=self.dir / "plain{}.out".format(option))
                self.assertIn("events: Ir\n", text)
                self.assertNotIn("jcnd=", text)
                self.assertNotIn("branch coverage", err)

    def test_batch_total_and_merge_list_agree(self):
        root = self.dir / "scenarios"
        for variant in VARIANTS:
            (root / variant).mkdir(parents=True)
            shutil.copy(str(TRACES / "branchy_m35p.{}.log.gz".format(variant)),
                        str(root / variant / "tarmac.log.gz"))
        with patch("sys.stderr", io.StringIO()):
            status = converter.main([str(root), "--elf", str(FIRMWARE / "branchy_m35p.elf"),
                                     "--objdump", OBJDUMP, "--addr2line", ADDR2LINE])
        self.assertEqual(status, 0)
        out = root / "cachegrind-output"
        total = (out / "total_merge_tarmac_cachegrind.out")
        if not total.exists():
            total = out / "total_merge_cachegrind.out"
        index = json.loads((out / "coverage_index.json").read_text())
        self.assertIn("Bj", index["branch_semantics"])
        listing = self.dir / "list.txt"
        listing.write_text("\n".join(str(p) for p in sorted(out.glob("[0-9]*_cachegrind.out"))) + "\n")
        merged = self.dir / "merged.out"
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(converter.main(["--merge-list", str(listing), "-o", str(merged)]), 0)

        def branch_rows(path):
            rows = {}
            for line in path.read_text().splitlines():
                fields = line.split()
                if line.startswith("0x") and len(fields) == 2 + 1 + 13 + 3:
                    rows[fields[0]] = fields[-3:]
            return rows

        self.assertEqual(len(branch_rows(total)), 19)
        self.assertEqual(branch_rows(total), branch_rows(merged))
        self.assertEqual(total.read_text().splitlines()[-1], merged.read_text().splitlines()[-1])
        self.assertIn("jcnd=", merged.read_text())

    def test_merge_list_rejects_mixed_or_tampered_profiles(self):
        with_branch, _ = self.convert("m4", "it")
        self.convert("m4", "es", "--no-branch", output=self.dir / "plain.out")
        listing = self.dir / "list.txt"
        listing.write_text("1_a_cachegrind.out\n2_b_cachegrind.out\n")
        (self.dir / "1_a_cachegrind.out").write_text(with_branch)
        (self.dir / "2_b_cachegrind.out").write_text((self.dir / "plain.out").read_text())
        with patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(converter.main(["--merge-list", str(listing), "-o", str(self.dir / "m.out")]), 1)
        self.assertIn("with and without branch events", err.getvalue())
        tampered = with_branch.replace("jcnd=", "jcnd=9", 1)
        (self.dir / "2_b_cachegrind.out").write_text(tampered)
        with patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(converter.main(["--merge-list", str(listing), "-o", str(self.dir / "m.out")]), 1)
        self.assertIn("jcnd= does not match", err.getvalue())


class WriterTests(unittest.TestCase):
    def test_write_cachegrind_round_trips_branch_counts(self):
        costs = Counter({converter.Position(0x100, "a.c", "f", 1): 3,
                         converter.Position(0x102, "a.c", "f", 1): 1,
                         converter.Position(0x104, "a.c", "f", 2): 0,
                         converter.Position(0x106, "a.c", "f", 3): 2})
        branch = converter.BranchCounts({0x102: 0x106, 0x104: None},
                                        Counter({0x102: 1}), Counter({0x102: 2}))
        out = io.StringIO()
        converter.write_cachegrind(out, costs, Path("fw.elf"), branch)
        self.assertEqual(out.getvalue(),
            "# callgrind format\npositions: instr line\nevents: Ir Bs Bj Bf\nob: \"fw.elf\"\n"
            "fl=a.c\nfn=f\n0x100 1 3\n"
            "jcnd=1/3 0x106 3\n0x102 1\n0x102 1 1 1 1 2\n"
            "0x104 2 0 1 0 0\n0x106 3 2\nsummary: 6 2 1 2\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "p.out"
            path.write_text(out.getvalue())
            counts, sources, obj, parsed = converter.read_ir_profile(path)
        self.assertEqual(parsed, branch)
        self.assertEqual(counts, {0x100: 3, 0x102: 1, 0x106: 2})

    def test_no_sites_writes_legacy_header(self):
        costs = Counter({converter.Position(0x100, "a.c", "f", 1): 1})
        out = io.StringIO()
        converter.write_cachegrind(out, costs, Path("fw.elf"), converter.BranchCounts({}, Counter(), Counter()))
        self.assertIn("events: Ir\n", out.getvalue())
        self.assertTrue(out.getvalue().endswith("summary: 1\n"))


if __name__ == "__main__":
    unittest.main()
