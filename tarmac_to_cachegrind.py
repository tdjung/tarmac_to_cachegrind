#!/usr/bin/env python3
"""Convert one core's Tarmac trace to a flat, Ir-only Cachegrind profile."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from dataclasses import asdict, dataclass, field
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable, TextIO


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


@dataclass
class Stats:
    lines: int = 0
    instructions: int = 0
    conditional_skipped: int = 0
    fetch_failed: int = 0
    exceptions: int = 0
    ignored: int = 0
    malformed: int = 0
    unique_pcs: int = 0
    unknown_function_instructions: int = 0
    unknown_source_instructions: int = 0
    events: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Source:
    file: str = "???"
    function: str = "???"
    line: int = 0


def count_pcs(
    lines: Iterable[str], *, input_format: str = "auto", skip_malformed: bool = False
) -> tuple[Counter[int], Stats]:
    """Stream the trace; retain only a histogram of unique instruction PCs.

    No timestamp-based deduplication: folded and same-cycle instructions count
    independently. ES without CCFAIL is assumed executed (see README limits).
    """
    counts: Counter[int] = Counter()
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
    if not counts:
        raise ConversionError(
            "no executed instructions found; expected ES (pc:opcode) state ... "
            "or IT [metadata] pc opcode state ..."
        )
    return counts, stats


def find_addr2line(explicit: str | None) -> str:
    candidates = [explicit] if explicit else [
        "arm-none-eabi-addr2line", "llvm-addr2line", "addr2line"
    ]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    raise ConversionError(
        "addr2line not found; install Arm GNU binutils or LLVM, "
        "then pass --addr2line /path/to/arm-none-eabi-addr2line"
    )


def resolve_pcs(
    pcs: Iterable[int], elf: Path, tool: str, *, load_offset: int = 0,
    batch_size: int = 4096, timeout: float = 120,
) -> dict[int, Source]:
    """Resolve unique PCs in bounded stdin batches, never one process per hit.

    No -i: attribute each PC once using addr2line's single-frame result. This
    neither reconstructs dynamic call stacks nor multiplies counts for inlining.
    """
    if batch_size < 1:
        raise ConversionError("batch size must be positive")
    addresses = sorted(set(pcs))
    sources: dict[int, Source] = {}
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
                text=True, encoding="utf-8", errors="replace",
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


def aggregate(counts: Counter[int], sources: dict[int, Source], stats: Stats) -> Counter[Source]:
    costs: Counter[Source] = Counter()
    for pc, count in counts.items():
        source = sources[pc]
        costs[source] += count
        if source.function == "???":
            stats.unknown_function_instructions += count
        if source.file == "???" or source.line == 0:
            stats.unknown_source_instructions += count
    return costs


def one_line(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ")


def write_cachegrind(out: TextIO, costs: Counter[Source], elf: Path) -> None:
    """Write the Cachegrind subset, without Callgrind-specific headers or edges."""
    out.write("desc: Tarmac flat instruction profile; no cache simulation\n")
    out.write(f"cmd: {one_line(str(elf))}\nevents: Ir\n")
    previous = None
    for source in sorted(costs, key=lambda s: (s.file, s.function, s.line)):
        key = (source.file, source.function)
        if key != previous:
            # Every fl is immediately followed by fn, as Cachegrind requires.
            out.write(f"fl={one_line(source.file)}\nfn={one_line(source.function)}\n")
            previous = key
        out.write(f"{source.line} {costs[source]}\n")
    out.write(f"summary: {sum(costs.values())}\n")


@contextmanager
def atomic_text(path: Path):
    """Do not leave a partial output file if conversion or writing fails."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as out:
            temporary = Path(out.name)
            yield out
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer, e.g. 0 or 0x20000000") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", help="one core's Tarmac .log/.gz file, or - for stdin")
    parser.add_argument("--elf", required=True, type=Path, help="matching ELF, preferably with DWARF (-g)")
    parser.add_argument("-o", "--output", required=True, type=Path, help="output cachegrind.out file")
    parser.add_argument("--format", choices=("auto", "es", "it"), default="auto", dest="input_format")
    parser.add_argument("--addr2line", help="GNU-compatible addr2line executable (auto-detected by default)")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_paths(args)
        tool = find_addr2line(args.addr2line)
        options = dict(input_format=args.input_format, skip_malformed=args.skip_malformed)
        if args.trace == "-":
            counts, stats = count_pcs(sys.stdin, **options)
        else:
            opener = gzip.open if args.trace.lower().endswith(".gz") else open
            with opener(args.trace, "rt", encoding="utf-8-sig", errors="strict") as trace:
                counts, stats = count_pcs(trace, **options)
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
                json.dump(asdict(stats), out, indent=2, sort_keys=True)
                out.write("\n")
        with atomic_text(args.output) as out:
            write_cachegrind(out, costs, args.elf)
        print(f"{stats.instructions:,} instructions, {stats.unique_pcs:,} unique PCs -> {args.output}", file=sys.stderr)
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
