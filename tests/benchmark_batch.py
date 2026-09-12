"""Opt-in synthetic benchmark; not collected by unittest. Python 3.6+."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path, help="previous standalone script")
    parser.add_argument("--logs", type=int, default=12)
    parser.add_argument("--instructions", type=int, default=200000, help="instructions per log")
    parser.add_argument("--functions", type=int, default=4000, help="ELF coverage size")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if min(args.logs, args.instructions, args.functions, args.workers) < 1:
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
                    pc = pcs[hit % len(pcs)]
                    if index % 2:
                        out.write("{} ps IT ({:08x}:00000000) {:08x} 2000 T16 MOVS r0,#0\n".format(hit, pc, pc))
                    else:
                        out.write("{} tic ES ({:08x}:2000) T thrd: MOVS r0,#0\n".format(hit, pc))
                    out.write("                              R R0 00000000\n")
                    out.write("{} ps BNR4___I 00000000 00800320\n".format(hit))
        results, expected = [], None
        for label, script, workers in [("baseline", args.baseline.resolve(), None),
                                        ("optimized_serial", repo / "tarmac_to_cachegrind.py", 1),
                                        ("optimized_parallel", repo / "tarmac_to_cachegrind.py", args.workers)]:
            output = work / label
            command = [sys.executable, str(script), str(root), "--elf", str(elf), "-o", str(output),
                       "--addr2line", "addr2line", "--objdump", "objdump"]
            if workers is not None:
                command += ["--workers", str(workers)]
            started = time.monotonic()
            subprocess.check_call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elapsed = time.monotonic() - started
            hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in output.glob("*.out")}
            if expected is None:
                expected = hashes
            if hashes != expected:
                raise RuntimeError("profile bytes differ from baseline: " + label)
            result = {"mode": label, "seconds": elapsed, "identical_profiles": len(hashes)}
            results.append(result)
            print(json.dumps(result), flush=True)
        print(json.dumps({"python": sys.version, "logs": args.logs, "instructions_per_log": args.instructions,
                          "lines_per_log": 3 * args.instructions, "functions": args.functions,
                          "workers": args.workers, "results": results}, indent=2))


if __name__ == "__main__":
    main()
