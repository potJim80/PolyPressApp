"""Run the whole competitor lineup over a corpus, one dataset per process.

    python3 benchmarks/sweep.py corpus100/*.csv --out results/socrata100.jsonl
    python3 benchmarks/sweep.py corpus/*.csv --out results/curated.jsonl

Writes one JSON line per dataset, appended as it goes, and **skips any dataset
already present in the output**. A sweep of a hundred tables takes hours; it
has to be safe to interrupt and restart, and it has to not lose the first
ninety when the ninety-first is the one that breaks.

Memory
------
Every dataset is measured in a fresh subprocess, so peak RSS is whatever the
single largest table needs rather than the accumulated total. The working
ceiling is 2 GB. `fast.py` expands CSV about 8.5x into Python strings and the
run holds an encoded and a decoded copy at once, which CLAUDE.md's corrected
rule prices at (input MB x 22) + 700 -- so the default 40 MB input cap
predicts about 1.6 GB.

That rule has been wrong before, by 18%, so it is only used to decide what to
*start*. Every worker reports its measured peak, this script prints the
maximum it saw, and `--rss-abort` stops the run if any dataset actually
crosses the ceiling. Predict to schedule; measure to believe.

Anything over the cap is truncated at a row boundary and recorded as
`truncated_from`. Truncating is honest here because every codec in the
comparison is then handed the identical file -- it changes which table is
being measured, not who wins on it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "measure_one.py")


def truncate_to(src: str, dst: str, max_bytes: int) -> None:
    with open(src, "rb") as fh:
        body = fh.read(max_bytes + (1 << 20))
    cut = body.rfind(b"\n", 0, max_bytes)
    with open(dst, "wb") as fh:
        fh.write(body[:cut + 1] if cut > 0 else body[:max_bytes])


def already_done(path: str) -> dict:
    done = {}
    if not os.path.exists(path):
        return done
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            done[rec.get("file", "")] = rec
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sweep")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", required=True, help="JSONL results file")
    ap.add_argument("--max-mb", type=float, default=40.0,
                    help="truncate inputs larger than this (default 40)")
    ap.add_argument("--rss-abort", type=float, default=2000.0,
                    help="stop if a worker's measured peak RSS exceeds this")
    ap.add_argument("--timeout", type=float, default=3600.0,
                    help="per-dataset wall clock limit, seconds")
    ap.add_argument("--scratch", default=None,
                    help="where truncated copies go (default alongside --out)")
    a = ap.parse_args(argv)

    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    scratch = a.scratch or os.path.join(
        os.path.dirname(os.path.abspath(a.out)), "truncated")
    max_bytes = int(a.max_mb * 1e6)

    done = already_done(a.out)
    todo = [p for p in sorted(a.paths)
            if os.path.basename(p) not in done]
    # Smallest first: a memory problem on the biggest table then cannot take
    # the rest of the run down with it.
    todo.sort(key=os.path.getsize)

    print("{} datasets, {} already measured, {} to go"
          .format(len(a.paths), len(done), len(todo)), flush=True)

    worst_rss, failures, t_start = 0.0, 0, time.time()
    for i, path in enumerate(todo, 1):
        base = os.path.basename(path)
        size = os.path.getsize(path)
        extra = {}
        run_path = path
        if size > max_bytes:
            os.makedirs(scratch, exist_ok=True)
            run_path = os.path.join(scratch, base)
            if not os.path.exists(run_path):
                truncate_to(path, run_path, max_bytes)
            extra["truncated_from"] = size

        print("[{:>3}/{}] {:<52} {:>11,} B ".format(
            i, len(todo), base[:52], os.path.getsize(run_path)),
            end="", flush=True)

        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, WORKER, run_path, "--json"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=a.timeout)
            line = proc.stdout.decode("utf-8", "replace").strip().splitlines()
            rec = json.loads(line[-1]) if line else {
                "file": base, "bytes": size,
                "error": "worker produced no output: "
                         + proc.stderr.decode("utf-8", "replace")[-160:]}
        except subprocess.TimeoutExpired:
            rec = {"file": base, "bytes": size,
                   "error": "timed out after {:.0f}s".format(a.timeout)}
        except Exception as exc:
            rec = {"file": base, "bytes": size,
                   "error": "{}: {}".format(type(exc).__name__, str(exc)[:160])}

        rec["file"] = base
        rec.update(extra)
        rec["wall_s"] = round(time.time() - t0, 2)
        with open(a.out, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

        rss = rec.get("peak_mb") or 0.0
        worst_rss = max(worst_rss, rss)
        if "error" in rec:
            failures += 1
            print("ERROR  {}".format(rec["error"][:60]), flush=True)
        else:
            best = min((v["bytes"], k) for k, v in rec["results"].items()
                       if not k.startswith("polypress"))
            ours = rec["results"]["polypress"]["bytes"]
            print("{:>5.0f}s  rss {:>6.0f}MB  ppz {:.2f}x  vs best {:.2f}x  {}"
                  .format(rec["wall_s"], rss, rec["bytes"] / ours,
                          rec["bytes"] / best[0],
                          "WIN" if ours < best[0] else "loss"), flush=True)

        if rss > a.rss_abort:
            print("\nABORT: {} peaked at {:.0f} MB, over the {:.0f} MB ceiling."
                  .format(base, rss, a.rss_abort))
            print("Lower --max-mb and rerun; finished datasets are kept.")
            return 2

    print("\ndone in {:.1f} min, {} failures, worst peak RSS {:.0f} MB"
          .format((time.time() - t_start) / 60, failures, worst_rss))
    return 0


if __name__ == "__main__":
    sys.exit(main())
