"""Instructions the debug info gives no source line (DWARF line 0).

addr2line reports them as `file.c:?` or `??:0` and `objdump -l` prints no
location for them, so in a listing they look like part of the line above,
yet a profile left them at line 0, outside every source line. The rules are
in attribute_line_zero; the firmware fixture is tests/firmware/line0.c built
with clang, which emits line 0 inside an inlined body too.
"""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import tarmac_to_cachegrind as converter
from test_branch import ADDR2LINE, FIRMWARE, OBJDUMP, arm_tool, disassembly_of

Source = converter.Source
Frame = converter.InlineFrame

# One function; comments give each instruction's resolved line / inline frame.
LISTING = """
Disassembly of section .text:

00001000 <A>:
    1000:\tmovs\tr0, #1
    1002:\tmovs\tr1, #2
    1004:\tb.n\t1010 <A+0x10>
    1006:\tmovs\tr2, #0
    1008:\tadds\tr2, #1
    100a:\tbx\tlr
    100c:\tmovs\tr3, #0
    100e:\tpop\t{r4, pc}
    1010:\tmovs\tr0, r0

00001020 <C>:
    1020:\tmovs\tr0, #1
    1022:\tmovs\tr1, #2
"""


def rules_case():
    sources = {
        0x1000: Source("a.c", "A", 5),
        0x1002: Source("a.c", "A", 0),    # falls from 1000, same function -> previous (5)
        0x1004: Source("a.c", "A", 0),    # chain: inherits 1002's placed line (5)
        0x1006: Source("a.c", "A", 0),    # after b.n: next in its run (7)
        0x1008: Source("a.c", "A", 7),
        0x100a: Source("a.c", "A", 7),
        0x100c: Source("???", "A", 0),    # inlined B, run reaches only A's line -> call site
        0x100e: Source("a.c", "A", 9),
        0x1010: Source("???", "???", 0),  # after pop {pc}, last instruction -> unresolved
        0x1020: Source("c.c", "C", 3),
        0x1022: Source("c.c", "C", 0),    # different symbol from A, previous in C
    }
    inline = {pc: Frame(s.function, "???", 0) for pc, s in sources.items()}
    inline[0x100c] = Frame("B", "a.c", 20)
    return sources, inline


class FlowTests(unittest.TestCase):
    def test_what_ends_straight_line_execution(self):
        ends = converter.ends_flow
        for mnemonic, operands in [("b.n", "1010 <A+0x10>"), ("b.w", "2000 <f>"), ("b", "1000"),
                                   ("bx", "lr"), ("pop", "{r4, pc}"), ("pop.w", "{r4, r5, pc}"),
                                   ("ldmia.w", "sp!, {r4, pc}"), ("ldr.w", "pc, [sp], #4"),
                                   ("mov", "pc, lr"), ("tbb", "[pc, r0]"), ("tbh", "[pc, r0, lsl #1]")]:
            self.assertTrue(ends(mnemonic, operands), (mnemonic, operands))
        # Conditional forms (also inside IT blocks), calls, and pops without pc continue.
        for mnemonic, operands in [("beq.n", "1010 <A+0x10>"), ("bxeq", "lr"), ("popne", "{r4, pc}"),
                                   ("bl", "2000 <f>"), ("blx", "r3"), ("pop", "{r4, r5}"),
                                   ("ldr", "r0, [pc, #8]"), ("mov", "r0, pc"), ("cbz", "r0, 1010 <A+0x10>")]:
            self.assertFalse(ends(mnemonic, operands), (mnemonic, operands))

    def test_listing_stops_at_symbols_and_data(self):
        listing = converter.Listing(LISTING + "    1024:\t.word\t0x12345678\n    1028:\tmovs\tr0, #0\n")
        self.assertEqual(listing.prev[0x1002], 0x1000)
        self.assertNotIn(0x1020, listing.prev)          # new symbol
        self.assertNotIn(0x1028, listing.prev)          # after literal data
        self.assertFalse(listing.falls[0x1004])         # b.n
        self.assertTrue(listing.falls[0x1006])
        self.assertEqual(converter.Listing(LISTING, 0x100).prev[0x1102], 0x1100)


