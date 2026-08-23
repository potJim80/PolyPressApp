#!/usr/bin/env python3
"""TEST-14 -- kanzi as a measured fallback candidate, checked here rather than
taken on report.

The research pass measured kanzi `-l9` (a word-dictionary TEXT transform in
front of the TPAQX context mixer) at 25.3% below `xz -9e` on this corpus, and
beating Polypress on 7 of 13 tables. That was on files truncated to 10 MB,
with Polypress numbers produced outside this repo's harness. This re-runs it
on the standard 40,000-row canonical CSV every other test in this series uses,
with Polypress driven by `fast.encode` / `fast.decode` and every kanzi archive
decompressed and compared.

The question is not "is kanzi better". It is **invariant 2's question**: does
`min(polypress, kanzi)` beat polypress, and by how much, on tables where each
one wins.

Run from work/:  python3 ../memory/tests/TEST-14/run_kanzi.py
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

KANZI = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
         "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/kanzi-cpp/bin/"
         "kanzi_dynamic")
SCRATCH = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
           "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/test14")
MAXROWS = int(os.environ.get("T14_ROWS", "40000"))
# -b 16m keeps peak RSS near 900 MB per the research pass's measurement; the
# session ceiling is 3 GB total and fast.encode wants a chunk of it.
LEVELS = [("kanzi -l7", "7"), ("kanzi -l9", "9")]


def run(argv):
    r = subprocess.run(argv, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"{argv[0]}: {r.stderr[:300]!r}")
    return r.stdout


def kanzi(data: bytes, level: str):
    os.makedirs(SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        src = os.path.join(d, "in.csv")
        arc = os.path.join(d, "out.knz")
        back = os.path.join(d, "back.csv")
        open(src, "wb").write(data)
        t0 = time.perf_counter()
        run([KANZI, "-c", "-i", src, "-o", arc, "-l", level, "-b", "16m",
             "-j", "1", "-f"])
        ct = time.perf_counter() - t0
        size = os.path.getsize(arc)
        t0 = time.perf_counter()
        run([KANZI, "-d", "-i", arc, "-o", back, "-j", "1", "-f"])
        dt = time.perf_counter() - t0
        ok = open(back, "rb").read() == data
        return size, ct, dt, ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


def bzip3(data: bytes):
    if not shutil.which("bzip3"):
        return None
    os.makedirs(SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        src = os.path.join(d, "in.csv")
        open(src, "wb").write(data)
        run(["bzip3", "-e", "-b", "16", src])
        arc = src + ".bz3"
        size = os.path.getsize(arc)
        run(["bzip3", "-d", arc])
        ok = open(src, "rb").read() == data
        return size if ok else None
    except Exception:
        return None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    os.makedirs(SCRATCH, exist_ok=True)
    if not os.path.exists(KANZI):
        print(f"kanzi binary not found at {KANZI}")
        return 1
    from polypress import dtz, fast

    paths = []
    for pat in (sys.argv[1:] or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    names = ["xz -9e", "bzip3", "kanzi -l7", "kanzi -l9", "polypress",
             "min(pp,knz)"]
    print(f"# TEST-14  kanzi as a fallback candidate, {MAXROWS:,} rows max")
    print("# kanzi archives decompressed and compared; polypress verified by "
          "fast.decode\n")
    hdr = f"{'table':<22}{'raw MB':>8}" + "".join(f"{n:>13}" for n in names) \
        + f"{'knz/pp':>9}"
    print(hdr)
    print("-" * len(hdr))

    tot = {n: 0 for n in names}
    kt = pt = 0.0
    wins = 0
    for p in paths:
        try:
            t = dtz.read_any(p)
        except Exception as e:
            print(f"{os.path.basename(p)[:21]:<22} SKIP {type(e).__name__}")
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

        s = {"xz -9e": len(lzma.compress(raw, format=lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}]))}
        b3 = bzip3(raw)
        s["bzip3"] = b3 if b3 else 0
        for label, lvl in LEVELS:
            size, ct, dt, ok = kanzi(raw, lvl)
            assert ok, f"{p}: {label} round-trip"
            s[label] = size
            if lvl == "9":
                kt += ct

        class T:
            pass
        tt = T()
        tt.columns = t.columns
        tt.rows = rows
        tt.column = lambda j, _r=rows: [r[j] for r in _r]
        t0 = time.perf_counter()
        blob = fast.encode(tt)
        pt += time.perf_counter() - t0
        assert fast.decode(blob).rows == rows, f"{p}: polypress round-trip"
        s["polypress"] = len(blob)
        s["min(pp,knz)"] = min(s["polypress"], s["kanzi -l9"])
        if s["kanzi -l9"] < s["polypress"]:
            wins += 1

        n = os.path.basename(p).replace(".csv", "")[:21]
        print(f"{n:<22}{len(raw)/1e6:>8.1f}"
              + "".join(f"{s[k]:>13,}" for k in names)
              + f"{s['kanzi -l9']/s['polypress']:>9.3f}", flush=True)
        for k in names:
            tot[k] += s[k]

    print("-" * len(hdr))
    print(f"{'TOTAL':<22}{'':>8}" + "".join(f"{tot[k]:>13,}" for k in names)
          + f"{tot['kanzi -l9']/tot['polypress']:>9.3f}")
    print(f"{'vs xz':<22}{'':>8}"
          + "".join(f"{tot[k]/tot['xz -9e']:>13.3f}" for k in names))
    print(f"\nkanzi -l9 beats polypress on {wins} tables")
    print(f"min(pp,knz) is {1 - tot['min(pp,knz)']/tot['polypress']:.2%} "
          f"smaller than polypress alone")
    print(f"encode time: polypress {pt:.1f}s, kanzi -l9 {kt:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
