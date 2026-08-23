#!/usr/bin/env python3
"""TEST-9 -- the CSV as a plain text stream, through every model we have.

Mahdi's Thought 12: forget rows and columns, feed the actual text. Before
building anything, measure what the EXISTING model families do to that same
text, because they are the honest ceiling for "a better text model".

Everything compresses the identical canonical CSV (the same bytes TEST-8's
baseline used), so the comparison is like for like. Every result is verified by
decompressing and comparing to the original.

Model families measured:
  lzma2   xz -9e            -- LZ77 + range coder            (the baseline)
  bwt     bzip2 -9          -- block sort + MTF + Huffman
  lz-fast zstd -22          -- LZ77 + FSE, big window
  lz-brot brotli -q 11      -- LZ77 + context modelling + static dictionary
  ppmd    7zz -m0=PPMd      -- context mixing by prediction, order 6/16/32
  ppmd-x  7zz -m0=PPMd o=32 -- deepest practical order

PPMd is the interesting one: it is not an LZ at all. It predicts the next byte
from the previous N bytes with an adaptive arithmetic coder. If a different
MODEL is where the room is (LAW 2), PPMd is the cheapest way to find out.

Run from work/:  python3 ../memory/tests/TEST-9/run_textstream.py
"""
from __future__ import annotations

import csv
import glob
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

MAXROWS = int(os.environ.get("T9_ROWS", "40000"))
WORKERS = int(os.environ.get("T9_WORKERS", "2"))   # half of 10 cores, headroom
SCRATCH = os.environ.get(
    "T9_SCRATCH",
    "/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
    "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/test9")

# name -> (compress argv template, decompress argv template)
# {i} input path, {o} output path.  Memory settings are chosen so that four
# of these running at once stay under 4 GB.
METHODS = [
    ("xz -9e",        ["xz", "-9e", "-T1", "-k", "-c", "{i}"],
                      ["xz", "-d", "-c", "{i}"]),
    ("bzip2 -9",      ["bzip2", "-9", "-c", "{i}"],
                      ["bzip2", "-d", "-c", "{i}"]),
    ("zstd -22",      ["zstd", "-22", "--ultra", "--long=27", "-T1", "-q",
                       "-c", "{i}"],
                      ["zstd", "-d", "--long=27", "-q", "-c", "{i}"]),
    ("brotli -q11",   ["brotli", "-q", "11", "--large_window=24", "-c", "{i}"],
                      ["brotli", "-d", "-c", "{i}"]),
]

# 7-Zip methods need a real archive, handled separately
SEVENZ = [
    ("ppmd o4",   "PPMd:mem=256m:o=4"),
    ("ppmd o6",   "PPMd:mem=256m:o=6"),
    ("ppmd o8",   "PPMd:mem=256m:o=8"),
    ("ppmd o12",  "PPMd:mem=256m:o=12"),
    ("ppmd o16",  "PPMd:mem=256m:o=16"),
    ("lzma2 mx9", "LZMA2:a=1:mf=bt4:fb=273:d=64m"),
]


def canonical_csv(path: str) -> bytes:
    sys.path.insert(0, os.getcwd())
    from polypress import dtz
    t = dtz.read_any(path)
    rows = t.rows[:MAXROWS]
    ncol = len(t.columns)
    rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
            for r in rows]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def _run(argv, inp, outp=None):
    argv = [a.format(i=inp, o=outp or "") for a in argv]
    t0 = time.perf_counter()
    r = subprocess.run(argv, capture_output=True)
    dt = time.perf_counter() - t0
    if r.returncode != 0:
        raise RuntimeError(f"{argv[0]}: {r.stderr[:200]!r}")
    return r.stdout, dt


