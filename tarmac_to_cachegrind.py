#!/usr/bin/env python3
"""Convert a Tarmac log or a folder of scenarios to flat Cachegrind profiles."""

import argparse
from collections import Counter, namedtuple
from contextlib import contextmanager
import csv
import gzip
import fnmatch
from functools import lru_cache
import json
import multiprocessing
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Counter as CounterType, Dict, Iterable, List, Optional, TextIO, Tuple


HEX = r"(?:0[xX])?[0-9a-fA-F]+"
ENCODING = r"(?:0[xX])?(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{4})|[-.]{4,8}"
STATE = r"(?:T16|T32|T|A|O)"
EVENT = re.compile(
    r"^\s*(?:\d+\s*(?:(?:tic|ps|ns|clk|cs|cyc)\s+|\s+))?"
    r"(?P<event>ES|IT|IF|IS)\b\s*(?P<body>.*)$"
)
ES = re.compile(
    rf"^\(\s*(?P<pc>{HEX})\s*:\s*(?P<opcode>{ENCODING})\s*\)"
    rf"\s+{STATE}\b(?P<tail>.*)$"
)
# Prefer the explicit address AFTER the optional metadata tuple. In particular,
# IT (address:index) address opcode T16 ... must not use index as the opcode.
IT = re.compile(
    rf"^(?:\([^)]*\)\s+)?(?P<pc>{HEX})\s+"
    rf"(?P<opcode>{ENCODING})\s+{STATE}\b(?P<tail>.*)$"
)
IT_PAREN_PC = re.compile(
    rf"^\(\s*(?P<pc>{HEX})(?:\s*:\s*{HEX})?\s*\)\s+"
    rf"(?P<opcode>{ENCODING})\s+{STATE}\b(?P<tail>.*)$"
)
LOCATION = re.compile(r"^(.*):(\d+|\?)(?:\s+\(discriminator \d+\))?$")
EXCEPTION = re.compile(r"EXC\b")
CCFAIL = re.compile(r"\bCCFAIL\b")


def decode_instruction(event, body):
    """Return (PC, disposition); caching includes the entire body, including flags."""
    if event == "ES" and EXCEPTION.match(body):
        return None, "exceptions"
    if event == "IS":
        return None, "conditional_skipped"
    match = ES.match(body) if event == "ES" else (IT.match(body) or IT_PAREN_PC.match(body))
    if match is None:
        return None, "malformed"
    if event == "ES" and CCFAIL.search(match.group("tail")):
        return None, "conditional_skipped"
    if match.group("opcode")[0] in "-.":
        return None, "fetch_failed"
    return int(match.group("pc"), 16), None


class ConversionError(Exception):
    """An actionable input, parsing, or symbolization error."""


class Stats:
    def __init__(self):
        self.lines = 0
        self.instructions = 0
        self.conditional_skipped = 0
        self.fetch_failed = 0
        self.exceptions = 0
        self.ignored = 0
        self.malformed = 0
        self.unique_pcs = 0
        self.unknown_function_instructions = 0
        self.unknown_source_instructions = 0
        self.events = {}  # type: Dict[str, int]
        self.elf_instruction_pcs = 0


class Source(namedtuple("SourceBase", "file function line")):
    """Immutable, hashable source location, without a dataclasses dependency."""

    __slots__ = ()

    def __new__(cls, file="???", function="???", line=0):
        return super().__new__(cls, file, function, line)


def count_pcs(
    lines: Iterable[str], *, input_format: str = "auto", skip_malformed: bool = False,
    allow_empty: bool = False
) -> Tuple[CounterType[int], Stats]:
    """Stream the trace; retain only a histogram of unique instruction PCs.

    No timestamp-based deduplication: folded and same-cycle instructions count
    independently. ES without CCFAIL is assumed executed (see README limits).
    """
    counts: CounterType[int] = Counter()
    stats = Stats()
    # Bounded and local to this trace: no timestamp keys, unbounded trace storage,
    # or cross-thread mutation. Cache hits still count every occurrence.
    decode = lru_cache(maxsize=8192)(decode_instruction)
    event_matcher = EVENT.match
    for line_number, line in enumerate(lines, 1):
        stats.lines += 1
        # Most register/memory records cannot possibly match EVENT. This filter
        # only rejects lines with none of its four literal event tokens.
        if "ES" not in line and "IT" not in line and "IF" not in line and "IS" not in line:
            stats.ignored += 1
            continue
        event_match = event_matcher(line)
        if not event_match:
            stats.ignored += 1
            continue
        event, body = event_match.group("event", "body")
        stats.events[event] = stats.events.get(event, 0) + 1
        family = "es" if event == "ES" else "it"
        if input_format != "auto" and input_format != family:
            raise ConversionError(
                f"line {line_number}: found {event} in --format {input_format}; "
                "use --format auto or the correct input file"
            )
        pc, disposition = decode(event, body)
        if disposition:
            setattr(stats, disposition, getattr(stats, disposition) + 1)
        if disposition == "malformed":
            if not skip_malformed:
                raise ConversionError(
                    f"line {line_number}: malformed/unsupported {event} instruction; "
                    "check the trace dialect, or use --skip-malformed to omit it"
                )
            continue
        if disposition:
            continue
        counts[pc] += 1
        stats.instructions += 1
    stats.unique_pcs = len(counts)
    if not counts and not allow_empty:
        raise ConversionError(
            "no executed instructions found; expected ES (pc:opcode) state ... "
            "or IT [metadata] pc opcode state ..."
        )
    return counts, stats


def find_addr2line(explicit: Optional[str]) -> Optional[str]:
    candidates = [explicit] if explicit else [
        "arm-none-eabi-addr2line", "llvm-addr2line", "addr2line"
    ]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    if not explicit:
        return None
    raise ConversionError(
        "addr2line not found; install Arm GNU binutils or LLVM, "
        "then pass --addr2line /path/to/arm-none-eabi-addr2line"
    )


