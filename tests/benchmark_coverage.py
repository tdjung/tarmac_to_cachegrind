"""Opt-in synthetic benchmark; not collected by unittest. Python 3.6+."""
import argparse
import hashlib
import json
import statistics
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def ir_hash(path):
    digest = hashlib.sha256()
    with path.open() as source:
        for line in source:
            if line.startswith("events:"):
                line = "events: Ir\n"
            elif line.startswith("0x"):
                line = " ".join(line.split()[:3]) + "\n"
            elif line.startswith("summary:"):
                line = " ".join(line.split()[:2]) + "\n"
            digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path, help="previous standalone script")
    parser.add_argument("--logs", type=int, default=12)
    parser.add_argument("--instructions", type=int, default=200000, help="instructions per log")
    parser.add_argument("--functions", type=int, default=4000, help="ELF coverage size")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if min(args.logs, args.instructions, args.functions, args.workers, args.rounds) < 1:
        parser.error("all counts must be positive")
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="benchmark-", dir=str(repo.parent)) as temporary:
        work = Path(temporary)
        source, elf = work / "firmware.c", work / "firmware.elf"
        source.write_text("\n".join("int f{}(int x) {{ return x * 3 + {}; }}".format(i, i)
                                    for i in range(args.functions)) + "\nint main(void) { return f0(1); }\n")
        subprocess.check_call(["gcc", "-g", "-O0", "-fno-pie", "-no-pie", str(source), "-o", str(elf)])
        symbols = subprocess.check_output(["nm", "-n", str(elf)], universal_newlines=True)
        pcs = [int(line.split()[0], 16) for line in symbols.splitlines()
               if len(line.split()) == 3 and line.split()[2].startswith("f")
               and line.split()[2][1:].isdigit()][:256]
        root = work / "logs"
        root.mkdir()
        for index in range(args.logs):
            folder = root / "case{:03d}".format(index)
            folder.mkdir()
            with (folder / "tarmac.log").open("w") as out:
                for hit in range(args.instructions):
                    pc = pcs[(hit % max(1, len(pcs) // 2) + index) % len(pcs)]
                    if index % 2:
                        out.write("{} ps IT ({:08x}:00000000) {:08x} 2000 T16 MOVS r0,#0\n".format(hit, pc, pc))
                    else:
                        out.write("{} tic ES ({:08x}:2000) T thrd: MOVS r0,#0\n".format(hit, pc))
                    out.write("                              R R0 00000000\n")
                    out.write("{} ps BNR4___I 00000000 00800320\n".format(hit))
        results, expected = [], None
        modes = [("before", args.baseline.resolve()), ("coverage", repo / "tarmac_to_cachegrind.py")]
        for round_number in range(args.rounds):
            for label, script in (modes if round_number % 2 == 0 else list(reversed(modes))):
                output = work / (label + str(round_number))
                command = [sys.executable, str(script), str(root), "--elf", str(elf), "-o", str(output),
                           "--addr2line", "addr2line", "--objdump", "objdump", "--workers", str(args.workers)]
                started = time.monotonic()
                subprocess.check_call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elapsed = time.monotonic() - started
                batch_report = json.loads((output / "batch_report.json").read_text())
                hashes = {item["input"]: ir_hash(output / item["output"]) for item in batch_report["successful"]}
                hashes["TOTAL"] = ir_hash(output / batch_report["merged_output"])
                if expected is None:
                    expected = hashes
                if hashes != expected:
                    raise RuntimeError("PC/line/Ir differs from baseline: " + label)
                result = {"mode": label, "round": round_number + 1, "seconds": elapsed,
                          "identical_ir_profiles": len(hashes),
                          "coverage_seconds": batch_report.get("coverage_seconds", 0),
                          "output_bytes": sum(path.stat().st_size for path in output.iterdir() if path.is_file())}
                results.append(result)
                print(json.dumps(result), flush=True)
        report = {"python": sys.version, "logs": args.logs, "instructions_per_log": args.instructions,
                  "lines_per_log": 3 * args.instructions, "functions": args.functions,
                  "workers": args.workers, "results": results,
                  "median_seconds": {label: statistics.median(item["seconds"] for item in results if item["mode"] == label)
                                     for label, _ in modes}}
        print(json.dumps(report, indent=2))
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n")



if __name__ == "__main__":
    main()
