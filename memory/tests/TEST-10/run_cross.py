#!/usr/bin/env python3
"""TEST-10 -- transform x backend, the whole grid.

TEST-9 asked what the best MODEL does to the raw CSV text and found PPMd
beating xz on some tables by a wide margin and losing on others. TEST-8 asked
what our TRANSFORM does with xz behind it. This test crosses the two, because
the interesting question is whether they STACK or whether they were finding
the same structure twice:

  transforms                       backends
    raw      canonical CSV           xz -9e      LZ77 + range coder
    E        row-major inline dict   ppmd o6     order-6 context model
    F        col-major inline dict   ppmd o8     order-8 context model
                                     brotli q11  LZ77 + context modelling

Every cell of the grid is decompressed AND decoded back to the original table
before its bytes are counted.

Run from work/:  python3 ../memory/tests/TEST-10/run_cross.py
"""
from __future__ import annotations

import csv
import glob
import io
import lzma
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "TEST-8"))
sys.path.insert(0, os.getcwd())
import inlinedict as I

MAXROWS = int(os.environ.get("T10_ROWS", "40000"))
WORKERS = int(os.environ.get("T10_WORKERS", "2"))
SCRATCH = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
           "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/test10")

BACKENDS = ["xz", "ppmd o8", "ppmd o16", "brotli"]
TRANSFORMS = ["raw", "E", "F"]


def _sh(argv):
    r = subprocess.run(argv, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"{argv[0]}: {r.stderr[:300]!r}")
    return r.stdout


def comp(backend: str, data: bytes) -> bytes:
    if backend == "xz":
        return lzma.compress(data, format=lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        src = os.path.join(d, "d.bin")
        open(src, "wb").write(data)
        if backend == "brotli":
            return _sh(["brotli", "-q", "11", "--large_window=24", "-c", src])
        order = backend.split("o")[1]
        arc = os.path.join(d, "a.7z")
        _sh(["7zz", "a", "-t7z", f"-m0=PPMd:mem=256m:o={order}", "-mmt=1",
             "-bso0", "-bsp0", arc, src])
        return open(arc, "rb").read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def decomp(backend: str, blob: bytes) -> bytes:
    if backend == "xz":
        return lzma.decompress(blob, format=lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        if backend == "brotli":
            src = os.path.join(d, "a.br")
            open(src, "wb").write(blob)
            return _sh(["brotli", "-d", "-c", src])
        arc = os.path.join(d, "a.7z")
        open(arc, "wb").write(blob)
        out = os.path.join(d, "out")
        os.makedirs(out, exist_ok=True)
        _sh(["7zz", "x", f"-o{out}", "-bso0", "-bsp0", "-y", arc])
        return open(os.path.join(out, "d.bin"), "rb").read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def canonical(path):
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
    return rows, buf.getvalue().encode()


def job(args):
    path, = args
    os.makedirs(SCRATCH, exist_ok=True)
    rows, raw = canonical(path)

    payload = {"raw": raw}
    hE, bE = I.stream_parts(rows, True)
    payload["E"] = I._container([hE, bE])
    hF, bF = I.colmajor_parts(rows)
    payload["F"] = I._container([hF, bF])

    res = {}
    for tf in TRANSFORMS:
        data = payload[tf]
        for be in BACKENDS:
            try:
                t0 = time.perf_counter()
                blob = comp(be, data)
                ct = time.perf_counter() - t0
                back = decomp(be, blob)
                if back != data:
                    res[(tf, be)] = (-1, ct, False)
                    continue
                if tf == "raw":
                    ok = back == raw
                else:
                    p = I._uncontainer(back)
                    got = (I.decode_stream_parts(p[0], p[1], True) if tf == "E"
                           else I.decode_colmajor_parts(p[0], p[1]))
                    ok = got == rows
                res[(tf, be)] = (len(blob), ct, ok)
            except Exception:
                res[(tf, be)] = (-1, 0.0, False)
    return os.path.basename(path).replace(".csv", ""), len(raw), res


def main():
    os.makedirs(SCRATCH, exist_ok=True)
    paths = []
    for pat in (sys.argv[1:] or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    keys = [(tf, be) for tf in TRANSFORMS for be in BACKENDS]
    print(f"# TEST-10  transform x backend, {MAXROWS:,} rows max")
    print("# every cell decompressed AND decoded back to the table\n")
    hdr = f"{'table':<22}" + "".join(f"{tf+'/'+be:>14}" for tf, be in keys)
    print(hdr)
    print("-" * len(hdr))

    tot = {k: 0 for k in keys}
    tim = {k: 0.0 for k in keys}
    bad = []
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        for tab, rawlen, res in ex.map(job, [(p,) for p in paths]):
            cells = []
            for k in keys:
                size, ct, ok = res[k]
                if size < 0 or not ok:
                    cells.append("FAIL")
                    bad.append((tab, k))
                    continue
                tot[k] += size
                tim[k] += ct
                cells.append(f"{size:,}")
            print(f"{tab[:21]:<22}" + "".join(f"{c:>14}" for c in cells),
                  flush=True)

    print("-" * len(hdr))
    print(f"{'TOTAL':<22}" + "".join(f"{tot[k]:>14,}" for k in keys))
    base = tot[("raw", "xz")]
    print(f"{'vs raw/xz':<22}" + "".join(f"{tot[k]/base:>14.3f}" for k in keys))
    print(f"{'compress s':<22}" + "".join(f"{tim[k]:>14.1f}" for k in keys))
    print(f"\nfailures: {bad if bad else 'none'}")

    print("\nbest cell per table (size, then which):")
    # recompute cheaply from totals is not possible; note only the aggregate
    best = min(keys, key=lambda k: tot[k])
    print(f"  aggregate winner: {best[0]}/{best[1]} at {tot[best]:,} "
          f"({tot[best]/base:.3f} of raw/xz)")


if __name__ == "__main__":
    main()
