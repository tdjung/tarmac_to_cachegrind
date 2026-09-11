"""Regression tests, including real ELF/addr2line and CLI integration."""

from collections import Counter
import csv
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tarmac_to_cachegrind as converter


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class ParserTests(unittest.TestCase):
    def test_es_user_dialect_and_exclusions(self):
        with (FIXTURES / "es.log").open() as trace:
            counts, stats = converter.count_pcs(trace)
        self.assertEqual(counts, {0x226F8: 1, 0x226FC: 2})
        self.assertEqual(stats.instructions, 3)
        self.assertEqual(stats.exceptions, 1)
        self.assertEqual(stats.conditional_skipped, 1)
        self.assertEqual(stats.fetch_failed, 1)
        self.assertEqual(stats.malformed, 0)

    def test_it_user_dialect_and_folded_instruction(self):
        with (FIXTURES / "it.log").open() as trace:
            counts, stats = converter.count_pcs(trace)
        self.assertEqual(counts, {0x24812: 1, 0x24814: 2})
        self.assertEqual(stats.instructions, 3)
        self.assertEqual(stats.conditional_skipped, 1)
        self.assertEqual(stats.fetch_failed, 1)
        self.assertNotIn(0, counts)  # Metadata and memory addresses are not PCs.

    def test_explicit_it_pc_wins_over_metadata(self):
        counts, _ = converter.count_pcs([
            "1 ps IT (deadbeef:00000000) 00024812 494e T16 LDR r1,[pc,#312]"
        ])
        self.assertEqual(counts, {0x24812: 1})

    def test_timestamp_and_it_layout_variants(self):
        lines = [
            "IT 00001000 2000 T16 MOVS r0,#0",
            "1 IT (9) 00001000 2000 T MOVS r0,#0",
            "2ps IT (00001000) 2000 T16 MOVS r0,#0",
            "3 ps IT (00001000:00000008) 2000 T16 MOVS r0,#0",
            "  ES (0x00001000:0x2000) T thrd: MOVS r0,#0",
            "4 tic ES (00001000:f2400000) T thrd: MOVW r0,#0",
            "4 tic ES (00001000:f2400000) T thrd: MOVW r0,#0",
        ]
        counts, stats = converter.count_pcs(lines)
        self.assertEqual(counts, {0x1000: len(lines)})
        self.assertEqual(stats.instructions, len(lines))

    def test_state_width_and_large_pc(self):
        counts, _ = converter.count_pcs([
            "IT ffff000000001000 d2800000 O EL1h: MOV x0,#0",
            "IT 00001000 e3a00000 A svc: MOV r0,#0",
            "IT 00001004 f2400000 T32 MOVW r0,#0",
        ])
        self.assertEqual(counts, {0xFFFF000000001000: 1, 0x1000: 1, 0x1004: 1})

    def test_malformed_fails_with_line_number(self):
        with self.assertRaisesRegex(converter.ConversionError, "line 2"):
            converter.count_pcs(["Tarmac Text Rev 3t", "3 tic ES (0000226fc: ....)"])

    def test_skip_malformed_is_explicit_and_counted(self):
        counts, stats = converter.count_pcs([
            "IT garbage", "IT 1000 2000 T16 MOVS r0,#0"
        ], skip_malformed=True)
        self.assertEqual(counts, {0x1000: 1})
        self.assertEqual(stats.malformed, 1)

    def test_selected_format_rejects_other_dialect(self):
        with self.assertRaisesRegex(converter.ConversionError, "--format es"):
            converter.count_pcs(["IT 1000 2000 T16 MOVS r0,#0"], input_format="es")

    def test_empty_or_only_skipped_input_is_an_error(self):
        for lines in ([], ["1 ps R r0 2000"], ["IS 1000 2000 T16 MOVS r0,#0"]):
            with self.subTest(lines=lines), self.assertRaisesRegex(converter.ConversionError, "no executed"):
                converter.count_pcs(lines)


