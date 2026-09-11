import io
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

    def test_800_logs_share_elf_analysis_and_merge_counts(self):
        root = self.fixture("large")
        for index in range(400):
            folder = root / "case{:03d}".format(index)
            self.log(folder / "tarmac_a.log")
            self.log(folder / "tarmac_b.log", "it")
        with patch.object(subprocess, "run", wraps=subprocess.run) as calls, patch("sys.stderr", io.StringIO()) as progress, patch("builtins.print", wraps=print) as prints:
            self.assertEqual(batch.main(self.args(root)), 0)
        self.assertIn('[1/800] complete "case000_tarmac_a_cachegrind.out"', progress.getvalue())
        self.assertIn('[800/800] complete "case399_tarmac_b_cachegrind.out"', progress.getvalue())
        self.assertIn('complete "total_merge_cachegrind.out"', progress.getvalue())
        completion_calls = [call for call in prints.call_args_list if 'complete "' in str(call[0][0])]
        self.assertEqual(len(completion_calls), 802)  # 800 profiles, total, report
        self.assertTrue(all(call[1].get("flush") is True for call in completion_calls))
        # One objdump plus one addr2line batch for this small ELF, not 800 each.
        self.assertEqual(calls.call_count, 2)
        output = root / "cachegrind-output"
        self.assertEqual(len(list(output.glob("*_cachegrind.out"))), 801)
        merged = (output / "total_merge_cachegrind.out").read_text()
        self.assertTrue(merged.startswith("# callgrind format\n"))
        self.assertIn("positions: instr line\nevents: Ir\n", merged)
        self.assertIn("0x{:x} 1 800\n".format(self.pcs["work"]), merged)
        self.assertRegex(merged, r"fn=work\n0x[0-9a-f]+ 1 800\n")
        self.assertRegex(merged, r"fn=never_called\n0x[0-9a-f]+ 3 0\n")
        self.assertTrue(merged.endswith("summary: 800\n"))
        report = json.loads((output / "batch_report.json").read_text())
        self.assertEqual(len(report["successful"]), 800)
        self.assertEqual(report["failed"], [])
        self.assertTrue((output / "case000_tarmac_a_cachegrind.out").exists())

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
        self.assertTrue((output / "nested_deep_tarmac_good_cachegrind.out").exists())
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
        self.assertEqual({p.name for p in output.iterdir()}, {"batch_report.json", "total_merge_cachegrind.out"})
        self.assertTrue((output / "total_merge_cachegrind.out").read_text().endswith("summary: 1\n"))

    def test_colliding_flat_names_rejected(self):
        root = self.fixture("collision")
        self.log(root / "a_b" / "tarmac_x.log")
        self.log(root / "a" / "b" / "tarmac_x.log")
        with patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(batch.main(self.args(root, "--max-depth", "2")), 1)
        self.assertIn("collision", err.getvalue())
        self.assertFalse((root / "cachegrind-output").exists())

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
        self.assertIn("0x{:x} 1 1\n".format(other_pc), expected1)
        self.assertIn('ob: "{}"'.format(other_elf), expected1)
        merge0.write_text("old")
        individual = output / "case_tarmac_core0_cachegrind.out"
        individual.write_text("old")
        (output / "keep.txt").write_text("keep")
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(batch.main(args0), 0)
        self.assertIn("0x{:x} 1 1\n".format(self.pcs["work"]), merge0.read_text())
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
        self.assertEqual(len(report["preserved_previous_outputs"]), 2)
        self.assertIn("total_merge_tarmac_cachegrind.out", report["preserved_previous_outputs"])

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
        merged = (root / "cachegrind-output" / "total_merge_cachegrind.out").read_text()
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
