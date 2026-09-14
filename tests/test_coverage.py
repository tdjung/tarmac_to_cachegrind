"""PC coverage attribution, zero-Ir suppression, sparse IDs and overflow references."""
from collections import Counter
import io
import json
from pathlib import Path
import unittest

import tarmac_to_cachegrind as converter


def rows(text):
    result = {}
    for line in text.splitlines():
        if line.startswith('0x'):
            values = line.split()
            result[int(values[0], 16)] = list(map(int, values[2:]))
    return result


def expand(values, start, sets):
    first = {value for value in values[start:start + 5] if value}
    set_id = values[start + 5]
    return first | set(sets[str(set_id)] if set_id else [])


def context_for(sources):
    context = converter.ProfileContext.__new__(converter.ProfileContext)
    context.elf = Path('firmware.elf')
    context.sources = sources
    context.baseline = Counter({converter.Position(pc, *source): 0 for pc, source in sources.items()})
    context.baseline_pcs = set(sources)
    context.layout = context.make_layout(context.baseline)
    return context


class CoverageTests(unittest.TestCase):
    def test_independent_pcs_on_same_line_and_failed_ids_excluded(self):
        context = context_for({
            0x100: converter.Source('a.c', 'foo', 1),
            0x102: converter.Source('a.c', 'foo', 1),
            0x104: converter.Source('a.c', 'foo', 2),
            0x106: converter.Source('a.c', 'foo', 3),
            0x108: converter.Source(), 0x110: converter.Source(),
        })
        coverage, total = converter.CoverageIndex(), Counter()
        successful = set(range(1, 15)) - {7}
        for index in sorted(successful):
            counts = Counter({0x106: 1})
            if index <= 8:
                counts[0x100] = 3
            if index in (2, 14):
                counts[0x102] = 5
            if index in (1, 2):
                counts[0x108 if index == 1 else 0x110] = 1
            total.update(counts)
            coverage.add(index, counts)
        out = io.StringIO()
        coverage.write(out, context, total)
        data = rows(out.getvalue())
        expected = {1, 2, 3, 4, 5, 6, 8}
        self.assertEqual(data[0x100][1], len(expected))
        self.assertEqual(expand(data[0x100], 2, coverage.sets), expected)
        self.assertEqual(expand(data[0x100], 8, coverage.sets), successful - expected)
        self.assertEqual(data[0x102][1], 2)
        self.assertEqual(expand(data[0x102], 2, coverage.sets), {2, 14})
        self.assertEqual(expand(data[0x102], 8, coverage.sets), successful - {2, 14})
        self.assertEqual(data[0x104], [0] * 14)
        self.assertEqual(expand(data[0x106], 2, coverage.sets), successful)
        self.assertEqual(data[0x106][8:], [0] * 6)
        self.assertEqual(expand(data[0x108], 2, coverage.sets), {1})
        self.assertEqual(expand(data[0x110], 2, coverage.sets), {2})
        for pc, values in data.items():
            self.assertEqual(values[0], total.get(pc, 0))
            self.assertEqual(len(values), 14)
        summary = list(map(int, out.getvalue().split('summary: ')[1].split()))
        self.assertEqual(summary, [sum(values[i] for values in data.values()) for i in range(14)])

    def test_zero_ir_and_later_instruction_on_same_line(self):
        context = context_for({pc: converter.Source('a.c', 'foo', 1) for pc in (0x100, 0x102, 0x104)})
        coverage = converter.CoverageIndex()
        coverage.add(1, Counter({0x102: 2, 0x104: 1}))
        # Normal zero-execution tests are still uncovered for executed PCs.
        for index in range(2, 9):
            coverage.add(index, Counter())
        out = io.StringIO()
        coverage.write(out, context, Counter({0x102: 2, 0x104: 1}))
        data = rows(out.getvalue())
        self.assertEqual(data[0x100], [0] * 14)
        self.assertEqual(data[0x102][1:], data[0x104][1:])
        self.assertEqual(expand(data[0x102], 2, coverage.sets), {1})
        self.assertEqual(expand(data[0x102], 8, coverage.sets), set(range(2, 9)))
        zero_coverage = converter.CoverageIndex()
        for index in range(1, 9):
            zero_coverage.add(index, Counter())
        out = io.StringIO()
        zero_coverage.write(out, context, Counter())
        self.assertTrue(all(values == [0] * 14 for values in rows(out.getvalue()).values()))
        self.assertEqual(zero_coverage.sets, {})

    def test_slots_padding_and_only_remaining_indices_in_set(self):
        coverage = converter.CoverageIndex()
        self.assertEqual(coverage.slots(0), [0] * 6)
        self.assertEqual(coverage.slots(1 << 449), [450, 0, 0, 0, 0, 0])
        slots = coverage.slots(sum(1 << (i - 1) for i in (1, 2, 3, 4, 5, 17, 305)))
        self.assertEqual(slots[:5], [1, 2, 3, 4, 5])
        self.assertEqual(coverage.sets[str(slots[5])], [17, 305])
        # Identical remaining lists share a reference even with different first five.
        other = coverage.slots(sum(1 << (i - 1) for i in (6, 7, 8, 9, 10, 17, 305)))
        self.assertEqual(other[5], slots[5])

    def test_450_tests_and_nearly_universal_coverage(self):
        context = context_for({0x100: converter.Source('a.c', 'foo', 1)})
        coverage, total = converter.CoverageIndex(), Counter()
        for index in range(1, 451):
            counts = Counter() if index in (17, 305) else Counter({0x100: 2})
            coverage.add(index, counts)
            total.update(counts)
        out = io.StringIO()
        coverage.write(out, context, total)
        values = rows(out.getvalue())[0x100]
        self.assertEqual(values[:7], [896, 448, 1, 2, 3, 4, 5])
        self.assertEqual(values[8:], [17, 305, 0, 0, 0, 0])
        self.assertEqual(len(coverage.sets[str(values[7])]), 443)
        self.assertEqual(expand(values, 2, coverage.sets), set(range(1, 451)) - {17, 305})


if __name__ == '__main__':
    unittest.main()