class ProfileTests(unittest.TestCase):
    def test_instruction_boundaries_exclude_arm_literal_pool(self):
        self.assertEqual(converter.instruction_pcs(
            "Disassembly of section .text:\n"
            "00001000 <work>:\n"
            " 1000: movw r0, #0\n"
            " 1004: bx lr\n"
            " 1006: .short 0x0000\n"
            " 1008: .word 0x12345678\n"
            " 100c: <unknown>\n"
        ), {0x1000, 0x1004})

    def test_zero_pcs_on_same_line_do_not_replace_execution_cost(self):
        source = converter.Source("a.c", "work", 7)
        costs = converter.aggregate(Counter({0x1000: 3, 0x1002: 0}),
                                    {0x1000: source, 0x1002: source}, converter.Stats())
        self.assertEqual(costs[source], 3)

    def test_costs_aggregate_by_file_function_line_and_preserve_unknowns(self):
        counts = Counter({0x1000: 3, 0x1002: 4, 0x2000: 2, 0x3000: 1})
        sources = {
            0x1000: converter.Source("a.c", "work", 7),
            0x1002: converter.Source("a.c", "work", 7),
            0x2000: converter.Source("b.c", "work", 7),
            0x3000: converter.Source(),
        }
        stats = converter.Stats()
        costs = converter.aggregate(counts, sources, stats)
        self.assertEqual(costs[converter.Source("a.c", "work", 7)], 7)
        self.assertEqual(stats.unknown_source_instructions, 1)
        self.assertEqual(stats.unknown_function_instructions, 1)
        out = io.StringIO()
        converter.write_cachegrind(out, costs, Path("firmware.elf"))
        self.assertEqual(out.getvalue(),
            "desc: Tarmac flat instruction profile; no cache simulation\n"
            "cmd: firmware.elf\nevents: Ir\n"
            "fl=???\nfn=???\n0 1\n"
            "fl=a.c\nfn=work\n7 7\n"
            "fl=b.c\nfn=work\n7 2\nsummary: 10\n")

    def test_atomic_writer_keeps_existing_file_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile"
            path.write_text("original")
            with self.assertRaises(RuntimeError):
                with converter.atomic_text(path) as out:
                    out.write("partial")
                    raise RuntimeError("disk failure")
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_addr2line_discriminator_unknowns_and_unique_batches(self):
        result = subprocess.CompletedProcess([], 0, "foo()\nC:/src/main.cpp:12 (discriminator 2)\n??\n??:?\n", "")
        with patch.object(converter.subprocess, "run", return_value=result) as run:
            sources = converter.resolve_pcs([0x3000, 0x3002, 0x3000], Path("a.elf"), "addr2line", load_offset=0x2000)
        self.assertEqual(run.call_args[1]["input"], "0x1000\n0x1002\n")
        self.assertEqual(sources[0x3000], converter.Source("C:/src/main.cpp", "foo()", 12))
        self.assertEqual(sources[0x3002], converter.Source())

    def test_bad_symbolizer_output_and_negative_offset_fail(self):
        result = subprocess.CompletedProcess([], 0, "foo\n", "")
        with patch.object(converter.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(converter.ConversionError, "unexpected addr2line"):
                converter.resolve_pcs([0x1000], Path("a.elf"), "addr2line")
        with self.assertRaisesRegex(converter.ConversionError, "negative"):
            converter.resolve_pcs([0x1000], Path("a.elf"), "addr2line", load_offset=0x2000)


@unittest.skipUnless(all(shutil.which(tool) for tool in ("gcc", "addr2line", "nm", "objdump")),
                     "real ELF integration requires gcc, addr2line, nm and objdump")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        cls.source = cls.directory / "example.c"
        cls.elf = cls.directory / "example.elf"
        cls.source.write_text(
            "int work(int x) { return x + 1; }\n"
            "int main(void) { return work(3); }\n"
            "int never_called(void) { return 99; }\n"
            "int data_only = 123;\n"
        )
        subprocess.run(["gcc", "-g", "-O0", "-fno-inline", "-no-pie", str(cls.source), "-o", str(cls.elf)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        nm = subprocess.run(["nm", "-n", str(cls.elf)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        cls.pcs = {}
        for line in nm.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[2] in ("work", "main"):
                cls.pcs[fields[2]] = int(fields[0], 16)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def trace(self, dialect, offset=0):
        # Synthetic Arm instruction records at real, host-compiled ELF addresses.
        # The converter does not decode opcodes or execute the ELF.
        pcs = [self.pcs["work"] + offset] * 3 + [self.pcs["main"] + offset] * 2
        if dialect == "es":
            return "\n".join(f"{i} tic ES ({pc:08x}:2000) T thrd: MOVS r0,#0" for i, pc in enumerate(pcs))
        return "\n".join(f"{i} ps IT ({pc:08x}:00000000) {pc:08x} 2000 T16 MOVS r0,#0" for i, pc in enumerate(pcs))

    def run_cli(self, trace, output, *extra, stdin=None):
        return subprocess.run([
            sys.executable, str(ROOT / "tarmac_to_cachegrind.py"), str(trace),
            "--elf", str(self.elf), "--addr2line", shutil.which("addr2line"),
            "-o", str(output), *map(str, extra),
        ], input=stdin, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_two_dialects_produce_same_real_source_profile(self):
        profiles = []
        for dialect in ("es", "it"):
            with self.subTest(dialect=dialect):
                trace = self.directory / f"{dialect}.log"
                trace.write_text(self.trace(dialect))
                output = self.directory / f"{dialect}.out"
                stats = self.directory / f"{dialect}.json"
                audit = self.directory / f"{dialect}.csv"
                result = self.run_cli(trace, output, "--stats", stats, "--pc-counts", audit)
                self.assertEqual(result.returncode, 0, result.stderr)
                profile = output.read_text()
                profiles.append(profile)
                self.assertIn("fn=work\n1 3\n", profile)
                self.assertIn("fn=main\n2 2\n", profile)
                self.assertIn("fn=never_called\n3 0\n", profile)
                self.assertNotIn("fn=data_only", profile)
                self.assertTrue(profile.endswith("summary: 5\n"))
                values = json.loads(stats.read_text())
                self.assertEqual(values["instructions"], 5)
                self.assertEqual(values["unique_pcs"], 2)
                self.assertGreater(values["elf_instruction_pcs"], 2)
                self.assertEqual(values["unknown_source_instructions"], 0)
                with audit.open() as file:
                    rows = list(csv.DictReader(file))
                self.assertEqual(sum(int(row["Ir"]) for row in rows), 5)
                self.assertTrue(any(row["function"] == "never_called" and row["Ir"] == "0" for row in rows))
                if shutil.which("cg_annotate"):
                    annotated = subprocess.run(["cg_annotate", "--show=Ir", "--sort=Ir", str(output)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                    self.assertEqual(annotated.returncode, 0, annotated.stderr)
                    self.assertIn("PROGRAM TOTALS", annotated.stdout)
        self.assertEqual(profiles[0], profiles[1])

    def test_gzip_stdin_and_load_offset(self):
        offset = 0x20000000
        trace = self.directory / "relocated.log.gz"
        with gzip.open(trace, "wt") as out:
            out.write(self.trace("it", offset))
        for name, stdin in ((trace, None), ("-", self.trace("es", offset))):
            with self.subTest(input=name):
                output = self.directory / "relocated.out"
                result = self.run_cli(name, output, "--load-offset", hex(offset), stdin=stdin)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("fn=work\n1 3\n", output.read_text())
                self.assertIn("fn=never_called\n3 0\n", output.read_text())

    def test_unknown_pc_preserves_count_and_warns(self):
        output = self.directory / "unknown.out"
        result = self.run_cli("-", output, stdin="IT 00000000 2000 T16 MOVS r0,#0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fl=???\nfn=???\n0 1\n", output.read_text())
        self.assertTrue(output.read_text().endswith("summary: 1\n"))
        self.assertIn("warning:", result.stderr)

    def test_multiple_addr2line_batches(self):
        sources = converter.resolve_pcs(self.pcs.values(), self.elf, shutil.which("addr2line"), batch_size=1)
        self.assertEqual({s.function for s in sources.values()}, {"main", "work"})

    def test_empty_trace_produces_zero_coverage(self):
        output = self.directory / "empty.out"
        result = self.run_cli("-", output, stdin="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fn=never_called\n3 0\n", output.read_text())
        self.assertTrue(output.read_text().endswith("summary: 0\n"))
        self.assertIn("no executed instructions", result.stderr)

    def test_executed_only_does_not_require_objdump(self):
        output = self.directory / "executed.out"
        result = self.run_cli("-", output, "--executed-only", "--objdump", "/missing/objdump", stdin=self.trace("es"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("never_called", output.read_text())
        self.assertIn("fn=work\n1 3\n", output.read_text())

    def test_wrong_objdump_does_not_replace_output(self):
        output = self.directory / "bad-objdump.out"
        output.write_text("original")
        result = self.run_cli("-", output, "--objdump", "/missing/objdump", stdin=self.trace("it"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(output.read_text(), "original")

    def test_malformed_input_does_not_replace_output(self):
        output = self.directory / "preserved.out"
        output.write_text("original")
        result = self.run_cli("-", output, stdin="1 ps IT broken")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(output.read_text(), "original")
        self.assertIn("line 1", result.stderr)

    def test_input_output_collision_is_rejected(self):
        trace = self.directory / "collision.log"
        trace.write_text(self.trace("es"))
        original = trace.read_text()
        result = self.run_cli(trace, trace)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(trace.read_text(), original)

    def test_symbol_only_elf_keeps_function_and_unknown_line(self):
        elf = self.directory / "no_debug.elf"
        subprocess.run(["gcc", "-O0", "-no-pie", str(self.source), "-o", str(elf)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        nm = subprocess.run(["nm", str(elf)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        pc = next(int(line.split()[0], 16) for line in nm.stdout.splitlines() if line.endswith(" T work"))
        source = converter.resolve_pcs([pc], elf, shutil.which("addr2line"))[pc]
        self.assertEqual(source.function, "work")
        self.assertEqual(source.line, 0)


if __name__ == "__main__":
    unittest.main()
