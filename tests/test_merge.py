"""Selection-list merge of the converter's individual profiles; no ELF tools."""
from collections import Counter
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
from test_coverage import rows, expand


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='merge-test-')
        self.root = Path(self.temp.name)
        self.listing = self.root / 'selection.txt'
        self.output = self.root / 'selected_cachegrind.out'

    def tearDown(self):
        self.temp.cleanup()

    def profile(self, name, hits=None, elf='firmware.elf'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        hits = hits or {}
        costs = Counter({converter.Position(pc, 'test.c', 'work', line): hits.get(pc, 0)
                         for pc, line in ((0x100, 1), (0x102, 1), (0x104, 2))})
        with path.open('w') as file:
            converter.write_cachegrind(file, costs, Path(elf))
        return path

    def run_merge(self, *options):
        with patch('sys.stderr', io.StringIO()) as error:
            status = converter.main(['--merge-list', str(self.listing), '-o', str(self.output)] + list(options))
        return status, error.getvalue()

    def lookup(self):
        return json.loads(self.output.with_name(self.output.name + '.coverage_index.json').read_text())

    def test_relative_files_folders_dedup_and_preserved_sparse_indices(self):
        first = self.profile('space dir/1_a_cm4_cachegrind.out', {0x100: 1})
        self.profile('space dir/9_b_cm4_cachegrind.out', {0x104: 3})
        self.profile('space dir/2_other_cm0_cachegrind.out', {0x102: 7}, 'other.elf')
        (self.root / 'alias.out').symlink_to(first)
        self.listing.write_text('\ufeff# selection\n\nspace dir\nspace dir/1_a_cm4_cachegrind.out\nalias.out\n')
        status, error = self.run_merge('--merge-name', '*_cm4_cachegrind.out')
        self.assertEqual(status, 0, error)
        lookup = self.lookup()
        self.assertEqual([entry['index'] for entry in lookup['tests']], [1, 9])
        self.assertEqual(lookup['successful_tests'], 2)
        data = rows(self.output.read_text())
        self.assertEqual(data[0x100][:2], [1, 1])
        self.assertEqual(expand(data[0x100], 2, lookup['sets']), {1})
        self.assertEqual(expand(data[0x100], 8, lookup['sets']), {9})
        self.assertEqual(data[0x102], [0] * 14)
        self.assertEqual(data[0x104][0], 3)

    def test_exclusion_files_and_folders(self):
        self.profile('a/1_a_cm4_cachegrind.out', {0x100: 1})
        self.profile('a/2_b_cm4_cachegrind.out', {0x102: 2})
        self.profile('b/3_c_cm4_cachegrind.out', {0x104: 3})
        self.profile('a/deeper/4_deep_cm4_cachegrind.out', {0x104: 99})
        self.listing.write_text('a\nb\n')
        excluded = self.root / 'exclude.txt'
        excluded.write_text('a/2_b_cm4_cachegrind.out\nb\n')
        status, error = self.run_merge('--merge-pattern', '*_cm4_cachegrind.out', '--exclude-list', str(excluded))
        self.assertEqual(status, 0, error)
        self.assertEqual([entry['index'] for entry in self.lookup()['tests']], [1])
        data = rows(self.output.read_text())
        self.assertEqual(data[0x100][8:], [0] * 6)
        self.assertEqual(data[0x104], [0] * 14)

    def test_duplicate_ids_reindex_and_source_line_union(self):
        self.profile('a/1_a.out', {0x100: 2})
        self.profile('b/1_b.out', {0x102: 3})
        self.listing.write_text('a/1_a.out\nb/1_b.out\n')
        self.output.write_text('old total')
        status, error = self.run_merge()
        self.assertEqual(status, 1)
        self.assertIn('duplicate test index', error)
        self.assertEqual(self.output.read_text(), 'old total')
        status, error = self.run_merge('--reindex')
        self.assertEqual(status, 0, error)
        lookup = self.lookup()
        data = rows(self.output.read_text())
        self.assertEqual(data[0x100][1], 2)
        self.assertEqual(expand(data[0x100], 2, lookup['sets']), {1, 2})
        self.assertEqual(data[0x102][1:], [0] * 13)
        self.assertFalse(lookup['indices_preserved'])

    def test_large_display_ids_do_not_allocate_sparse_bitmasks(self):
        self.profile('1000000000000_big.out', {0x100: 1})
        self.profile('7_small.out', {0x102: 2})
        self.listing.write_text('1000000000000_big.out\n7_small.out\n')
        status, error = self.run_merge()
        self.assertEqual(status, 0, error)
        self.assertEqual(rows(self.output.read_text())[0x100][2:7], [7, 1000000000000, 0, 0, 0])

    def test_unnumbered_and_empty_selection_errors(self):
        self.profile('plain.out')
        self.listing.write_text('plain.out\n')
        status, error = self.run_merge()
        self.assertEqual(status, 1)
        self.assertIn('--reindex', error)
        self.assertEqual(self.run_merge('--reindex')[0], 0)
        self.listing.write_text('# empty\n')
        self.assertEqual(self.run_merge()[0], 1)
        self.listing.write_text('missing.out\n')
        self.assertEqual(self.run_merge()[0], 1)
        self.listing.write_text('.\n')
        self.assertIn('require --merge-pattern', self.run_merge()[1])

    def test_reject_mixed_objects_mappings_truncation_and_merged_events(self):
        first = self.profile('1_a.out', {0x100: 1})
        second = self.profile('2_b.out', {0x102: 1})
        self.listing.write_text('1_a.out\n2_b.out\n')
        valid = second.read_text()
        broken = [valid.replace('firmware.elf', 'other.elf'),
                  valid.replace('test.c', 'different.c'),
                  valid[:valid.index('summary:')],
                  valid.replace('summary: 1', 'summary: 9'),
                  valid.replace('events: Ir', 'events: Ir Tests'),
                  valid.replace('0x102 1 1', '0x102 1 -1')]
        for content in broken:
            with self.subTest(content=content[-80:]):
                second.write_text(content)
                self.output.write_text('keep')
                self.assertEqual(self.run_merge()[0], 1)
                self.assertEqual(self.output.read_text(), 'keep')
        self.listing.write_text('total_merge_cachegrind.out\n')
        shutil.copyfile(str(first), str(self.root / 'total_merge_cachegrind.out'))
        self.assertIn('individual profiles', self.run_merge()[1])

    def test_current_output_skipped_in_folder_and_hardlink_overwrite_rejected(self):
        first = self.profile('1_a_cachegrind.out', {0x100: 1})
        self.listing.write_text('.\n')
        for _ in range(2):
            status, error = self.run_merge('--merge-pattern', '*_cachegrind.out')
            self.assertEqual(status, 0, error)
            self.assertEqual(self.lookup()['successful_tests'], 1)
        import os
        self.output.unlink()
        os.link(str(first), str(self.output))
        self.listing.write_text('1_a_cachegrind.out\n')
        self.assertIn('aliases an input', self.run_merge()[1])
        self.assertEqual(first.read_text(), self.output.read_text())

    def test_standalone_without_elf_or_binutils(self):
        import os
        self.profile('1_a.out', {0x100: 1})
        self.listing.write_text('1_a.out\n')
        script = self.root / 'converter.py'
        shutil.copyfile(converter.__file__, str(script))
        result = subprocess.run([sys.executable, str(script), '--merge-list', str(self.listing),
                                 '-o', str(self.output), '--progress-stream', 'stdout'],
                                env=dict(os.environ, PATH=str(self.root / 'no-tools')), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[1/1] merged', result.stdout)
        self.assertEqual(self.lookup()['successful_tests'], 1)


if __name__ == '__main__':
    unittest.main()
