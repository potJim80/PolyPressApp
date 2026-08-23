#!/usr/bin/env python3
"""What is the context mixer exploiting? Order-0 and order-1 entropy floors of
each gap-carrying column, as VALUES (not bytes), against what xz / polypress /
the pure CM actually achieve.

H0 = n*H(p) bits, the order-0 floor: what any dictionary+static coder can reach.
H1 = the conditional entropy of a value given the previous ROW's value in the
     same column, plus the cost of the transition table (charged, at
     (#distinct pairs) * (2*log2(k)+8) bits, deliberately crude and generous).
"""
from __future__ import annotations
import collections, lzma, math, os, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.getcwd())

SCRATCH = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
           "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/floats/tmp")
KANZI = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
         "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/kanzi-cpp/bin/"
         "kanzi_dynamic")


def xz(b):
    return len(lzma.compress(b, format=lzma.FORMAT_RAW, filters=[
        {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])) if b else 0


def cm(data):
    os.makedirs(SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        src = os.path.join(d, "in"); arc = os.path.join(d, "o.knz")
        open(src, "wb").write(data)
        r = subprocess.run([KANZI, "-c", "-i", src, "-o", arc, "-b", "16m",
                            "-j", "1", "-f", "-t", "None", "-e", "TPAQX"],
                           capture_output=True)
        return os.path.getsize(arc) if r.returncode == 0 else -1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def h0(vals):
    c = collections.Counter(vals); n = len(vals)
    return sum(-k * math.log2(k / n) for k in c.values()) / 8


def h1(vals):
    n = len(vals)
    ctx = collections.defaultdict(collections.Counter)
    for a, b in zip(vals, vals[1:]):
        ctx[a][b] += 1
    bits = 0.0
    pairs = 0
    for a, c in ctx.items():
        m = sum(c.values())
        pairs += len(c)
        bits += sum(-k * math.log2(k / m) for k in c.values())
    k = len(set(vals))
    table = pairs * (2 * max(1, math.ceil(math.log2(max(k, 2)))) + 8)
    return bits / 8, (bits + table) / 8


def main():
    from polypress import dtz, fast
    for path, want in [
        ("../IN/corpus/usgs_quakes.csv",
         ["place", "magError", "time", "latitude", "longitude", "depthError",
          "dmin", "magType", "id"]),
        ("../IN/corpus/austin_incidents.csv",
         ["ucr_code", "census_block_group", "occ_time", "crime_type",
          "rep_date_time", "rep_time", "occ_date_time", "district",
          "location_type"]),
    ]:
        t = dtz.read_any(path)
        n = len(t.columns)
        rows = [r + [""] * (n - len(r)) if len(r) < n else r[:n]
                for r in t.rows[:40000]]
        print(f"\n=== {os.path.basename(path)}  ({len(rows):,} rows)")
        hdr = (f"{'column':<22}{'k':>7}{'H0':>10}{'H1':>10}{'H1+tab':>10}"
               f"{'xz':>10}{'pp':>10}{'CM':>10}{'CM/H0':>7}{'CM/H1':>7}")
        print(hdr); print("-" * len(hdr))
        for j, c in enumerate(t.columns):
            if c not in want:
                continue
            v = [r[j] for r in rows]
            b = ("\n".join(v) + "\n").encode()

            class T:
                pass
            tt = T(); tt.columns = [c]; tt.rows = [[x] for x in v]
            tt.column = lambda k, _r=v: _r
            p = len(fast.encode(tt))
            a, at = h1(v)
            e0 = h0(v)
            k = len(set(v))
            s = cm(b)
            print(f"{c[:21]:<22}{k:>7,}{e0:>10,.0f}{a:>10,.0f}{at:>10,.0f}"
                  f"{xz(b):>10,}{p:>10,}{s:>10,}{s/e0:>7.2f}"
                  f"{s/max(at,1):>7.2f}", flush=True)


main()
