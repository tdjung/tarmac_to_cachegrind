"""Experimental direct parser comparison; production converter is unchanged.

Run with Python 3.6+: python3 tests/benchmark_parser.py --output results.json
Input construction and correctness checks are outside the timed sections.
"""
import argparse
from collections import Counter
from functools import lru_cache, partial
import json
from pathlib import Path
import statistics
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tarmac_to_cachegrind as reference

EVENTS = frozenset(('ES', 'IT', 'IF', 'IS'))
UNITS = frozenset(('tic', 'ps', 'ns', 'clk', 'cs', 'cyc'))
STATES = frozenset(('T16', 'T32', 'T', 'A', 'O'))
HEX = '0123456789abcdefABCDEF'


def hex_text(value):
    if value.startswith(('0x', '0X')):
        value = value[2:]
    return bool(value) and not value.strip(HEX)


def direct_decode(event, body, to_pc):
    # Common formats use string operations. Unusual supported layouts and
    # ambiguous boundary cases go through the original validated decoder.
    if event == 'IS':
        return None, 'conditional_skipped'
    if event == 'ES':
        if not body.startswith('('):
            return reference.decode_instruction(event, body)
        address, sep, rest = body[1:].partition(')')
        if not sep or not rest or not rest[0].isspace():
            return reference.decode_instruction(event, body)
        pc, colon, opcode = address.partition(':')
        pc, opcode = pc.strip(), opcode.strip()
        state_tail = rest.split(None, 1)
        if not colon or not state_tail or state_tail[0] not in STATES:
            return reference.decode_instruction(event, body)
        tail = state_tail[1] if len(state_tail) > 1 else ''
        # Keep exact regex word-boundary semantics for this rare flag.
        if 'CCFAIL' in tail:
            return reference.decode_instruction(event, body)
    else:
        rest = body
        if rest.startswith('('):
            _, sep, rest = rest.partition(')')
            if not sep or not rest or not rest[0].isspace():
                return reference.decode_instruction(event, body)
        fields = rest.split(None, 3)
        if len(fields) < 3 or fields[2] not in STATES:
            return reference.decode_instruction(event, body)
        pc, opcode = fields[:2]
    if not hex_text(pc):
        return reference.decode_instruction(event, body)
    encoding = opcode[2:] if opcode.startswith(('0x', '0X')) else opcode
    if len(encoding) not in (4, 8) or not encoding or encoding.strip(HEX):
        return reference.decode_instruction(event, body)
    return to_pc(pc), None


def count_direct(lines, *, input_format='auto', skip_malformed=False,
                 allow_empty=False, pc_cache=False, body_cache=False):
    counts, stats = Counter(), reference.Stats()
    to_pc = lambda pc: int(pc, 16)
    if pc_cache:
        to_pc = lru_cache(maxsize=8192)(to_pc)
    decode = lru_cache(maxsize=8192)(lambda event, body: direct_decode(event, body, to_pc)) if body_cache else None
    for number, line in enumerate(lines, 1):
        stats.lines += 1
        if 'ES' not in line and 'IT' not in line and 'IF' not in line and 'IS' not in line:
            stats.ignored += 1
            continue
        fields = line.split(None, 3)
        if (len(fields) == 4 and fields[0].isdecimal() and
                fields[1] in UNITS and fields[2] in EVENTS and
                '\n' not in fields[3].rstrip('\n')):
            event, body = fields[2], fields[3].rstrip('\n')
        else:
            matched = reference.EVENT.match(line)
            if not matched:
                stats.ignored += 1
                continue
            event, body = matched.group('event', 'body')
        stats.events[event] = stats.events.get(event, 0) + 1
        family = 'es' if event == 'ES' else 'it'
        if input_format != 'auto' and input_format != family:
            raise reference.ConversionError(
                'line {}: found {} in --format {}; use --format auto or the correct input file'.format(
                    number, event, input_format))
        pc, disposition = decode(event, body) if decode else direct_decode(event, body, to_pc)
        if disposition:
            setattr(stats, disposition, getattr(stats, disposition) + 1)
            if disposition == 'malformed' and not skip_malformed:
                raise reference.ConversionError(
                    'line {}: malformed/unsupported {} instruction; check the trace dialect, '
                    'or use --skip-malformed to omit it'.format(number, event))
            continue
        counts[pc] += 1
        stats.instructions += 1
    stats.unique_pcs = len(counts)
    if not counts and not allow_empty:
        raise reference.ConversionError(
            'no executed instructions found; expected ES (pc:opcode) state ... '
            'or IT [metadata] pc opcode state ...')
    return counts, stats


MODES = [('regex_body_cache', reference.count_pcs),
         ('direct', count_direct),
         ('direct_pc_cache', partial(count_direct, pc_cache=True)),
         ('direct_body_cache', partial(count_direct, body_cache=True))]


def outcome(parser, lines, **options):
    try:
        counts, stats = parser(lines, **options)
        return dict(counts), vars(stats)
    except reference.ConversionError as error:
        return str(error)


