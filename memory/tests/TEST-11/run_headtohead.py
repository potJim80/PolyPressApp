#!/usr/bin/env python3
"""TEST-11 -- the head-to-head TEST-9 forces: the SHIPPING codec vs PPMd.

TEST-9 found PPMd order-16 beating `xz -9e` by 10.3% on the raw CSV text, and
found that no context-mixing model has ever been in this project's competitor
set. That raises one question that has to be answered before anything else is
built: **is PPMd a threat to Polypress, or only to the streaming scheme?**

Same 13 tables, same 40,000-row cap, same canonical CSV. `fast.encode` is the
shipping encoder. Its output is verified by `fast.decode`; the PPMd archives
are extracted and compared to the source bytes.

Run from work/:  python3 ../memory/tests/TEST-11/run_headtohead.py
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

sys.path.insert(0, os.getcwd())
SCRATCH = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
           "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/test11")
MAXROWS = int(os.environ.get("T11_ROWS", "40000"))
ORDERS = [8, 12, 16]


def _sh(argv):
    r = subprocess.run(argv, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"{argv[0]}: {r.stderr[:300]!r}")
    return r.stdout


def ppmd(data: bytes, order: int):
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        src = os.path.join(d, "d.bin")
        open(src, "wb").write(data)
        arc = os.path.join(d, "a.7z")
        t0 = time.perf_counter()
        _sh(["7zz", "a", "-t7z", f"-m0=PPMd:mem=256m:o={order}", "-mmt=1",
             "-bso0", "-bsp0", arc, src])
        ct = time.perf_counter() - t0
        size = os.path.getsize(arc)
        out = os.path.join(d, "out")
        os.makedirs(out, exist_ok=True)
        _sh(["7zz", "x", f"-o{out}", "-bso0", "-bsp0", "-y", arc])
        ok = open(os.path.join(out, "d.bin"), "rb").read() == data
        return size, ct, ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    os.makedirs(SCRATCH, exist_ok=True)
    from polypress import dtz, fast

    paths = []
    for pat in (sys.argv[1:] or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    names = ["xz -9e"] + [f"ppmd o{o}" for o in ORDERS] + ["best ppmd",
                                                           "polypress"]
    print(f"# TEST-11  shipping codec vs PPMd, {MAXROWS:,} rows max")
    print("# polypress verified by fast.decode; PPMd archives extracted and "
          "compared\n")
    hdr = f"{'table':<24}{'raw MB':>8}" + "".join(f"{n:>13}" for n in names) \
        + f"{'pp/best':>9}"
    print(hdr)
    print("-" * len(hdr))

    tot = {n: 0 for n in names}
    ptime = xtime = 0.0
    for p in paths:
        try:
            t = dtz.read_any(p)
        except Exception as e:
            print(f"{os.path.basename(p)[:23]:<24} SKIP {type(e).__name__}")
            continue
        rows = t.rows[:MAXROWS]
        if not rows:
            continue
        ncol = len(t.columns)
        rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
                for r in rows]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        for r in rows:
            w.writerow(r)
        raw = buf.getvalue().encode()

        t0 = time.perf_counter()
        xzb = len(lzma.compress(raw, format=lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}]))
        xtime += time.perf_counter() - t0

        sizes = {"xz -9e": xzb}
        for o in ORDERS:
            s, ct, ok = ppmd(raw, o)
            assert ok, f"{p}: ppmd o{o} round-trip"
            sizes[f"ppmd o{o}"] = s
        sizes["best ppmd"] = min(sizes[f"ppmd o{o}"] for o in ORDERS)

        class T:
            pass
        tt = T()
        tt.columns = t.columns
        tt.rows = rows
        tt.column = lambda j, _r=rows: [r[j] for r in _r]
        t0 = time.perf_counter()
        blob = fast.encode(tt)
        ptime += time.perf_counter() - t0
        back = fast.decode(blob)
        assert back.rows == rows, f"{p}: polypress round-trip"
        sizes["polypress"] = len(blob)

        n = os.path.basename(p).replace(".csv", "")[:23]
        print(f"{n:<24}{len(raw)/1e6:>8.1f}"
              + "".join(f"{sizes[k]:>13,}" for k in names)
              + f"{sizes['polypress']/sizes['best ppmd']:>9.3f}", flush=True)
        for k in names:
            tot[k] += sizes[k]

    print("-" * len(hdr))
    print(f"{'TOTAL':<24}{'':>8}" + "".join(f"{tot[k]:>13,}" for k in names)
          + f"{tot['polypress']/tot['best ppmd']:>9.3f}")
    print(f"{'vs xz':<24}{'':>8}"
          + "".join(f"{tot[k]/tot['xz -9e']:>13.3f}" for k in names))
    print(f"\nencode time: polypress {ptime:.1f}s, xz {xtime:.1f}s")


if __name__ == "__main__":
    main()