def measure_pipe(name, cargv, dargv, src: str, raw: bytes):
    blob, ct = _run(cargv, src)
    with tempfile.NamedTemporaryFile(dir=SCRATCH, delete=False) as f:
        f.write(blob)
        tmp = f.name
    try:
        back, dt = _run(dargv, tmp)
    finally:
        os.unlink(tmp)
    ok = back == raw
    return name, len(blob), ct, dt, ok


def measure_7z(name, method, src: str, raw: bytes):
    d = tempfile.mkdtemp(dir=SCRATCH)
    arc = os.path.join(d, "a.7z")
    try:
        _, ct = _run(["7zz", "a", "-t7z", f"-m0={method}", "-mmt=1", "-bso0",
                      "-bsp0", arc, src], src)
        size = os.path.getsize(arc)
        out = os.path.join(d, "out")
        os.makedirs(out, exist_ok=True)
        _, dt = _run(["7zz", "x", f"-o{out}", "-bso0", "-bsp0", "-y", arc], src)
        got = open(os.path.join(out, os.path.basename(src)), "rb").read()
        return name, size, ct, dt, got == raw
    finally:
        shutil.rmtree(d, ignore_errors=True)


def job(args):
    path, = args
    os.makedirs(SCRATCH, exist_ok=True)
    raw = canonical_csv(path)
    with tempfile.NamedTemporaryFile(dir=SCRATCH, suffix=".csv",
                                     delete=False) as f:
        f.write(raw)
        src = f.name
    rows = []
    try:
        for name, c, d in METHODS:
            try:
                rows.append(measure_pipe(name, c, d, src, raw))
            except Exception as e:
                rows.append((name, -1, 0.0, 0.0, False))
        for name, m in SEVENZ:
            try:
                rows.append(measure_7z(name, m, src, raw))
            except Exception as e:
                rows.append((name, -1, 0.0, 0.0, False))
    finally:
        os.unlink(src)
    return os.path.basename(path).replace(".csv", ""), len(raw), rows


def main():
    os.makedirs(SCRATCH, exist_ok=True)
    paths = []
    for pat in (sys.argv[1:] or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    names = [n for n, _, _ in METHODS] + [n for n, _ in SEVENZ]
    print(f"# TEST-9  the CSV as a text stream, {MAXROWS:,} rows max, "
          f"{WORKERS} workers")
    print("# every result decompressed and compared to the original\n")
    hdr = f"{'table':<24}{'raw MB':>8}" + "".join(f"{n:>13}" for n in names)
    print(hdr)
    print("-" * len(hdr))

    tot = {n: 0 for n in names}
    ctime = {n: 0.0 for n in names}
    dtime = {n: 0.0 for n in names}
    bad = []
    traw = 0
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        for tab, rawlen, rows in ex.map(job, [(p,) for p in paths]):
            cells = []
            for name, size, ct, dt, ok in rows:
                if size < 0:
                    cells.append("ERR")
                    continue
                if not ok:
                    bad.append((tab, name))
                tot[name] += size
                ctime[name] += ct
                dtime[name] += dt
                cells.append(f"{size:,}" + ("" if ok else "!"))
            traw += rawlen
            print(f"{tab[:23]:<24}{rawlen/1e6:>8.1f}"
                  + "".join(f"{c:>13}" for c in cells), flush=True)

    print("-" * len(hdr))
    print(f"{'TOTAL':<24}{traw/1e6:>8.1f}"
          + "".join(f"{tot[n]:>13,}" for n in names))
    base = tot["xz -9e"]
    print(f"{'vs xz -9e':<24}{'':>8}"
          + "".join(f"{tot[n]/base:>13.3f}" for n in names))
    print(f"\n{'compress s':<24}{'':>8}"
          + "".join(f"{ctime[n]:>13.1f}" for n in names))
    print(f"{'decompress s':<24}{'':>8}"
          + "".join(f"{dtime[n]:>13.1f}" for n in names))
    if bad:
        print(f"\nROUND-TRIP FAILURES: {bad}")
    else:
        print("\nall round-trips exact")


if __name__ == "__main__":
    main()
