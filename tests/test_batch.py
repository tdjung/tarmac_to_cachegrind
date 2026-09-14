import io
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import tarmac_to_cachegrind as batch
import test_converter as single_tests

ROOT = single_tests.ROOT


def ir_profile(text):
    """Project an enriched merge onto the legacy Ir profile for regression checks."""
    lines = []
    for line in text.splitlines():
        if line.startswith("events:"):
            line = "events: Ir"
        elif line.startswith("0x"):
            line = " ".join(line.split()[:3])
        elif line.startswith("summary:"):
            line = " ".join(line.split()[:2])
        lines.append(line)
    return "\n".join(lines) + "\n"



@unittest.skipUnless(all(shutil.which(t) for t in ("gcc", "nm", "addr2line", "objdump")),
                     "batch integration requires gcc and binutils")
class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        single_tests.IntegrationTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def fixture(self, name):
        root = self.directory / name
        root.mkdir()
        return root

    def log(self, path, dialect="es"):
        path.parent.mkdir(parents=True, exist_ok=True)
        pc = self.pcs["work"]
        if dialect == "es":
            text = "1 tic ES ({:08x}:2000) T thrd: MOVS r0,#0".format(pc)
        else:
            text = "1 ps IT ({:08x}:00000000) {:08x} 2000 T16 MOVS r0,#0".format(pc, pc)
        path.write_text(text)

    def args(self, root, *extra):
        return [str(root), "--elf", str(self.elf), "--addr2line", shutil.which("addr2line"),
                "--objdump", shutil.which("objdump")] + list(extra)

    def test_parallel_matches_serial_with_gzip_unknown_pc_and_failures(self):
        root = self.fixture("parallel")
        for index in range(9):
            path = root / "case{}".format(index) / "tarmac.log"
            self.log(path, "es" if index % 2 else "it")
            path.write_text((path.read_text() + "\n") * (index + 1))
        unknown = root / "tarmac_unknown.log.gz"
        with gzip.open(str(unknown), "wt") as file:
            file.write("IT fffffffe 2000 T16 MOVS r0,#0\n" * 3)
        (root / "tarmac_bad.log").write_text("ES malformed")
        (root / "tarmac_zero.log").write_text("ES EXC [1] Reset\n")
        outputs = [self.directory / "serial-output", self.directory / "parallel-output"]
        for workers, output in zip((1, 3), outputs):
            output.mkdir()
            old_name = "10_parallel_tarmac_bad_cachegrind.out"
            (output / old_name).write_text("previous failed output")
            result = subprocess.run([sys.executable, str(ROOT / "tarmac_to_cachegrind.py")] +
                                    self.args(root, "--workers", str(workers), "-o", str(output)),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("[12/12]", result.stderr)
            self.assertEqual((output / old_name).read_text(), "previous failed output")
        for path in outputs[0].glob("*.out"):
            self.assertEqual(path.read_bytes(), (outputs[1] / path.name).read_bytes(), path.name)
        reports = [json.loads((output / "batch_report.json").read_text()) for output in outputs]
        for report in reports:
            self.assertEqual(report["total_instructions"], 48)
            self.assertEqual(len(report["failed"]), 1)
            self.assertTrue(report["partial_merge"])
            self.assertGreaterEqual(report["merge_seconds"], 0)
            for entry in report["successful"]:
                self.assertGreaterEqual(entry.pop("seconds")["parse"], 0)
        self.assertEqual(reports[0]["successful"], reports[1]["successful"])

    def test_parallel_merge_only_executed_only_and_spawn(self):
        root = self.fixture("spawn")
        for index in range(5):
            self.log(root / str(index) / "tarmac_core0.log", "it")
        # Exercise initializer/pickling even on Linux, with a real standalone CLI.
        runner = self.directory / "spawn_runner.py"
        runner.write_text("import multiprocessing, sys\nsys.path.insert(0, {!r})\n"
                          "import tarmac_to_cachegrind as m\n"
                          "if __name__ == '__main__':\n"
                          "    multiprocessing.set_start_method('spawn')\n"
                          "    sys.exit(m.main())\n".format(str(ROOT)))
        result = subprocess.run([sys.executable, str(runner)] + self.args(
            root, "--workers", "2", "--merge-only", "--executed-only",
            "--log-name", "tarmac_core0.log"), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = root / "cachegrind-output"
        self.assertEqual(len(list(output.iterdir())), 3)
        merged = ir_profile((output / "total_merge_tarmac_core0_cachegrind.out").read_text())
        self.assertTrue(merged.endswith("summary: 5\n"))
        self.assertNotIn("fn=never_called", merged)

    def test_invalid_workers_rejected(self):
        for workers in ("0", "-1"):
            with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                batch.build_parser().parse_args(["logs", "--elf", "test.elf", "--workers", workers])

    def test_parallel_progress_stdout_and_merge_only(self):
        root = self.fixture("stdout-progress")
        for index in range(3):
            self.log(root / str(index) / "tarmac.log")
        for merge_only in (False, True):
            options = ["--workers", "2", "--progress-stream", "stdout"]
            if merge_only:
                options.append("--merge-only")
            result = subprocess.run([sys.executable, str(ROOT / "tarmac_to_cachegrind.py")] +
                                    self.args(root, *options), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[0/3] starting", result.stdout)
            for index in range(1, 4):
                self.assertIn("[{}/3] {}".format(index, "processed" if merge_only else "complete"), result.stdout)
            self.assertIn("[3/3] generating merged profile", result.stdout)
            self.assertIn("Completed: 3 succeeded, 0 failed", result.stdout)
            self.assertNotIn("complete", result.stderr)

    def test_waiting_progress_keeps_completed_count_without_sleep(self):
        args = batch.build_parser().parse_args(["logs", "--elf", "test.elf", "--progress-stream", "stdout"])
        tasks = [(Path("a"), Path("a.out")), (Path("b"), Path("b.out"))]
        with patch.object(batch.multiprocessing, "Pool"), patch.object(batch.queue, "Queue") as queue_factory, \
                patch("sys.stdout", io.StringIO()) as output, patch("builtins.print", wraps=print) as prints:
            ready = queue_factory.return_value
            ready.get.side_effect = [batch.queue.Empty(), (True, "first"),
                                     batch.queue.Empty(), (True, "second")]
            self.assertEqual(list(batch.batch_results(tasks, None, args, 2)), ["first", "second"])
            self.assertIn("[0/2] waiting for workers", output.getvalue())
            self.assertIn("[1/2] waiting for workers", output.getvalue())
            self.assertNotIn("[2/2] waiting", output.getvalue())
            self.assertTrue(all(call[1]["timeout"] == 5.0 for call in ready.get.call_args_list))
            self.assertTrue(all(call[1]["flush"] for call in prints.call_args_list))

    def test_coverage_manifest_and_sets_are_deterministic_across_workers(self):
        from test_coverage import rows, expand
        root = self.fixture("attribution")
        for index in range(1, 15):
            path = root / "case{:03d}".format(index) / "tarmac_core0.log"
            self.log(path, "it" if index % 2 else "es")
            if index == 7:
                path.write_text("IT broken")
            elif index == 10:
                path.write_text("ES EXC [1] Reset")
        previous = None
        for workers in (1, 3):
            output = self.directory / "attribution-output{}".format(workers)
            with patch("sys.stderr", io.StringIO()):
                self.assertEqual(batch.main(self.args(root, "--workers", str(workers),
                                                     "--log-name", "tarmac_core0.log", "-o", str(output))), 1)
            merged = (output / "total_merge_tarmac_core0_cachegrind.out").read_text()
            index_text = (output / "coverage_index_tarmac_core0.json").read_text()
            if previous is not None:
                self.assertEqual((merged, index_text), previous)
            previous = merged, index_text
            lookup = json.loads(index_text)
            self.assertEqual((lookup["successful_tests"], lookup["failed_tests"]), (13, 1))
            self.assertEqual([entry["index"] for entry in lookup["tests"]], list(range(1, 15)))
            self.assertEqual(lookup["tests"][6]["status"], "failed")
            self.assertIsNone(lookup["tests"][6]["output"])
            self.assertEqual(lookup["tests"][0]["output"], "1_case001_tarmac_core0_cachegrind.out")
            self.assertEqual(lookup["tests"][0]["input"], "case001/tarmac_core0.log")
            self.assertFalse((output / "7_case007_tarmac_core0_cachegrind.out").exists())
            values = rows(merged)[self.pcs["work"]]
            self.assertEqual(values[1], 12)
            self.assertEqual(expand(values, 2, lookup["sets"]), set(range(1, 15)) - {7, 10})
            self.assertEqual(expand(values, 8, lookup["sets"]), {10})

    def test_800_logs_share_elf_analysis_and_merge_counts(self):
        root = self.fixture("large")
        for index in range(400):
            folder = root / "case{:03d}".format(index)
            self.log(folder / "tarmac_a.log")
            self.log(folder / "tarmac_b.log", "it")
        with patch.object(subprocess, "run", wraps=subprocess.run) as calls, patch("sys.stderr", io.StringIO()) as progress, patch("builtins.print", wraps=print) as prints:
            self.assertEqual(batch.main(self.args(root)), 0)
        self.assertIn('[1/800] complete "1_case000_tarmac_a_cachegrind.out"', progress.getvalue())
        self.assertIn('[800/800] complete "800_case399_tarmac_b_cachegrind.out"', progress.getvalue())
        self.assertIn('complete "total_merge_cachegrind.out"', progress.getvalue())
        completion_calls = [call for call in prints.call_args_list if 'complete "' in str(call[0][0])]
        self.assertEqual(len(completion_calls), 803)  # 800 profiles, total, index, report
        self.assertTrue(all(call[1].get("flush") is True for call in completion_calls))
        # One objdump plus one addr2line batch for this small ELF, not 800 each.
        self.assertEqual(calls.call_count, 2)
        output = root / "cachegrind-output"
        self.assertEqual(len(list(output.glob("*_cachegrind.out"))), 801)
        merged = ir_profile((output / "total_merge_cachegrind.out").read_text())
        self.assertTrue(merged.startswith("# callgrind format\n"))
        self.assertIn("positions: instr line\nevents: Ir\n", merged)
        self.assertIn("0x{:x} 1 800\n".format(self.pcs["work"]), merged)
        self.assertRegex(merged, r"fn=work\n0x[0-9a-f]+ 1 800\n")
        self.assertRegex(merged, r"fn=never_called\n0x[0-9a-f]+ 3 0\n")
        self.assertTrue(merged.endswith("summary: 800\n"))
        report = json.loads((output / "batch_report.json").read_text())
        self.assertEqual(len(report["successful"]), 800)
        self.assertEqual(report["failed"], [])
        self.assertTrue((output / "1_case000_tarmac_a_cachegrind.out").exists())

    def test_non_tarmac_ignored_and_failed_candidate_reported(self):
        root = self.fixture("partial")
        self.log(root / "nested" / "deep" / "tarmac_good.log")
        (root / "build.log").write_text("unrelated")
        (root / "tarmac_noise.log").write_text("unrelated")
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(batch.main(self.args(root, "--max-depth", "2")), 1)
        output = root / "cachegrind-output"
        report = json.loads((output / "batch_report.json").read_text())
        self.assertEqual(report["matched"], 2)
        self.assertEqual(len(report["failed"]), 1)
        self.assertTrue((output / "1_nested_deep_tarmac_good_cachegrind.out").exists())
        self.assertTrue(report["partial_merge"])
        self.assertNotIn("desc:", (output / "total_merge_cachegrind.out").read_text())
        self.assertTrue((output / "total_merge_cachegrind.out").read_text().startswith("# callgrind format\n"))

    def test_pattern_merge_only_rerun_replaces_not_accumulates(self):
        root = self.fixture("pattern")
        self.log(root / "case" / "tarmac_core0.log")
        self.log(root / "case" / "tarmac_core1.log")
        args = self.args(root, "--pattern", "tarmac_core0*.log", "--merge-only")
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(batch.main(args), 0)
            self.assertEqual(batch.main(args), 0)
        output = root / "cachegrind-output"
        self.assertEqual({p.name for p in output.iterdir()}, {"batch_report.json", "total_merge_cachegrind.out", "coverage_index.json"})
        self.assertTrue(ir_profile((output / "total_merge_cachegrind.out").read_text()).endswith("summary: 1\n"))

    def test_indices_prevent_colliding_flat_names(self):
        root = self.fixture("collision")
        self.log(root / "a_b" / "tarmac_x.log")
        self.log(root / "a" / "b" / "tarmac_x.log")
        with patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(batch.main(self.args(root, "--max-depth", "2")), 0)
        self.assertTrue((root / "cachegrind-output" / "1_a_b_tarmac_x_cachegrind.out").exists())
        self.assertTrue((root / "cachegrind-output" / "2_a_b_tarmac_x_cachegrind.out").exists())

    def test_depth_limit_prunes_before_opening_deeper_directories(self):
        root = self.fixture("depth")
        self.log(root / "tarmac_root.log")
        self.log(root / "a" / "tarmac_child.log")
        self.log(root / "a" / "b" / "tarmac_deep.log")
        output = root / "out"
        output.mkdir()
        self.log(output / "tarmac_generated.log")
        for depth, expected in ((0, 1), (1, 2), (2, 3)):
            with self.subTest(depth=depth), patch.object(os, "scandir", wraps=os.scandir) as scan:
                found = batch.discover(root, ["tarmac*.log"], output, depth)
                self.assertEqual(len(found), expected)
                opened = {Path(call[0][0]) for call in scan.call_args_list}
                self.assertNotIn(output, opened)
                if depth < 2:
                    self.assertNotIn(root / "a" / "b", opened)
                if depth == 0:
                    self.assertEqual(opened, {root})
        self.assertEqual(len(batch.discover(root, ["tarmac*.log"], output)), 2)

    def test_negative_depth_is_rejected(self):
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            batch.build_parser().parse_args(["logs", "--elf", "firmware.elf", "--max-depth", "-1"])

    def test_exact_names_use_two_elfs_and_preserve_both_merges_on_rerun(self):
        root = self.fixture("two-cores")
        self.log(root / "case" / "tarmac_core0.log")
        other_elf = self.directory / "other.elf"
        subprocess.run(["gcc", "-g", "-O0", "-no-pie", "-Wl,-Ttext=0x600000",
                        str(self.source), "-o", str(other_elf)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        nm = subprocess.run(["nm", str(other_elf)], check=True, universal_newlines=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        other_pc = next(int(line.split()[0], 16) for line in nm.stdout.splitlines() if line.endswith(" T work"))
        self.assertNotEqual(other_pc, self.pcs["work"])
        other_log = root / "case" / "tarmac_core1.log"
        self.log(other_log, "it")
        other_log.write_text(other_log.read_text().replace("{:08x}".format(self.pcs["work"]), "{:08x}".format(other_pc)))
        args0 = self.args(root, "--log-name", "tarmac_core0.log")
        args1 = self.args(root, "--log-name", "tarmac_core1.log", "--elf", str(other_elf))
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(batch.main(args0), 0)
            self.assertEqual(batch.main(args1), 0)
        output = root / "cachegrind-output"
        merge0 = output / "total_merge_tarmac_core0_cachegrind.out"
        merge1 = output / "total_merge_tarmac_core1_cachegrind.out"
        expected1 = merge1.read_text()
        self.assertIn("0x{:x} 1 1\n".format(other_pc), ir_profile(expected1))
        self.assertIn('ob: "{}"'.format(other_elf), expected1)
        merge0.write_text("old")
        individual = output / "1_case_tarmac_core0_cachegrind.out"
        individual.write_text("old")
        (output / "keep.txt").write_text("keep")
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(batch.main(args0), 0)
        self.assertIn("0x{:x} 1 1\n".format(self.pcs["work"]), ir_profile(merge0.read_text()))
        self.assertTrue(individual.read_text().endswith("summary: 1\n"))
        self.assertEqual(merge1.read_text(), expected1)
        self.assertEqual((output / "keep.txt").read_text(), "keep")
        for core in (0, 1):
            report = json.loads((output / "batch_report_tarmac_core{}.json".format(core)).read_text())
            self.assertEqual(report["matched"], 1)
            self.assertEqual(report["log_name"], "tarmac_core{}.log".format(core))

    def test_log_name_is_literal_not_glob(self):
        root = self.fixture("literal")
        self.log(root / "tarmac[0].log")
        self.log(root / "tarmac0.log")
        found = batch.discover(root, [], root / "out", log_name="tarmac[0].log")
        self.assertEqual([p.name for p in found], ["tarmac[0].log"])

    def test_log_name_rejects_path_and_conflicting_pattern(self):
        for options in (["--log-name", "a/b.log"],
                        ["--log-name", "tarmac.log", "--pattern", "*.log"]):
            with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                batch.build_parser().parse_args(["logs", "--elf", "x.elf"] + options)

    def test_failed_rerun_reports_preserved_old_files(self):
        root = self.fixture("failed-rerun")
        log = root / "tarmac.log"
        self.log(log)
        args = self.args(root, "--log-name", "tarmac.log")
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(batch.main(args), 0)
            log.write_text("IT broken")
            self.assertEqual(batch.main(args), 1)
        output = root / "cachegrind-output"
        report = json.loads((output / "batch_report_tarmac.json").read_text())
        self.assertIsNone(report["merged_output"])
        self.assertEqual(len(report["preserved_previous_outputs"]), 3)
        self.assertIn("total_merge_tarmac_cachegrind.out", report["preserved_previous_outputs"])

    def test_index_write_failure_preserves_previous_total(self):
        root = self.fixture("blocked-index")
        self.log(root / "tarmac.log")
        output = root / "cachegrind-output"
        output.mkdir()
        total = output / "total_merge_cachegrind.out"
        total.write_text("previous total")
        (output / "coverage_index.json").mkdir()
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(batch.main(self.args(root)), 1)
        self.assertEqual(total.read_text(), "previous total")

    def test_real_cli_fallback(self):
        root = self.fixture("fallback")
        self.log(root / "tarmac_one.log")
        result = subprocess.run([
            sys.executable, str(ROOT / "tarmac_to_cachegrind.py"), str(root),
            "--elf", str(self.elf), "--objdump", shutil.which("objdump"),
        ], env=dict(os.environ, PATH=str(root / "missing-tools")),
            universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("using objdump -l", result.stderr)
        merged = ir_profile((root / "cachegrind-output" / "total_merge_cachegrind.out").read_text())
        self.assertRegex(merged, r"fn=work\n0x[0-9a-f]+ 1 1\n")

    def test_copied_single_script_supports_file_and_directory(self):
        root = self.fixture("standalone")
        script = root / "converter.py"
        shutil.copyfile(str(ROOT / "tarmac_to_cachegrind.py"), str(script))
        logs = root / "logs"
        self.log(logs / "tarmac_one.log")
        for source, output in ((logs / "tarmac_one.log", root / "single.out"),
                               (logs, root / "batch-out")):
            result = subprocess.run([
                sys.executable, str(script), str(source), "--elf", str(self.elf),
                "--addr2line", shutil.which("addr2line"), "--objdump", shutil.which("objdump"),
                "-o", str(output),
            ], cwd=str(root), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("complete ", result.stderr)
        self.assertRegex((root / "single.out").read_text(), r"fn=work\n0x[0-9a-f]+ 1 1\n")
        self.assertTrue((root / "batch-out" / "total_merge_cachegrind.out").exists())


if __name__ == "__main__":
    unittest.main()
