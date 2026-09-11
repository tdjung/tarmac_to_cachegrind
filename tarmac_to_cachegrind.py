#!/usr/bin/env python3
"""Convert a Tarmac log or a folder of scenarios to flat Cachegrind profiles."""

import argparse
from collections import Counter, namedtuple
from contextlib import contextmanager
import csv
import gzip
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
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
    for line_number, line in enumerate(lines, 1):
        stats.lines += 1
        event_match = EVENT.match(line)
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
        if event == "ES" and re.match(r"EXC\b", body):
            stats.exceptions += 1
            continue
        if event == "IS":
            stats.conditional_skipped += 1
            continue
        match = ES.match(body) if event == "ES" else (IT.match(body) or IT_PAREN_PC.match(body))
        if match is None:
            stats.malformed += 1
            if not skip_malformed:
                raise ConversionError(
                    f"line {line_number}: malformed/unsupported {event} instruction; "
                    "check the trace dialect, or use --skip-malformed to omit it"
                )
            continue
        if event == "ES" and re.search(r"\bCCFAIL\b", match.group("tail")):
            stats.conditional_skipped += 1
            continue
        opcode = match.group("opcode")
        if opcode[0] in "-.":
            stats.fetch_failed += 1
            continue
        pc = int(match.group("pc"), 16)
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


def write_cachegrind(out: TextIO, costs: CounterType[Position], elf: Path, description=None) -> None:
    """Write the requested instruction-address and source-line profile format."""
    out.write("# cachegrind format\n")
    if description:
        out.write("desc: " + one_line(description) + "\n")
    out.write("desc: Tarmac flat instruction profile; no cache simulation\n")
    out.write(f"cmd: {one_line(str(elf))}\npositions: instr line\nevents: Ir\n")
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

    def costs(self, counts, stats):
        self.resolve(counts)
        stats.elf_instruction_pcs = self.elf_pc_count
        costs = self.baseline.copy()
        # Counter.update preserves zero entries, unlike Counter addition.
        costs.update(aggregate(counts, self.sources, stats))
        return costs


def discover(root, patterns, output_dir):
    """Filename filter; do not follow directory/file symlinks or scan outputs."""
    found = []
    for directory, dirs, names in os.walk(str(root), followlinks=False):
        dirs[:] = sorted(name for name in dirs
                         if not (Path(directory) / name).is_symlink()
                         and (Path(directory) / name).resolve() != output_dir)
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink() and any(fnmatch.fnmatchcase(name, p) for p in patterns):
                found.append(path)
    return sorted(found)


def output_name(path, root):
    relative = path.relative_to(root)
    name = relative.name
    if name.endswith(".gz"):
        name = name[:-3]
    if name.endswith(".log"):
        name = name[:-4]
    folders = relative.parts[:-1] or (root.name,)
    return "_".join(folders + (name, "cachegrind.out"))


def run_batch(args):
    root = Path(args.trace).resolve()
    output = (args.output_dir or args.output or root / "cachegrind-output").resolve()
    if not root.is_dir():
        raise ConversionError("root must be a directory")
    if output == root:
        raise ConversionError("output directory must differ from the input root")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ConversionError("output directory must be empty; choose a new --output-dir for each run")
    with args.elf.open("rb") as file:
        if file.read(4) != b"\x7fELF":
            raise ConversionError("--elf must point to an ELF file")
    paths = discover(root, args.pattern or ["tarmac*.log", "tarmac*.log.gz"], output)
    if not paths:
        raise ConversionError("no matching logs found")
    names = [output_name(path, root) for path in paths]
    if len(set(names)) != len(names):
        raise ConversionError("output filename collision; narrow --pattern or rename colliding input folders/logs")
    print("Found {} logs; analyzing ELF...".format(len(paths)), file=sys.stderr, flush=True)
    context = ProfileContext(args)
    output.mkdir(parents=True, exist_ok=True)
    total = context.baseline.copy()
    report = {"elf": str(args.elf.resolve()), "root": str(root), "matched": len(paths),
              "successful": [], "failed": [], "total_instructions": 0,
              "merged_output": None}
    for index, (path, name) in enumerate(zip(paths, names), 1):
        try:
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(str(path), "rt", encoding="utf-8-sig", errors="strict") as trace:
                counts, stats = count_pcs(trace, input_format=args.input_format,
                                                   allow_empty=not args.executed_only)
            if not stats.events:
                raise ConversionError("no Tarmac instruction/exception records; empty or unrelated log")
            costs = context.costs(counts, stats)
            if not args.merge_only:
                with atomic_text(output / name) as file:
                    write_cachegrind(file, costs, args.elf)
            total.update(costs)
            report["total_instructions"] += stats.instructions
            report["successful"].append({"input": str(path.relative_to(root)),
                                         "output": None if args.merge_only else name,
                                         "stats": vars(stats)})
            action = "processed" if args.merge_only else "complete"
            label = str(path.relative_to(root)) if args.merge_only else name
            print("[{}/{}] {} {}".format(index, len(paths), action, json.dumps(label, ensure_ascii=False)), file=sys.stderr, flush=True)
        except (ConversionError, OSError, UnicodeError, EOFError) as exc:
            report["failed"].append({"input": str(path.relative_to(root)), "error": str(exc)})
            print("[{}/{}] FAILED {}: {}".format(index, len(paths), path.relative_to(root), exc), file=sys.stderr, flush=True)
    if report["successful"]:
        name = "total_merge_cachegrind.out"
        with atomic_text(output / name) as file:
            description = None
            if report["failed"]:
                description = "PARTIAL MERGE: {} of {} logs failed; see batch_report.json".format(len(report["failed"]), len(paths))
            write_cachegrind(file, total, args.elf, description=description)
        report["merged_output"] = name
        print("complete " + json.dumps(name), file=sys.stderr, flush=True)
    with atomic_text(output / "batch_report.json") as file:
        json.dump(report, file, indent=2, sort_keys=True)
        file.write("\n")
    print('complete "batch_report.json"', file=sys.stderr, flush=True)
    print("Completed: {} succeeded, {} failed; {:,} instructions; {}".format(
        len(report["successful"]), len(report["failed"]), report["total_instructions"], output), file=sys.stderr, flush=True)
    return 1 if report["failed"] else 0



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", help="Tarmac log, - for stdin, or parent folder for recursive batch conversion")
    parser.add_argument("--elf", required=True, type=Path, help="matching ELF, preferably with DWARF (-g)")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("-o", "--output", type=Path, help="output file, or empty output directory in batch mode")
    output.add_argument("--output-dir", type=Path, help="batch output directory (default: ROOT/cachegrind-output)")
    parser.add_argument("--pattern", action="append", help="batch basename glob, repeatable; default: tarmac*.log and tarmac*.log.gz")
    parser.add_argument("--merge-only", action="store_true", help="batch: write only merged profile and report")
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
        if Path(args.trace).is_dir() or args.output_dir is not None:
            if args.pc_counts or args.stats or args.skip_malformed:
                raise ConversionError("--pc-counts, --stats and --skip-malformed are single-log options; batch writes batch_report.json")
            return run_batch(args)
        if args.pattern or args.merge_only:
            raise ConversionError("--pattern and --merge-only require a folder input")
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
