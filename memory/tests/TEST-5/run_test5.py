#!/usr/bin/env python3
"""Drive TEST-5. Run from work/: python3 ../memory/tests/TEST-5/run_test5.py"""
import glob, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import rowdict as R
from polypress import dtz, fast

MAXROWS = int(os.environ.get("T5_ROWS", "60000"))

paths = []
for pat in (sys.argv[1:] or ["../IN/corpus/*.csv"]):
    paths.extend(sorted(glob.glob(pat)))

print("# TEST-5  row-major number-encoding vs column-major vs polypress")
print(f"# rows capped at {MAXROWS:,}. All variants round-trip verified.\n")
hdr = (f"{'table':<24}{'rows':>7}{'cols':>5}{'raw':>11}{'xz raw':>10}"
       f"{'B row':>10}{'C col':>10}{'polypress':>11}{'B/xz':>7}{'B/C':>7}{'B/pp':>7}")
print(hdr); print("-" * len(hdr))
tot = dict(raw=0, xz=0, b=0, c=0, pp=0)
for p in paths:
    try:
        t = dtz.read_any(p)
    except Exception as e:
        print(f"{os.path.basename(p)[:23]:<24} SKIP {type(e).__name__}"); continue
    rows = t.rows[:MAXROWS]
    if not rows: continue
    ncol = len(t.columns)
    rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol] for r in rows]
    # The encoded body only ever holds digits, sign, dot, e/E and separators,
    # because every non-numeric cell becomes an id -- so comma/newline in the
    # ORIGINAL data cannot leak into it. The baseline, though, must be a real
    # CSV, so it is written with csv.writer and quoted properly.
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows: w.writerow(r)
    raw = buf.getvalue().encode()

    b = R.encode_row(rows)
    assert R.decode_row(b) == rows, f"{p}: row round-trip"
    c = R.encode_col(rows)
    assert R.decode_col(c) == rows, f"{p}: col round-trip"
    xzr = len(R.xz(raw))

    class T: pass
    tt = T(); tt.columns = t.columns; tt.rows = rows
    tt.column = lambda j, _r=rows: [r[j] for r in _r]
    pp = len(fast.encode(tt))

    n = os.path.basename(p).replace(".csv", "")[:23]
    print(f"{n:<24}{len(rows):>7}{ncol:>5}{len(raw):>11,}{xzr:>10,}"
          f"{len(b):>10,}{len(c):>10,}{pp:>11,}"
          f"{len(b)/xzr:>7.2f}{len(b)/len(c):>7.2f}{len(b)/pp:>7.2f}", flush=True)
    tot['raw'] += len(raw); tot['xz'] += xzr
    tot['b'] += len(b); tot['c'] += len(c); tot['pp'] += pp

print("-" * len(hdr))
print(f"{'TOTAL':<24}{'':>7}{'':>5}{tot['raw']:>11,}{tot['xz']:>10,}"
      f"{tot['b']:>10,}{tot['c']:>10,}{tot['pp']:>11,}"
      f"{tot['b']/tot['xz']:>7.2f}{tot['b']/tot['c']:>7.2f}{tot['b']/tot['pp']:>7.2f}")
