"""Size AND speed, both directions, against the real binaries and real Parquet.

    python3 benchmarks/bench.py table.csv [more.csv ...]
    python3 benchmarks/bench.py --max-mb 200 big.csv

Compression tools are a ratio/speed trade-off, so a size win means nothing
without the clock next to it. Every tool here is timed the same way: bytes in
from a file, bytes out to a pipe, then the reverse.

Parquet is in this list because it is the honest competitor. Nobody stores a
209-column survey as compressed CSV -- they store it as Parquet -- so a
comparison that only beats gzip and xz has not beaten anything anyone uses.
This script previously omitted it, which meant the headline claim in the
README could not be reproduced by anything in the repo; bench_gov.py had the
Parquet lineup but measured dtz.try_all, the retired container.

The comparison is only fair if Parquet is reproducing the same thing we are.
It is read with type inference, the way a data engineer would, and that can
quietly turn "1.50" into 1.5 -- which is a smaller file for a reason that has
nothing to do with compression. So the exact-text check runs on every dataset
and its verdict is printed next to the numbers. Read the table with that
verdict in hand: if it says Parquet lost formatting, the size column is not
comparing like with like.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
from polypress import dtz, fast

TOOLS = [
    ("gzip -9",        ["gzip", "-9", "-c"],                    ["gzip", "-dc"]),
    ("bzip2 -9",       ["bzip2", "-9", "-c"],                   ["bzip2", "-dc"]),
    ("xz -9e",         ["xz", "-9e", "-c"],                     ["xz", "-dc"]),
    ("zstd -3",        ["zstd", "-3", "-c", "-q"],              ["zstd", "-dc", "-q"]),
    ("zstd -19",       ["zstd", "-19", "-c", "-q"],             ["zstd", "-dc", "-q"]),
    ("zstd -22 ultra", ["zstd", "--ultra", "-22", "-c", "-q"],  ["zstd", "-dc", "-q"]),
    ("brotli -q 11",   ["brotli", "-q", "11", "-c"],            ["brotli", "-dc"]),
]

PARQUET_CODECS = (("snappy", None), ("gzip", 9), ("brotli", 11), ("zstd", 22))

REPS = 3          # overridden by --reps; see main()

# fast.py holds the table as Python strings, roughly 8.5x the CSV bytes, and
# timing runs encode and decode back to back, so both copies are resident.
# 80 MB is therefore about 1.4 GB peak -- the most that fits a laptop without
# swapping. Refuse anything larger unless the caller raises it on purpose.
DEFAULT_MAX_MB = 80


def have(binary: str) -> bool:
    return subprocess.call(["which", binary], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def timed(fn):
    """Best of REPS. Best, not mean: we want the tool's speed, not the
    machine's background noise.

    Three passes is right for one file and wrong for a corpus -- it triples a
    run that is already dominated by brotli -q 11 at about 1 MB/s. --reps 1
    trades timing precision for finishing, and sizes are unaffected either
    way."""
    best, out = None, None
    for _ in range(REPS):
        t0 = time.time()
        out = fn()
        dt = time.time() - t0
        best = dt if best is None else min(best, dt)
    return out, best


def parquet_rows(path, mb):
    """Parquet at each codec, timed both directions, plus the fidelity verdict.

    Returns ([(label, bytes, enc MB/s, dec MB/s)], exact) where `exact` is
    True/False/None -- None meaning the check could not run at all.
    """
    try:
        import pyarrow.csv as pacsv
        import pyarrow.parquet as pq
    except ImportError:
        return [], None

    try:
        table = pacsv.read_csv(path)
    except Exception:
        return [], None      # not delimited, or pyarrow rejects the shape

    out = []
    for name, level in PARQUET_CODECS:
        kw = {"compression": name}
        if level is not None:
            kw["compression_level"] = level

        def write():
            buf = io.BytesIO()
            pq.write_table(table, buf, use_dictionary=True, **kw)
            return buf

        try:
            buf, te = timed(write)
        except Exception:
            continue         # this build of pyarrow lacks the codec
        blob = buf.getvalue()
        _, td = timed(lambda: pq.read_table(io.BytesIO(blob)))
        out.append(("parquet+" + name, len(blob), mb / te, mb / td))

    # Does the typed Parquet path give back the exact printed cells? If not,
    # part of any Parquet size win is discarded formatting, not compression.
    exact = None
    try:
        original = dtz.read_any(path)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="zstd", compression_level=22)
        buf.seek(0)
        back = pq.read_table(buf)
        cols = {c: back.column(c).to_pylist() for c in back.column_names}
        exact = True
        for j, name in enumerate(original.columns):
            if name not in cols:
                exact = False
                break
            got = ["" if v is None else dtz._scalar(v) for v in cols[name]]
            if got != original.column(j):
                exact = False
                break
    except Exception:
        exact = None
    return out, exact


def run_one(path, max_mb) -> int:
    raw = os.path.getsize(path)
    mb = raw / 1e6
    if mb > max_mb:
        print("\n{}: {:.0f} MB exceeds the {} MB ceiling -- skipped.\n"
              "  fast.py expands CSV about 8.5x into Python strings, so this\n"
              "  would need roughly {:.1f} GB. Raise it with --max-mb if the\n"
              "  machine can take it, or use tzip.py stream-compress."
              .format(os.path.basename(path), mb, max_mb, mb * 8.5 / 1000))
        return 0

    data = open(path, "rb").read()
    print("\n" + "=" * 78)
    print("{}   {:,} B".format(os.path.basename(path), raw))

    rows = []
    for label, ce, cd in TOOLS:
        if not have(ce[0]):
            continue
        comp, te = timed(lambda: subprocess.run(
            ce, input=data, stdout=subprocess.PIPE).stdout)
        _, td = timed(lambda: subprocess.run(
            cd, input=comp, stdout=subprocess.PIPE).stdout)
        rows.append((label, len(comp), mb / te, mb / td))

    pq_rows, exact = parquet_rows(path, mb)
    rows.extend(pq_rows)

    t = dtz.read_any(path)
    nr, nc = t.shape
    print("{:,} rows x {} columns".format(nr, nc))
    print("=" * 78)
    blob, te = timed(lambda: fast.encode(t))
    back, td = timed(lambda: fast.decode(blob))
    if back.columns != t.columns or back.rows != t.rows:
        print("  *** POLYPRESS FAILED TO ROUND-TRIP THIS FILE ***")
    rows.append(("*** polypress", len(blob), mb / te, mb / td))

    print("  {:<16} {:>12} {:>7} {:>11} {:>11}".format(
        "codec", "bytes", "ratio", "enc MB/s", "dec MB/s"))
    print("  " + "-" * 62)
    for label, n, e, d in sorted(rows, key=lambda r: r[1]):
        print("  {:<16} {:>12,} {:>6.2f}x {:>10.1f} {:>11.1f}".format(
            label, n, raw / n, e, d))

    ours = min(n for lb, n, _, _ in rows if lb.startswith("***"))
    others = [(lb, n) for lb, n, _, _ in rows if not lb.startswith("***")]
    if others:
        blabel, bn = min(others, key=lambda r: r[1])
        print("\n  best other: {} at {:,} B -- polypress is {:.2f}x {}".format(
            blabel, bn, max(bn, ours) / min(bn, ours),
            "smaller" if ours < bn else "LARGER"))
    if exact is True:
        print("  parquet reproduced the exact printed text of every column, "
              "so the comparison is clean")
    elif exact is False:
        print("  NOTE: parquet did NOT reproduce the exact printed text -- "
              "part of its size win is discarded formatting, not compression")
    else:
        print("  NOTE: the parquet fidelity check could not run on this file")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB,
                    help="skip inputs larger than this (default {})"
                         .format(DEFAULT_MAX_MB))
    ap.add_argument("--reps", type=int, default=REPS,
                    help="timing passes per codec (default {}); use 1 for a "
                         "corpus run, where sizes matter and the clock is "
                         "only indicative".format(REPS))
    a = ap.parse_args(argv)
    globals()["REPS"] = max(1, a.reps)
    for p in a.paths:
        run_one(p, a.max_mb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