def resolve_pcs(
    pcs: Iterable[int], elf: Path, tool: str, *, load_offset: int = 0,
    batch_size: int = 4096, timeout: float = 120,
) -> Dict[int, Source]:
    """Resolve unique PCs in bounded stdin batches, never one process per hit.

    No -i: attribute each PC once using addr2line's single-frame result. This
    neither reconstructs dynamic call stacks nor multiplies counts for inlining.
    """
    if batch_size < 1:
        raise ConversionError("batch size must be positive")
    addresses = sorted(set(pcs))
    sources: Dict[int, Source] = {}
    env = dict(os.environ, LC_ALL="C")
    for start in range(0, len(addresses), batch_size):
        batch = addresses[start:start + batch_size]
        queries = [pc - load_offset for pc in batch]
        if any(pc < 0 for pc in queries):
            raise ConversionError("--load-offset produces a negative ELF address")
        try:
            result = subprocess.run(
                [tool, "-e", str(elf.resolve()), "-f", "-C"],
                input="".join(f"0x{pc:x}\n" for pc in queries),
                universal_newlines=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConversionError(f"cannot run addr2line: {exc}") from exc
        if result.returncode:
            raise ConversionError(f"addr2line failed: {result.stderr.strip()}")
        output = result.stdout.splitlines()
        if len(output) != 2 * len(batch):
            raise ConversionError(
                "unexpected addr2line output: expected function/location pairs; "
                "use a GNU-compatible addr2line"
            )
        for index, pc in enumerate(batch):
            function = output[2 * index].strip()
            location = output[2 * index + 1].strip()
            match = LOCATION.fullmatch(location)
            if not match:
                raise ConversionError(f"unexpected addr2line source location: {location!r}")
            filename, line_number = match.groups()
            sources[pc] = Source(
                file=filename if filename not in ("", "??", "???") else "???",
                function=function if function not in ("", "??", "???") else "???",
                line=int(line_number) if line_number != "?" else 0,
            )
    return sources


def find_objdump(explicit: Optional[str], addr2line: Optional[str]) -> str:
    # Prefer the same toolchain as the selected symbolizer.
    sibling = re.sub(r"addr2line(?=(?:\.exe)?$)", "objdump", addr2line) if addr2line else None
    candidates = [explicit] if explicit else [
        sibling, "arm-none-eabi-objdump", "llvm-objdump", "objdump"
    ]
    for candidate in candidates:
        if not candidate or candidate == addr2line:
            continue
        found = shutil.which(candidate)
        if found:
            return found
    raise ConversionError("objdump not found; pass --objdump /path/to/arm-none-eabi-objdump")


def instruction_pcs(disassembly: str):
    """Read instruction starts, excluding ARM mapping-symbol data directives."""
    pcs = set()
    for line in disassembly.splitlines():
        match = re.match(r"^\s*([0-9a-fA-F]+):\s+(\S+)", line)
        if not match:
            continue
        mnemonic = match.group(2)
        if mnemonic.startswith(".") or mnemonic in ("(bad)", "<unknown>"):
            continue
        pcs.add(int(match.group(1), 16))
    return pcs


def disassemble(elf: Path, tool: str, with_lines: bool = False) -> str:
    command = [tool, "-d", "-z", "--no-show-raw-insn"]
    if with_lines:
        command.extend(["-l", "-C"])
    command.append(str(elf.resolve()))
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, encoding="utf-8", errors="replace",
            env=dict(os.environ, LC_ALL="C"), timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConversionError(f"cannot run objdump: {exc}") from exc
    if result.returncode:
        raise ConversionError(f"objdump failed (check the target toolchain): {result.stderr.strip()}")
    return result.stdout


def objdump_sources(disassembly: str) -> Dict[int, Source]:
    """Parse objdump -d -l -C; source annotations apply until the next one.

    Function/section boundaries clear the source context. Only exact instruction
    starts are indexed: an unmatched trace PC must not inherit a nearby line.
    """
    sources = {}  # type: Dict[int, Source]
    function, filename, line_number = "???", "???", 0
    for raw in disassembly.splitlines():
        line = raw.strip()
        symbol = re.match(r"^[0-9a-fA-F]+ <(.+)>:$", line)
        if line.startswith("Disassembly of section ") or symbol:
            function = re.sub(r"\+0x[0-9a-fA-F]+$", "", symbol.group(1)) if symbol else "???"
            filename, line_number = "???", 0
            continue
        location = LOCATION.fullmatch(line.lstrip("; "))
        if location:
            filename, number = location.groups()
            filename = filename if filename not in ("", "??", "???") else "???"
            line_number = int(number) if number != "?" else 0
            continue
        pcs = instruction_pcs(raw)
        for pc in pcs:
            sources[pc] = Source(filename, function, line_number)
    return sources


def seed_pcs(counts: CounterType[int], pcs: Iterable[int], load_offset: int) -> int:
    pcs = set(pcs)
    if not pcs:
        raise ConversionError("objdump found no executable instructions in the ELF")
    for pc in pcs:
        runtime_pc = pc + load_offset
        if runtime_pc < 0:
            raise ConversionError("--load-offset produces a negative runtime address")
        counts.setdefault(runtime_pc, 0)
    return len(pcs)


def seed_elf_pcs(counts: CounterType[int], elf: Path, tool: str,
                 load_offset: int = 0) -> int:
    """Seed executable instruction starts with zero; keep observed counts intact."""
    return seed_pcs(counts, instruction_pcs(disassemble(elf, tool)), load_offset)


Position = namedtuple("Position", "pc file function line")


def aggregate(counts: CounterType[int], sources: Dict[int, Source], stats: Stats) -> CounterType[Position]:
    costs: CounterType[Position] = Counter()
    for pc, count in counts.items():
        source = sources[pc]
        costs[Position(pc, *source)] += count
        if source.function == "???":
            stats.unknown_function_instructions += count
        if source.file == "???" or source.line == 0:
            stats.unknown_source_instructions += count
    return costs


def one_line(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ")


def write_cachegrind(out: TextIO, costs: CounterType[Position], elf: Path) -> None:
    """Write the requested instruction-address and source-line profile format."""
    out.write("# callgrind format\npositions: instr line\nevents: Ir\n")
    out.write("ob: " + json.dumps(str(elf), ensure_ascii=False) + "\n")
    previous = None
    for source in sorted(costs, key=lambda s: (s.file, s.function, s.pc, s.line)):
        key = (source.file, source.function)
        if key != previous:
            # Every fl is immediately followed by fn, as Cachegrind requires.
            out.write(f"fl={one_line(source.file)}\nfn={one_line(source.function)}\n")
            previous = key
        out.write(f"0x{source.pc:x} {source.line} {costs[source]}\n")
    out.write(f"summary: {sum(costs.values())}\n")


@contextmanager
def atomic_text(path: Path):
    """Do not leave a partial output file if conversion or writing fails."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=str(path.parent),
            prefix=f".{path.name}.", delete=False,
        ) as out:
            temporary = Path(out.name)
            yield out
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer, e.g. 0 or 0x20000000") from exc


def nonnegative_integer(value):
    number = integer(value)
    if number < 0:
        raise argparse.ArgumentTypeError("depth must be non-negative")
    return number


def positive_integer(value):
    number = integer(value)
    if number < 1:
        raise argparse.ArgumentTypeError("workers must be positive")
    return number


def log_basename(value):
    if not value or value in (".", "..") or "/" in value or "\\" in value:
        raise argparse.ArgumentTypeError("--log-name requires a filename, not a path")
    return value


class ProfileContext:
    """Keep ELF instruction/source analysis and unknown-PC results across logs."""

    def __init__(self, args):
        self.elf = args.elf
        self.offset = args.load_offset
        self.addr2line = find_addr2line(args.addr2line)
        self.sources = {}
        self.baseline = Counter()
        self.elf_pc_count = 0
        self.fallback = None
        if not args.executed_only or self.addr2line is None:
            objdump = find_objdump(args.objdump, self.addr2line)
            dump = disassemble(self.elf, objdump, with_lines=self.addr2line is None)
            if self.addr2line is None:
                print("info: addr2line not found; using objdump -l", file=sys.stderr)
                self.fallback = objdump_sources(dump)
                pcs = set(self.fallback)
            else:
                pcs = instruction_pcs(dump)
            if not pcs:
                raise ConversionError("objdump found no executable instructions in the ELF")
            if not args.executed_only:
                runtime = Counter()
                self.elf_pc_count = seed_pcs(runtime, pcs, self.offset)
                self.resolve(runtime)
                for pc, source in self.sources.items():
                    self.baseline[Position(pc, *source)] = 0
        self.layout = self.make_layout(self.baseline)
        self.baseline_pcs = {position.pc for position in self.baseline}

    @staticmethod
    def make_layout(positions):
        rows = []
        previous = None
        for source in sorted(positions, key=lambda s: (s.file, s.function, s.pc, s.line)):
            key = (source.file, source.function)
            header = ""
            if key != previous:
                header = "fl={}\nfn={}\n".format(one_line(source.file), one_line(source.function))
                previous = key
            prefix = header + "0x{:x} {} ".format(source.pc, source.line)
            rows.append((source.pc, prefix, prefix + "0\n"))
        return rows

    def prepare(self, counts, stats):
        self.resolve(counts)
        stats.elf_instruction_pcs = self.elf_pc_count
        for pc, count in counts.items():
            source = self.sources[pc]
            if source.function == "???":
                stats.unknown_function_instructions += count
            if source.file == "???" or source.line == 0:
                stats.unknown_source_instructions += count

    def output_layout(self, counts):
        # Reuse sorted, preformatted zero-coverage rows. Only an observed PC
        # outside the baseline requires a per-output extended layout.
        extras = [Position(pc, *self.sources[pc]) for pc in counts
                  if pc not in self.baseline_pcs]
        return self.make_layout(list(self.baseline) + extras) if extras else self.layout

    def write(self, out, counts):
        layout = self.output_layout(counts)
        out.write("# callgrind format\npositions: instr line\nevents: Ir\n")
        out.write("ob: " + json.dumps(str(self.elf), ensure_ascii=False) + "\n")
        get = counts.get
        out.writelines(prefix + str(get(pc)) + "\n" if get(pc, 0) else zero
                       for pc, prefix, zero in layout)
        out.write("summary: {}\n".format(sum(counts.values())))

    def resolve(self, pcs):
        missing = set(pcs).difference(self.sources)
        if any(pc - self.offset < 0 for pc in missing):
            raise ConversionError("--load-offset produces a negative ELF address")
        if self.addr2line is not None:
            self.sources.update(resolve_pcs(
                missing, self.elf, self.addr2line, load_offset=self.offset))
        else:
            self.sources.update({pc: self.fallback.get(pc - self.offset, Source())
                                 for pc in missing})


def discover(root, patterns, output_dir, max_depth=1, log_name=None):
    """Depth 0 scans root only; prune traversal before opening deeper folders."""
    if max_depth < 0:
        raise ConversionError("--max-depth must be non-negative")
    found = []
    pending = [(root, 0)]
    while pending:
        directory, depth = pending.pop()
        with os.scandir(str(directory)) as entries:
            for entry in entries:
                path = directory / entry.name
                if path == output_dir:
                    continue
                # Filter names before file metadata calls; never resolve each path.
                if (entry.name == log_name if log_name is not None else
                        any(fnmatch.fnmatchcase(entry.name, p) for p in patterns)):
                    if entry.is_file(follow_symlinks=False):
                        found.append(path)
                        continue
                if depth < max_depth and entry.is_dir(follow_symlinks=False):
                    pending.append((path, depth + 1))
    return sorted(found)


def output_name(path, index, root):
    name = path.name
    if name.endswith(".gz"):
        name = name[:-3]
    if name.endswith(".log"):
        name = name[:-4]
    folders = path.relative_to(root).parts[:-1] or (root.name,)
    return "{}_{}_{}_cachegrind.out".format(index, "_".join(folders), name)


COVERAGE_EVENTS = (["Tests"] + ["Covered{}".format(i) for i in range(1, 6)] +
                   ["CoveredSet"] + ["Uncovered{}".format(i) for i in range(1, 6)] +
                   ["UncoveredSet"])


def mask_indices(mask):
    while mask:
        bit = mask & -mask
        yield bit.bit_length()
        mask ^= bit


class CoverageIndex:
    """Parent-owned sparse PC membership; expand each distinct set only once."""

    def __init__(self, test_ids=None):
        self.pc_masks = {}
        self.success_mask = 0
        self.set_ids = {}
        self.sets = {}
        self.rows = {}
        # Re-merge may retain sparse/large filename IDs. Masks always use
        # dense internal positions; display IDs are sorted by the caller.
        self.test_ids = test_ids

    def display_id(self, index):
        return self.test_ids[index - 1] if self.test_ids is not None else index

    def add(self, index, counts):
        bit = 1 << (index - 1)
        self.success_mask |= bit
        masks = self.pc_masks
        for pc, count in counts.items():
            if count:
                masks[pc] = masks.get(pc, 0) | bit

    def slots(self, mask):
        first = []
        for _ in range(5):
            if not mask:
                break
            bit = mask & -mask
            first.append(self.display_id(bit.bit_length()))
            mask ^= bit
        first.extend([0] * (5 - len(first)))
        set_id = 0
        if mask:
            set_id = self.set_ids.get(mask)
            if set_id is None:
                set_id = len(self.set_ids) + 1
                self.set_ids[mask] = set_id
                self.sets[str(set_id)] = [self.display_id(i) for i in mask_indices(mask)]
        return first + [set_id]

    def row(self, covered):
        if covered not in self.rows:
            # int.bit_count is unavailable on Python 3.6.
            values = ([bin(covered).count("1")] + self.slots(covered) +
                      self.slots(self.success_mask ^ covered)) if covered else [0] * len(COVERAGE_EVENTS)
            self.rows[covered] = (values, " " + " ".join(map(str, values)) + "\n")
        return self.rows[covered]

    @staticmethod
    def line_key(pc, source):
        return (source.file, source.line) if source.file != "???" and source.line > 0 else (None, pc)

    def write(self, out, context, counts):
        layout = context.output_layout(counts)
        anchors, line_masks = {}, {}
        for pc, _, _ in layout:
            if counts.get(pc, 0) > 0:
                key = self.line_key(pc, context.sources[pc])
                anchors.setdefault(key, pc)
                line_masks[key] = line_masks.get(key, 0) | self.pc_masks.get(pc, 0)
        out.write("# callgrind format\npositions: instr line\nevents: Ir " +
                  " ".join(COVERAGE_EVENTS) + "\n")
        out.write("ob: " + json.dumps(str(context.elf), ensure_ascii=False) + "\n")
        summary = [sum(counts.values())] + [0] * len(COVERAGE_EVENTS)
        zero = " 0" * len(COVERAGE_EVENTS) + "\n"
        for pc, prefix, _ in layout:
            count = counts.get(pc, 0)
            suffix = zero
            key = self.line_key(pc, context.sources[pc])
            if count > 0 and pc == anchors[key]:
                values, suffix = self.row(line_masks[key])
                for i, value in enumerate(values, 1):
                    summary[i] += value
            out.write(prefix + str(count) + suffix)
        out.write("summary: " + " ".join(map(str, summary)) + "\n")


_WORKER_CONTEXT = None
_WORKER_ARGS = None


def init_worker(context, args):
    global _WORKER_CONTEXT, _WORKER_ARGS
    _WORKER_CONTEXT, _WORKER_ARGS = context, args


def convert_batch_file(task, context=None, args=None):
    """Workers own their counters, symbol caches and distinct atomic outputs."""
    context = context if context is not None else _WORKER_CONTEXT
    args = args if args is not None else _WORKER_ARGS
    path, destination = task
    started = time.monotonic()
    try:
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(str(path), "rt", encoding="utf-8-sig", errors="strict") as trace:
            counts, stats = count_pcs(trace, input_format=args.input_format,
                                      allow_empty=not args.executed_only)
        parsed = time.monotonic()
        if not stats.events:
            raise ConversionError("no Tarmac instruction/exception records; empty or unrelated log")
        context.prepare(counts, stats)
        mapped = time.monotonic()
        if not args.merge_only:
            with atomic_text(destination) as file:
                context.write(file, counts)
        finished = time.monotonic()
        # Transfer only executed PCs, never the ELF-wide zero baseline or log.
        return (path, destination.name, counts, vars(stats),
                {"parse": parsed - started, "map": mapped - parsed,
                 "write": finished - mapped, "total": finished - started}, None)
    except (ConversionError, OSError, UnicodeError, EOFError) as exc:
        return path, destination.name, None, None, None, str(exc)


def progress(message, args):
    stream = sys.stdout if args.progress_stream == "stdout" else sys.stderr
    print(message, file=stream, flush=True)


def batch_results(tasks, context, args, workers):
    started = time.monotonic()
    progress("[0/{}] starting with {} worker process(es)".format(len(tasks), workers), args)
    if workers == 1:
        for task in tasks:
            yield convert_batch_file(task, context, args)
        return
    # Pool.initializer works on Python 3.6 (Executor.initializer does not).
    # Linux fork shares the precomputed ELF/layout through copy-on-write;
    # spawn also works by serializing context once per worker, not per log.
    with multiprocessing.Pool(workers, initializer=init_worker,
                              initargs=(context, args)) as pool:
        # Bound queued tasks AND completed sparse histograms to 2*workers.
        # No per-line shared counter, lock, Manager, or worker-side merge.
        ready = queue.Queue()
        pending = iter(tasks)

        def submit(task):
            pool.apply_async(convert_batch_file, (task,),
                             callback=lambda result: ready.put((True, result)),
                             error_callback=lambda error: ready.put((False, error)))

        for _ in range(min(2 * workers, len(tasks))):
            submit(next(pending))
        for completed in range(len(tasks)):
            while True:
                try:
                    ok, result = ready.get(timeout=5.0)
                    break
                except queue.Empty:
                    progress("[{}/{}] waiting for workers; {:.1f}s elapsed".format(
                        completed, len(tasks), time.monotonic() - started), args)
            if not ok:
                raise ConversionError("worker failed: {}".format(result))
            yield result
            task = next(pending, None)
            if task is not None:
                submit(task)


def run_batch(args):
    batch_started = time.monotonic()
    root = Path(args.trace).resolve()
    output = (args.output_dir or args.output or root / "cachegrind-output").resolve()
    if not root.is_dir():
        raise ConversionError("root must be a directory")
    if output == root:
        raise ConversionError("output directory must differ from the input root")
    if output.exists() and not output.is_dir():
        raise ConversionError("output path must be a directory")
    with args.elf.open("rb") as file:
        if file.read(4) != b"\x7fELF":
            raise ConversionError("--elf must point to an ELF file")
    progress("Searching {} (max-depth={})...".format(root, args.max_depth), args)
    started = time.monotonic()
    paths = discover(root, args.pattern or ["tarmac*.log", "tarmac*.log.gz"], output, args.max_depth, args.log_name)
    progress("Search complete: {} logs in {:.2f}s".format(len(paths), time.monotonic() - started), args)
    if not paths:
        raise ConversionError("no matching logs found")
    indices = {path: i for i, path in enumerate(paths, 1)}
    names = [output_name(path, indices[path], root) for path in paths]
    progress("Found {} logs; analyzing ELF...".format(len(paths)), args)
    started = time.monotonic()
    context = ProfileContext(args)
    progress("ELF analysis complete in {:.2f}s".format(time.monotonic() - started), args)
    output.mkdir(parents=True, exist_ok=True)
    total = Counter()
    coverage = CoverageIndex()
    workers = min(args.workers, len(paths))
    progress("Converting with {} worker process(es)...".format(workers), args)
    tag = ""
    if args.log_name:
        tag = args.log_name
        if tag.endswith(".gz"):
            tag = tag[:-3]
        if tag.endswith(".log"):
            tag = tag[:-4]
        tag = "_" + tag
    merge_name = "total_merge" + tag + "_cachegrind.out"
    report_name = "batch_report" + tag + ".json"
    index_name = "coverage_index" + tag + ".json"
    manifest = [{"index": indices[path], "input": str(path.relative_to(root)),
                 "output": None, "planned_output": name, "status": "pending"}
                for path, name in zip(paths, names)]
    report = {"elf": str(args.elf.resolve()), "root": str(root), "matched": len(paths),
              "successful": [], "failed": [], "total_instructions": 0,
              "merged_output": None, "max_depth": args.max_depth, "partial_merge": False,
              "log_name": args.log_name, "preserved_previous_outputs": [],
              "workers": workers, "merge_seconds": 0.0, "coverage_seconds": 0.0,
              "coverage_index": None}
    tasks = [(path, output / name) for path, name in zip(paths, names)]
    for index, result in enumerate(batch_results(tasks, context, args, workers), 1):
        path, name, counts, stats, timings, error = result
        test_index = indices[path]
        entry = manifest[test_index - 1]
        if error is None:
            merge_started = time.monotonic()
            total.update(counts)
            coverage_started = time.monotonic()
            coverage.add(test_index, counts)
            report["coverage_seconds"] += time.monotonic() - coverage_started
            report["merge_seconds"] += time.monotonic() - merge_started
            report["total_instructions"] += stats["instructions"]
            entry.update(status="successful", output=None if args.merge_only else name)
            report["successful"].append({"input": str(path.relative_to(root)), "index": test_index,
                                         "output": None if args.merge_only else name,
                                         "stats": stats, "seconds": timings})
            action = "processed" if args.merge_only else "complete"
            label = str(path.relative_to(root)) if args.merge_only else name
            progress("[{}/{}] {} {}".format(index, len(paths), action, json.dumps(label, ensure_ascii=False)), args)
        else:
            entry.update(status="failed", error=error)
            report["failed"].append({"input": str(path.relative_to(root)), "index": test_index, "error": error})
            if not args.merge_only and (output / name).exists():
                report["preserved_previous_outputs"].append(name)
                print("warning: previous output preserved, excluded from this merge: " + name, file=sys.stderr, flush=True)
            progress("[{}/{}] FAILED {}: {}".format(index, len(paths), path.relative_to(root), error), args)
    if report["successful"]:
        progress("[{}/{}] generating merged profile and coverage index...".format(len(paths), len(paths)), args)
        name = merge_name
        merge_started = time.monotonic()
        context.resolve(total)
        coverage_started = time.monotonic()
        with atomic_text(output / name) as file:
            coverage.write(file, context, total)
            with atomic_text(output / index_name) as index_file:
                json.dump({"version": 3, "elf": str(args.elf.resolve()), "root": str(root),
                           "merged_output": merge_name, "tests": manifest,
                           "successful_tests": len(report["successful"]),
                           "failed_tests": len(report["failed"]),
                           "set_semantics": "remaining indices after the first five; 0 means empty",
                           "coverage_unit": "source_line",
                           "line_semantics": "union by file and line; stored on one executed PC; unknown locations kept per PC",
                           "assembly_guidance": "only Ir is an instruction-level metric; other events are source-line metadata",
                           "zero_ir_semantics": "all coverage events are zero when total PC Ir is zero",
                           "sets": coverage.sets}, index_file, indent=2, sort_keys=True)
                index_file.write("\n")
        report["coverage_seconds"] += time.monotonic() - coverage_started
        report["coverage_index"] = index_name
        progress("complete " + json.dumps(index_name), args)
        report["merge_seconds"] += time.monotonic() - merge_started
        if report["failed"]:
            report["partial_merge"] = True
            print("warning: PARTIAL MERGE: {} logs failed; see {}".format(len(report["failed"]), report_name), file=sys.stderr, flush=True)
        report["merged_output"] = name
        progress("complete " + json.dumps(name), args)
    if not report["successful"] and (output / merge_name).exists():
        report["preserved_previous_outputs"].append(merge_name)
        print("warning: all logs failed; previous merge was not updated: " + merge_name, file=sys.stderr, flush=True)
        if (output / index_name).exists():
            report["preserved_previous_outputs"].append(index_name)
    report["successful"].sort(key=lambda item: item["input"])
    report["failed"].sort(key=lambda item: item["input"])
    report["preserved_previous_outputs"].sort()
    report["elapsed_seconds"] = time.monotonic() - batch_started
    with atomic_text(output / report_name) as file:
        json.dump(report, file, indent=2, sort_keys=True)
        file.write("\n")
    progress("complete " + json.dumps(report_name), args)
    progress("Completed: {} succeeded, {} failed; {:,} instructions; {:.2f}s elapsed; {}".format(
        len(report["successful"]), len(report["failed"]), report["total_instructions"],
        time.monotonic() - batch_started, output), args)
    return 1 if report["failed"] else 0



def file_identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def list_profiles(list_path, patterns, outputs, excluding=False):
    """Paths are relative to the list file; directory entries are not recursive."""
    found = {}
    with list_path.open(encoding="utf-8-sig") as file:
        for number, raw in enumerate(file, 1):
            entry = raw.strip()
            if not entry or entry.startswith("#"):
                continue
            path = Path(entry).expanduser()
            if not path.is_absolute():
                path = list_path.parent / path
            path = path.resolve()
            if path.is_dir():
                if not patterns:
                    raise ConversionError("{}:{}: folder entries require --merge-pattern (or --merge-name)".format(list_path, number))
                candidates = []
                with os.scandir(str(path)) as children:
                    for child in children:
                        if (any(fnmatch.fnmatchcase(child.name, pattern) for pattern in patterns)
                                and child.is_file(follow_symlinks=False)):
                            candidate = Path(child.path).resolve()
                            if candidate in outputs or child.name.startswith("total_merge"):
                                continue
                            candidates.append(candidate)
                if not candidates and not excluding:
                    raise ConversionError("{}:{}: no matching individual profiles in {}".format(list_path, number, path))
            elif path.is_file():
                if path in outputs:
                    raise ConversionError("merge input and output must differ: {}".format(path))
                if path.name.startswith("total_merge"):
                    raise ConversionError("select individual profiles, not total_merge files: {}".format(path))
                candidates = [path]
            else:
                raise ConversionError("{}:{}: file/folder not found: {}".format(list_path, number, path))
            for candidate in sorted(candidates):
                found.setdefault(file_identity(candidate), candidate)
    return found


def read_ir_profile(path):
    """Read this converter's absolute PC/line/Ir format, not arbitrary callgrind.

    Reject multi-event totals: their original test membership cannot be recovered
    by treating the aggregate as an individual test. Require a matching summary
    so a truncated file is never silently merged.
    """
    counts, sources, headers = Counter(), {}, {}
    filename = function = None
    summary = None
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(str(path), "rt", encoding="utf-8-sig", errors="strict") as file:
        for number, raw in enumerate(file, 1):
            line = raw.rstrip("\r\n")
            if not line.strip() or line.startswith("#"):
                continue
            try:
                if summary is not None:
                    raise ValueError("records after summary")
                if line.startswith(("positions:", "events:", "ob:")):
                    key, value = line.split(":", 1)
                    if key in headers or sources:
                        raise ValueError("duplicate or misplaced header")
                    headers[key] = value.strip()
                    if key == "events" and value.split() != ["Ir"]:
                        raise ValueError("only individual Ir-only profiles are supported; do not merge totals")
                elif line.startswith("fl="):
                    filename, function = line[3:], None
                elif line.startswith("fn="):
                    function = line[3:]
                elif line.startswith("summary:"):
                    summary = int(line.split(":", 1)[1].strip())
                elif line.startswith(("0x", "0X")):
                    if headers.get("positions") != "instr line" or headers.get("events") != "Ir":
                        raise ValueError("expected positions: instr line and events: Ir")
                    if filename is None or function is None:
                        raise ValueError("missing fl/fn before instruction")
                    pc_text, line_text, cost_text = line.split()
                    pc, line_number, cost = int(pc_text, 16), int(line_text), int(cost_text)
                    if min(pc, line_number, cost) < 0:
                        raise ValueError("negative PC, line or Ir")
                    source = Source(filename, function, line_number)
                    if pc in sources and sources[pc] != source:
                        raise ValueError("conflicting source mapping for PC")
                    sources[pc] = source
                    if cost:
                        counts[pc] += cost
                else:
                    raise ValueError("unsupported record; use this converter's individual output")
            except ValueError as exc:
                raise ConversionError("{}:{}: {}".format(path, number, exc)) from exc
    if not sources or summary is None or summary != sum(counts.values()):
        raise ConversionError("{}: missing instructions/summary or Ir summary mismatch".format(path))
    try:
        obj = json.loads(headers["ob"])
        if not isinstance(obj, str) or not obj:
            raise ValueError("invalid object path")
    except (KeyError, ValueError) as exc:
        raise ConversionError("{}: expected ob: followed by a quoted ELF path".format(path)) from exc
    return counts, sources, obj


def run_profile_merge(args):
    if (args.trace is not None or args.elf or args.output_dir or args.output is None or
            args.pattern or args.log_name or args.merge_only or args.workers != 1 or
            args.addr2line or args.objdump or args.executed_only or args.load_offset or
            args.skip_malformed or args.pc_counts or args.stats or args.input_format != "auto" or args.max_depth != 1):
        raise ConversionError("--merge-list requires -o FILE and uses existing profiles; omit trace/ELF and conversion options")
    started = time.monotonic()
    listing = args.merge_list.resolve()
    output = args.output.resolve()
    index_path = output.with_name(output.name + ".coverage_index.json")
    report_path = output.with_name(output.name + ".merge_report.json")
    outputs = {output, index_path, report_path}
    lists = [listing] + ([args.exclude_list.resolve()] if args.exclude_list else [])
    for destination in outputs:
        if destination.is_dir() or not destination.parent.is_dir():
            raise ConversionError("invalid output path (parent must exist): {}".format(destination))
        for source in lists:
            if destination == source or (destination.exists() and source.exists() and destination.samefile(source)):
                raise ConversionError("output must not overwrite a selection list")
    progress("Reading merge selection: {}".format(listing), args)
    selected = list_profiles(listing, args.merge_pattern, outputs)
    excluded = list_profiles(lists[1], args.merge_pattern, outputs, excluding=True) if args.exclude_list else {}
    selected = {identity: path for identity, path in selected.items() if identity not in excluded}
    if not selected:
        raise ConversionError("no individual profiles remain after selection/exclusion")
    for destination in outputs:
        if destination.exists() and file_identity(destination) in selected:
            raise ConversionError("output aliases an input profile: {}".format(destination))
    paths = sorted(selected.values())
    if args.reindex:
        entries = list(enumerate(paths, 1))
    else:
        entries, used = [], set()
        for path in paths:
            match = re.match(r"^([1-9][0-9]*)_", path.name)
            if not match:
                raise ConversionError("missing test index prefix in {}; use --reindex".format(path))
            index = int(match.group(1))
            if index in used:
                raise ConversionError("duplicate test index {}; narrow the selection or use --reindex".format(index))
            used.add(index)
            entries.append((index, path))
        entries.sort()
    coverage = CoverageIndex([index for index, _ in entries])
    total, sources, obj, manifest = Counter(), {}, None, []
    progress("[0/{}] merging existing profiles".format(len(entries)), args)
    for internal_index, (test_index, path) in enumerate(entries, 1):
        counts, mapping, current_obj = read_ir_profile(path)
        if obj is None:
            obj = current_obj
        elif obj != current_obj:
            raise ConversionError("ELF object mismatch in {}; merge only profiles from the same image".format(path))
        for pc, source in mapping.items():
            if pc in sources and sources[pc] != source:
                raise ConversionError("{}: conflicting source mapping at PC 0x{:x}".format(path, pc))
        sources.update(mapping)
        total.update(counts)
        coverage.add(internal_index, counts)
        manifest.append({"index": test_index, "input": str(path), "output": str(path),
                         "status": "successful", "instructions": sum(counts.values())})
        progress("[{}/{}] merged {}".format(internal_index, len(entries), json.dumps(str(path), ensure_ascii=False)), args)
    context = ProfileContext.__new__(ProfileContext)
    context.elf, context.sources = Path(obj), sources
    context.baseline = Counter({Position(pc, *source): 0 for pc, source in sources.items()})
    context.baseline_pcs = set(sources)
    context.layout = context.make_layout(context.baseline)
    # All selected files must pass before any result is replaced.
    with atomic_text(output) as file:
        coverage.write(file, context, total)
        with atomic_text(index_path) as index_file:
            json.dump({"version": 3, "elf": obj, "input_kind": "individual_profile",
                       "merged_output": str(output), "tests": manifest,
                       "successful_tests": len(entries), "failed_tests": 0,
                       "coverage_unit": "source_line", "indices_preserved": not args.reindex,
                       "set_semantics": "remaining indices after the first five; 0 means empty",
                       "line_semantics": "union by file and line; stored on one executed PC; unknown locations kept per PC",
                       "zero_ir_semantics": "all coverage events are zero when total PC Ir is zero",
                       "assembly_guidance": "only Ir is an instruction-level metric; other events are source-line metadata",
                       "sets": coverage.sets}, index_file, indent=2, sort_keys=True)
            index_file.write("\n")
    report = {"mode": "merge_list", "selection_list": str(listing), "merged": len(entries),
              "total_instructions": sum(total.values()), "merged_output": str(output),
              "coverage_index": str(index_path), "indices_preserved": not args.reindex,
              "inputs": manifest, "elapsed_seconds": time.monotonic() - started}
    with atomic_text(report_path) as file:
        json.dump(report, file, indent=2, sort_keys=True)
        file.write("\n")
    for path in (output, index_path, report_path):
        progress("complete " + json.dumps(str(path), ensure_ascii=False), args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", nargs="?", help="Tarmac log, - for stdin, or parent folder (omit with --merge-list)")
    parser.add_argument("--elf", type=Path, help="matching ELF, required for trace conversion; not used with --merge-list")
    parser.add_argument("--merge-list", type=Path, help="merge existing individual profiles listed as files/folders, one path per line")
    parser.add_argument("--merge-pattern", "--merge-name", action="append", dest="merge_pattern",
                        type=log_basename, help="merge-list: filename or basename glob inside listed folders; repeatable, non-recursive")
    parser.add_argument("--exclude-list", type=Path, help="merge-list: exclude files/folders from this path list")
    parser.add_argument("--reindex", action="store_true", help="merge-list: assign new test IDs instead of retaining filename index prefixes")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("-o", "--output", type=Path, help="output file, or output directory in batch mode (existing files replaced)")
    output.add_argument("--output-dir", type=Path, help="batch output directory (default: ROOT/cachegrind-output)")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--log-name", type=log_basename, help="batch: exact filename for this ELF (e.g. tarmac_core0.log); tags merge/report filenames")
    selection.add_argument("--pattern", action="append", help="batch basename glob, repeatable; default: tarmac*.log and tarmac*.log.gz")
    parser.add_argument("--max-depth", type=nonnegative_integer, default=1, help="batch directory depth: 0=root only, 1=root and immediate children (default)")
    parser.add_argument("--merge-only", action="store_true", help="batch: write only merged profile and report")
    parser.add_argument("--workers", type=positive_integer, default=1,
                        help="batch: parallel worker processes (default: 1; bounded to matching log count)")
    parser.add_argument("--progress-stream", choices=("stderr", "stdout"), default="stderr",
                        help="batch: progress output stream (default: stderr; use stdout for captured job output)")
    parser.add_argument("--format", choices=("auto", "es", "it"), default="auto", dest="input_format")
    parser.add_argument("--addr2line", help="GNU-compatible addr2line (auto-detect by default; use objdump -l if none found)")
    parser.add_argument("--objdump", help="target-compatible objdump for ELF instruction enumeration")
    parser.add_argument("--executed-only", action="store_true", help="omit unexecuted ELF locations (legacy behavior; no objdump needed)")
    parser.add_argument("--load-offset", type=integer, default=0, help="runtime PC minus ELF address (decimal/hex)")
    parser.add_argument("--skip-malformed", action="store_true", help="omit unsupported instruction records and warn (default: fail)")
    parser.add_argument("--pc-counts", type=Path, help="optional CSV audit: PC, ELF address, Ir, source")
    parser.add_argument("--stats", type=Path, help="optional JSON conversion statistics")
    return parser


def validate_paths(args: argparse.Namespace) -> None:
    inputs = [args.elf]
    if args.trace != "-":
        inputs.append(Path(args.trace))
    outputs = [p for p in (args.output, args.pc_counts, args.stats) if p is not None]
    paths = inputs + outputs
    # Resolve symlinks and check hard links before replacing any output.
    for index, path in enumerate(paths):
        for other in paths[index + 1:]:
            if path.resolve() == other.resolve() or (
                path.exists() and other.exists() and path.samefile(other)
            ):
                raise ConversionError("input and output paths must all be different")
    for path in inputs:
        if not path.is_file():
            raise ConversionError(f"input file does not exist: {path}")
    with args.elf.open("rb") as elf_file:
        if elf_file.read(4) != b"\x7fELF":
            raise ConversionError(f"not an ELF file: {args.elf}")
    for path in outputs:
        if not path.parent.is_dir() or path.is_dir():
            raise ConversionError(f"invalid output path (parent must exist): {path}")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.merge_list:
            return run_profile_merge(args)
        if args.merge_pattern or args.exclude_list or args.reindex:
            raise ConversionError("--merge-pattern, --exclude-list and --reindex require --merge-list")
        if args.trace is None or args.elf is None:
            raise ConversionError("trace conversion requires a trace/folder and --elf; use --merge-list for existing profiles")
        if Path(args.trace).is_dir() or args.output_dir is not None:
            if args.pc_counts or args.stats or args.skip_malformed:
                raise ConversionError("--pc-counts, --stats and --skip-malformed are single-log options; batch writes batch_report.json")
            return run_batch(args)
        if args.pattern or args.log_name or args.merge_only or args.workers != 1 or args.progress_stream != "stderr":
            raise ConversionError("--pattern, --log-name, --merge-only, --workers and --progress-stream require a folder input")
        if args.output is None:
            raise ConversionError("single-log conversion requires -o/--output")
        validate_paths(args)
        tool = find_addr2line(args.addr2line)
        options = dict(input_format=args.input_format, skip_malformed=args.skip_malformed,
                       allow_empty=not args.executed_only)
        if args.trace == "-":
            counts, stats = count_pcs(sys.stdin, **options)
        else:
            opener = gzip.open if args.trace.lower().endswith(".gz") else open
            with opener(args.trace, "rt", encoding="utf-8-sig", errors="strict") as trace:
                counts, stats = count_pcs(trace, **options)
        if tool is None:
            print("info: addr2line not found on PATH; using objdump -l for source locations", file=sys.stderr)
            dump = disassemble(args.elf, find_objdump(args.objdump, None), with_lines=True)
            elf_sources = objdump_sources(dump)
            if not elf_sources:
                raise ConversionError("objdump found no executable instructions in the ELF")
            if not args.executed_only:
                stats.elf_instruction_pcs = seed_pcs(counts, elf_sources, args.load_offset)
            if any(pc - args.load_offset < 0 for pc in counts):
                raise ConversionError("--load-offset produces a negative ELF address")
            sources = {pc: elf_sources.get(pc - args.load_offset, Source()) for pc in counts}
        else:
            if not args.executed_only:
                stats.elf_instruction_pcs = seed_elf_pcs(
                    counts, args.elf, find_objdump(args.objdump, tool), args.load_offset
                )
            sources = resolve_pcs(counts, args.elf, tool, load_offset=args.load_offset)
        costs = aggregate(counts, sources, stats)
        if args.pc_counts:
            with atomic_text(args.pc_counts) as out:
                writer = csv.writer(out)
                writer.writerow(["pc", "elf_address", "Ir", "file", "function", "line"])
                for pc in sorted(counts):
                    source = sources[pc]
                    writer.writerow([f"0x{pc:x}", f"0x{pc - args.load_offset:x}", counts[pc],
                                     source.file, source.function, source.line])
        if args.stats:
            with atomic_text(args.stats) as out:
                json.dump(vars(stats), out, indent=2, sort_keys=True)
                out.write("\n")
        with atomic_text(args.output) as out:
            write_cachegrind(out, costs, args.elf)
        print("complete " + json.dumps(str(args.output), ensure_ascii=False), file=sys.stderr, flush=True)
        print(f"{stats.instructions:,} instructions, {stats.unique_pcs:,} unique PCs -> {args.output}", file=sys.stderr)
        if not stats.instructions:
            print("warning: no executed instructions found; output contains only zero-cost ELF locations", file=sys.stderr)
        if stats.malformed:
            print(f"warning: omitted {stats.malformed} malformed instruction records", file=sys.stderr)
        if stats.unknown_source_instructions or stats.unknown_function_instructions:
            print(
                f"warning: {stats.unknown_source_instructions:,} instructions without source lines; "
                f"{stats.unknown_function_instructions:,} without function names. "
                "Counts retained; check ELF debug info, tool architecture and --load-offset.",
                file=sys.stderr,
            )
        return 0
    except (ConversionError, OSError, UnicodeError, EOFError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
