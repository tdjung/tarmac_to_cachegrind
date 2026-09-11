#!/usr/bin/env python3
"""Recursively convert one ELF's Tarmac scenarios and merge Ir in memory.

Python 3.6.8+, standard library only. Import the single-file converter from the
same directory. No shell subprocess per log and no post-merge file rereading.
"""

import argparse
from collections import Counter
import fnmatch
import gzip
import json
import os
from pathlib import Path
import sys

import tarmac_to_cachegrind as converter


class ProfileContext:
    """Keep ELF instruction/source analysis and unknown-PC results across logs."""

    def __init__(self, args):
        self.elf = args.elf
        self.offset = args.load_offset
        self.addr2line = converter.find_addr2line(args.addr2line)
        self.sources = {}
        self.baseline = Counter()
        self.elf_pc_count = 0
        self.fallback = None
        if not args.executed_only or self.addr2line is None:
            objdump = converter.find_objdump(args.objdump, self.addr2line)
            dump = converter.disassemble(self.elf, objdump, with_lines=self.addr2line is None)
            if self.addr2line is None:
                print("info: addr2line not found; using objdump -l", file=sys.stderr)
                self.fallback = converter.objdump_sources(dump)
                pcs = set(self.fallback)
            else:
                pcs = converter.instruction_pcs(dump)
            if not pcs:
                raise converter.ConversionError("objdump found no executable instructions in the ELF")
            if not args.executed_only:
                runtime = Counter()
                self.elf_pc_count = converter.seed_pcs(runtime, pcs, self.offset)
                self.resolve(runtime)
                for source in self.sources.values():
                    self.baseline[source] = 0

    def resolve(self, pcs):
        missing = set(pcs).difference(self.sources)
        if any(pc - self.offset < 0 for pc in missing):
            raise converter.ConversionError("--load-offset produces a negative ELF address")
        if self.addr2line is not None:
            self.sources.update(converter.resolve_pcs(
                missing, self.elf, self.addr2line, load_offset=self.offset))
        else:
            self.sources.update({pc: self.fallback.get(pc - self.offset, converter.Source())
                                 for pc in missing})

    def costs(self, counts, stats):
        self.resolve(counts)
        stats.elf_instruction_pcs = self.elf_pc_count
        costs = self.baseline.copy()
        # Counter.update preserves zero entries, unlike Counter addition.
        costs.update(converter.aggregate(counts, self.sources, stats))
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


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="parent directory containing scenario logs")
    parser.add_argument("--elf", required=True, type=Path, help="ELF shared by all selected logs")
    parser.add_argument("-o", "--output-dir", type=Path, help="empty output directory (default: ROOT/cachegrind-output)")
    parser.add_argument("--pattern", action="append", help="basename glob, repeatable; default: tarmac*.log and tarmac*.log.gz")
    parser.add_argument("--addr2line")
    parser.add_argument("--objdump")
    parser.add_argument("--load-offset", type=converter.integer, default=0)
    parser.add_argument("--format", choices=("auto", "es", "it"), default="auto", dest="input_format")
    parser.add_argument("--executed-only", action="store_true")
    parser.add_argument("--merge-only", action="store_true", help="write only total and report; omit individual profiles")
    return parser


def run(args):
    root = args.root.resolve()
    output = (args.output_dir or root / "cachegrind-output").resolve()
    if not root.is_dir():
        raise converter.ConversionError("root must be a directory")
    if output == root:
        raise converter.ConversionError("output directory must differ from the input root")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise converter.ConversionError("output directory must be empty; choose a new --output-dir for each run")
    with args.elf.open("rb") as file:
        if file.read(4) != b"\x7fELF":
            raise converter.ConversionError("--elf must point to an ELF file")
    paths = discover(root, args.pattern or ["tarmac*.log", "tarmac*.log.gz"], output)
    if not paths:
        raise converter.ConversionError("no matching logs found")
    names = [output_name(path, root) for path in paths]
    if len(set(names)) != len(names):
        raise converter.ConversionError("output filename collision; narrow --pattern or rename colliding input folders/logs")
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
                counts, stats = converter.count_pcs(trace, input_format=args.input_format,
                                                   allow_empty=not args.executed_only)
            if not stats.events:
                raise converter.ConversionError("no Tarmac instruction/exception records; empty or unrelated log")
            costs = context.costs(counts, stats)
            if not args.merge_only:
                with converter.atomic_text(output / name) as file:
                    converter.write_cachegrind(file, costs, args.elf)
            total.update(costs)
            report["total_instructions"] += stats.instructions
            report["successful"].append({"input": str(path.relative_to(root)),
                                         "output": None if args.merge_only else name,
                                         "stats": vars(stats)})
            print("[{}/{}] {}: {:,} instructions".format(index, len(paths), path.relative_to(root), stats.instructions), file=sys.stderr)
        except (converter.ConversionError, OSError, UnicodeError, EOFError) as exc:
            report["failed"].append({"input": str(path.relative_to(root)), "error": str(exc)})
            print("[{}/{}] FAILED {}: {}".format(index, len(paths), path.relative_to(root), exc), file=sys.stderr)
    if report["successful"]:
        name = "total_merge_cachegrind.out"
        with converter.atomic_text(output / name) as file:
            if report["failed"]:
                file.write("desc: PARTIAL MERGE: {} of {} logs failed; see batch_report.json\n".format(len(report["failed"]), len(paths)))
            converter.write_cachegrind(file, total, args.elf)
        report["merged_output"] = name
    with converter.atomic_text(output / "batch_report.json") as file:
        json.dump(report, file, indent=2, sort_keys=True)
        file.write("\n")
    print("Completed: {} succeeded, {} failed; {:,} instructions; {}".format(
        len(report["successful"]), len(report["failed"]), report["total_instructions"], output), file=sys.stderr)
    return 1 if report["failed"] else 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (converter.ConversionError, OSError, UnicodeError, EOFError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