class RuleTests(unittest.TestCase):
    def test_each_rule(self):
        sources, inline = rules_case()
        placed = converter.attribute_line_zero(sources, inline, converter.Listing(LISTING))
        self.assertEqual(sources[0x1002], Source("a.c", "A", 5))
        self.assertEqual(sources[0x1004], Source("a.c", "A", 5))
        self.assertEqual(sources[0x1006], Source("a.c", "A", 7))
        self.assertEqual(sources[0x100c], Source("a.c", "A", 20))
        self.assertEqual(sources[0x1010], Source("???", "???", 0))
        self.assertEqual(sources[0x1022], Source("c.c", "C", 3))
        self.assertEqual(placed, {"previous": 3, "next": 1, "call_site": 1, "unresolved": 1})

    def test_never_inherits_across_inline_functions(self):
        # 0x100c is B's code: A's line 9 after it is not its line, so the call
        # site is used. With the B frame unknown, A's line would be taken.
        sources, inline = rules_case()
        converter.attribute_line_zero(sources, inline, converter.Listing(LISTING), targets=[0x100c])
        self.assertEqual(sources[0x100c].line, 20)
        sources, inline = rules_case()
        inline[0x100c] = Frame("???", "???", 0)
        converter.attribute_line_zero(sources, inline, converter.Listing(LISTING), targets=[0x100c])
        self.assertEqual(sources[0x100c].line, 9)

    def test_unknown_file_and_function_come_from_the_neighbour(self):
        # `??:0` with no function name: both are taken with the line.
        sources, inline = rules_case()
        sources[0x1002] = Source()
        inline[0x1002] = Frame("???", "???", 0)
        converter.attribute_line_zero(sources, inline, converter.Listing(LISTING), targets=[0x1002])
        self.assertEqual(sources[0x1002], Source("a.c", "A", 5))

    def test_unresolved_neighbours_are_resolved_first(self):
        # --executed-only: only 0x1002 ran. Its neighbours are resolved on
        # demand, so the result is the same as with every PC resolved.
        full, inline = rules_case()
        converter.attribute_line_zero(full, inline, converter.Listing(LISTING))
        sources = {0x1002: Source("a.c", "A", 0)}
        partial_inline = {0x1002: inline[0x1002]}
        asked = []

        def more(pcs):
            asked.append(set(pcs))
            for pc in pcs:
                sources[pc] = rules_case()[0][pc]
                partial_inline[pc] = inline[pc]

        converter.attribute_line_zero(sources, partial_inline, converter.Listing(LISTING),
                                      targets=[0x1002], resolve_more=more)
        self.assertEqual(len(asked), 1)
        self.assertEqual(sources[0x1002], full[0x1002])

    def test_message(self):
        self.assertIsNone(converter.line_zero_message({}))
        text = converter.line_zero_message({"previous": 3, "next": 1, "call_site": 1, "unresolved": 1})
        self.assertIn("6 instructions", text)
        self.assertIn("previous 3, next 1, call site 1, unresolved 1", text)


class InlineFrameTests(unittest.TestCase):
    def test_innermost_function_and_call_site(self):
        # The reported case: B's code at 0x16 has no line; A called B at 1039.
        result = subprocess.CompletedProcess(
            [], 0, "0x00000016\nB\nfile.c:?\nA\nfile.c:1039\n0x00000018\nA\nfile.c:1040\n"
                   "0x0000001a\n??\n??:0\n", "")
        inline = {}
        with patch.object(converter.subprocess, "run", return_value=result):
            sources = converter.resolve_pcs([0x16, 0x18, 0x1a], Path("a.elf"), "addr2line", inline=inline)
        self.assertEqual(sources[0x16], Source("file.c", "A", 0))
        self.assertEqual(inline[0x16], Frame("B", "file.c", 1039))
        self.assertEqual(inline[0x18], Frame("A", "???", 0))
        self.assertEqual(inline[0x1a], Frame("???", "???", 0))