def validate():
    # Run existing parser regression cases against all candidates.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_converter
    original = reference.count_pcs
    try:
        for _, parser in MODES[1:]:
            reference.count_pcs = parser
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_converter.ParserTests)
            result = unittest.TextTestRunner(verbosity=0).run(suite)
            if not result.wasSuccessful():
                raise AssertionError('candidate failed parser regressions')
    finally:
        reference.count_pcs = original
    # Differential checks include malformed addresses/opcodes and fallback
    # layouts, since omitting validation would make the comparison unfair.
    bodies = [
        '(1000:2000) T MOVS r0,#0', '(1000:2000) T MOVS r0,#0 CCFAIL',
        '(1000:2000) T MOVS r0,#0 xCCFAIL', '(1000:....) T MOVS r0,#0',
        '(1000:12345) T MOVS r0,#0', '(1_000:2000) T MOVS r0,#0',
        '(0x1000:0X2000) T16 MOVS r0,#0', '(1000:2000) T: MOVS r0,#0',
        'EXC [1] Reset', 'EXCEPTION', 'EXC[1]', 'garbage', '',
        '(dead:0) 1000 2000 T16 MOVS r0,#0', '(53) 1000 2000 T MOVS r0,#0',
        '(1000:0) 2000 T16 MOVS r0,#0', '(1000) 2000 T16 MOVS r0,#0',
        '1000 2000 T16 MOVS r0,#0', '(1)1000 2000 T16 MOVS r0,#0',
        '1000 +200 T MOVS r0,#0', '1000 20_0 T MOVS r0,#0',
    ]
    checked = 0
    for event in EVENTS:
        for prefix in ('', '1 ', '1 ps ', '  123 tic ', '2ps ', 'bad ', '1 xx '):
            for body in bodies:
                line = prefix + event + ' ' + body + '\n'
                for options in ({'allow_empty': True}, {'allow_empty': True, 'skip_malformed': True},
                                {'allow_empty': True, 'input_format': 'es'}):
                    expected = outcome(original, [line], **options)
                    for name, parser in MODES[1:]:
                        if outcome(parser, [line], **options) != expected:
                            raise AssertionError((name, line, options, expected, outcome(parser, [line], **options)))
                    checked += 1
    return checked


def corpus(kind, count):
    lines = []
    exceptions = ['ES EXC [1] Reset', 'IS (1) 1000 2000 T16 MOVS r0,#0',
                  'ES (1000:2000) T MOVS r0,#0 CCFAIL', 'ES (1000:....) T MOVS r0,#0',
                  'IT malformed', 'IF (1) 1000 2000 T16 MOVS r0,#0']
    for i in range(count):
        pc = 0x1000 + 2 * (i % (16384 if kind == 'es_large_working_set' else 256))
        if kind == 'mixed_exclusions' and i % 5 == 0:
            record = '{} ps {}'.format(i, exceptions[(i // 5) % len(exceptions)])
        elif kind.startswith('es'):
            record = '{} tic ES ({:08x}:2000) T thrd: MOVS r0,#0'.format(i, pc)
        else:
            metadata = i if kind == 'it_changing_metadata' else 0
            record = '{} ps IT ({:08x}:{:08x}) {:08x} 2000 T16 MOVS r0,#0'.format(i, pc, metadata, pc)
        lines.append(record + '\n')
        lines.append('                              R R0 00000000\n')
        lines.append('{} ps BNR4___I 00000000 00800320\n'.format(i))
    return lines


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--records', type=int, default=100000)
    cli.add_argument('--rounds', type=int, default=3)
    cli.add_argument('--output', type=Path)
    args = cli.parse_args()
    if min(args.records, args.rounds) < 1:
        cli.error('counts must be positive')
    checked = validate()
    report = {'python': sys.version, 'records_per_case': args.records, 'rounds': args.rounds,
              'differential_cases': checked, 'scope': 'in-memory parsing; no ELF, disk or multiprocessing',
              'cases': []}
    for kind in ('es_repeated', 'it_repeated', 'it_changing_metadata',
                 'es_large_working_set', 'mixed_exclusions'):
        lines = corpus(kind, args.records)
        expected = outcome(MODES[0][1], lines, skip_malformed=True)
        samples = {name: [] for name, _ in MODES}
        for name, parser in MODES:
            if outcome(parser, lines, skip_malformed=True) != expected:
                raise AssertionError('corpus mismatch: ' + kind + '/' + name)
        for round_number in range(args.rounds):
            # Rotate order to avoid always favoring the same warm-cache position.
            modes = MODES[round_number % len(MODES):] + MODES[:round_number % len(MODES)]
            for name, parser in modes:
                started = time.perf_counter()
                counts, stats = parser(lines, skip_malformed=True)
                elapsed = time.perf_counter() - started
                if (dict(counts), vars(stats)) != expected:
                    raise AssertionError('timed result differs')
                samples[name].append(elapsed)
        row = {'case': kind, 'lines': len(lines), 'samples_seconds': samples,
               'median_seconds': {name: statistics.median(values) for name, values in samples.items()}}
        report['cases'].append(row)
        print(json.dumps(row), flush=True)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
