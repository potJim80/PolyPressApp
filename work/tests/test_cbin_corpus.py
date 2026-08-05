"""Invariant 1, checked against real data instead of constructed cases.

    python3 tests/test_cbin_corpus.py ../IN/corpus100/*.csv
    python3 tests/test_cbin_corpus.py ../IN/corpus100/*.csv --max-mb 20

(Run from work/. Corpora moved to IN/ on 2026-08-04 --
see memory/RESTRUCTURE-2026-08-04.md.)

`tests/test_cbin.py` proves the C encoder is byte-identical to the Python one
on a set of tables written to provoke it. That is the right way to find
disagreements in the corners, and it has found several. It cannot find the
disagreement that only a real file produces -- a column of a shape nobody
thought to construct, a tie broken differently because a float summed in a
different order, a parent chosen one place further along.

So this runs the same check over whatever corpus is on disk. It is slow and
it needs the data downloaded, which is why it is not in the normal suite;
run it before releasing anything.

Three things are compared, and the first is the one that matters:

  bytes      C `compress` must produce the identical archive to fast.encode.
             Not an equivalent one -- identical, including which container
             was chosen, because the container choice is part of the
             guarantee.
  decode     the Python decoder must read the C archive back to the exact
             table.
  restore    the C `restore` must reproduce the table too, so neither
             implementation is trusted alone.

A mismatch prints the first differing offset. That offset has been the
fastest route to the cause every time: the header, the metadata JSON and the
binary sections are laid out in that order, so where the difference starts
says which stage disagreed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from polypress import dtz, fast

BINARY = os.path.join(ROOT, "csrc", "polypress")


def first_difference(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def check(path: str, tmp: str) -> str:
    """Empty string on success, else the reason it failed."""
    arc = os.path.join(tmp, "a.ppz")
    proc = subprocess.run([BINARY, "compress", path, "-o", arc],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=3600)
    if proc.returncode != 0:
        return "C compress exit {}: {}".format(
            proc.returncode, proc.stderr.decode("utf-8", "replace")[-120:])

    table = dtz.read_any(path)
    py = fast.encode(table)
    c = open(arc, "rb").read()
    if py != c:
        off = first_difference(py, c)
        where = ("header" if off < 16 else
                 "metadata or later (offset {})".format(off))
        return ("BYTES DIFFER: python {:,} B, C {:,} B, first difference at "
                "byte {} -- {}".format(len(py), len(c), off, where))

    back = fast.decode(c)
    if back.columns != table.columns or back.rows != table.rows:
        return "python decoder did not reproduce the table from the C archive"

    out = os.path.join(tmp, "r.csv")
    proc = subprocess.run([BINARY, "restore", arc, "-o", out],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=3600)
    if proc.returncode != 0:
        return "C restore exit {}: {}".format(
            proc.returncode, proc.stderr.decode("utf-8", "replace")[-120:])
    restored = dtz.read_any(out)
    if restored.columns != table.columns or restored.rows != table.rows:
        return "C restore did not reproduce the table"
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="test_cbin_corpus")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--max-mb", type=float, default=40.0)
    a = ap.parse_args(argv)

    if not os.path.exists(BINARY):
        print("csrc/polypress not built -- run ./csrc/build.sh first.")
        return 0

    paths = sorted((p for p in a.paths
                    if os.path.getsize(p) <= a.max_mb * 1e6),
                   key=os.path.getsize)
    skipped = len(a.paths) - len(paths)
    print("checking {} datasets ({} over the {} MB ceiling, skipped)"
          .format(len(paths), skipped, a.max_mb))

    failures = []
    for i, path in enumerate(paths, 1):
        base = os.path.basename(path)
        print("[{:>3}/{}] {:<54} ".format(i, len(paths), base[:54]),
              end="", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                why = check(path, tmp)
            except Exception as exc:
                why = "{}: {}".format(type(exc).__name__, str(exc)[:120])
        if why:
            failures.append((base, why))
            print("FAIL  " + why[:60], flush=True)
        else:
            print("identical", flush=True)

    print()
    if failures:
        print("{} of {} datasets DISAGREED:".format(len(failures), len(paths)))
        for base, why in failures:
            print("  {:<50} {}".format(base[:50], why))
        return 1
    print("the C encoder produced byte-identical archives on all {} datasets, "
          "and both decoders reproduced every table".format(len(paths)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