@unittest.skipUnless(OBJDUMP and ADDR2LINE, "needs Arm-capable objdump and addr2line")
class FirmwareTests(unittest.TestCase):
    """tests/firmware/line0.c, clang -O2, Cortex-M4."""

    elf = FIRMWARE / "line0_m4.elf"

    def resolve(self):
        dump = disassembly_of(self.elf)
        pcs = converter.instruction_pcs(dump)
        inline = {}
        sources = converter.resolve_pcs(pcs, self.elf, ADDR2LINE, inline=inline)
        return dump, sources, inline

    def test_line_zero_is_placed(self):
        dump, sources, inline = self.resolve()
        original = dict(sources)
        listing = converter.Listing(dump)
        placed = converter.attribute_line_zero(sources, inline, listing)
        zero = [pc for pc, s in original.items() if converter.needs_line(s)]
        self.assertGreater(len(zero), 10, "the fixture must contain line-0 instructions")
        self.assertGreater(placed["previous"], placed["unresolved"])
        self.assertEqual(sum(placed.values()), len(zero))
        text = (FIRMWARE / "line0.c").read_text().splitlines()
        b_body = range(next(i for i, l in enumerate(text, 1) if "static int B" in l),
                       next(i for i, l in enumerate(text, 1) if "return s ^ v" in l) + 1)
        calls = {i for i, l in enumerate(text, 1) if "B(x, a)" in l or "B(y, b)" in l}
        for pc in zero:
            source = sources[pc]
            if converter.needs_line(source):
                continue
            # The containing function never changes; B's own code stays on
            # B's lines or on the line that called it.
            self.assertEqual(source.function, original[pc].function if original[pc].function != "???"
                             else source.function)
            if inline[pc].function == "B":
                self.assertTrue(source.line in b_body or source.line in calls, hex(pc))
            prev = listing.prev.get(pc)
            if prev is not None and listing.falls.get(prev) and inline[prev].function == inline[pc].function:
                self.assertEqual(source.line, sources[prev].line, hex(pc))

    def test_inlined_body_line_zero_takes_the_body_line(self):
        # The reported shape: B():file.c:N ... then B's code with line 0 that
        # objdump shows as a continuation of N. It must count on line N.
        dump, sources, inline = self.resolve()
        original = dict(sources)
        listing = converter.Listing(dump)
        converter.attribute_line_zero(sources, inline, listing)
        shaped = [pc for pc in original
                  if converter.needs_line(original[pc]) and inline[pc].function == "B"
                  and listing.prev.get(pc) is not None and listing.falls.get(listing.prev[pc])
                  and inline[listing.prev[pc]].function == "B"
                  and not converter.needs_line(original[listing.prev[pc]])]
        self.assertTrue(shaped, "fixture lost the inlined line-0 case")
        for pc in shaped:
            self.assertEqual(sources[pc].line, original[listing.prev[pc]].line)

    def test_output_has_no_placeable_line_zero_rows_in_either_mode(self):
        dump, sources, inline = self.resolve()
        pcs = sorted(converter.instruction_pcs(dump))
        trace = "".join(f"IT ({i}) {pc:08x} 0000 T16 NOP\n" for i, pc in enumerate(pcs))
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "t.log").write_text(trace)
            rows = {}
            for mode in ([], ["--executed-only"]):
                out = directory / ("x.out" if not mode else "e.out")
                status = converter.main([str(directory / "t.log"), "--elf", str(self.elf), "-o", str(out),
                                         "--objdump", OBJDUMP, "--addr2line", ADDR2LINE] + mode)
                self.assertEqual(status, 0)
                rows[bool(mode)] = {line.split()[0]: line.split()[1] for line in out.read_text().splitlines()
                                    if line.startswith("0x")}
            self.assertEqual(rows[False], rows[True], "--executed-only must place lines the same way")
            converter.attribute_line_zero(sources, inline, converter.Listing(dump))
            unresolved = {f"0x{pc:x}" for pc, s in sources.items() if converter.needs_line(s)}
            zero_rows = {pc for pc, line in rows[False].items() if line == "0"}
            self.assertEqual(zero_rows, unresolved)


if __name__ == "__main__":
    unittest.main()
