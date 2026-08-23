#!/usr/bin/env python3
"""TEST-8 addendum -- the straight comparison: variant E against plain xz -9e,
bytes AND wall clock, encode and decode. Run from work/:

    python3 ../memory/tests/TEST-8/time_vs_xz.py
"""
import csv
import glob
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import inlinedict as I

MAXROWS = int(os.environ.get("T8_ROWS", "40000"))


def main():
    from polypress import dtz
    paths = []
    for pat in (sys.argv[1:] or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    cols = ("table", "input MB", "xz B", "E B", "E/xz",
            "xz enc s", "E enc s", "enc x", "xz dec s", "E dec s", "dec x")
    w = [24, 9, 11, 11, 7, 9, 9, 7, 9, 9, 7]
    hdr = "".join(c.rjust(x) if i else c.ljust(x)
                  for i, (c, x) in enumerate(zip(cols, w)))
    print(f"# TEST-8 addendum -- variant E vs plain xz -9e, {MAXROWS:,} rows max")
    print(hdr)
    print("-" * len(hdr))

    T = dict(raw=0, xz=0, e=0, xe=0, ee=0, xd=0, ed=0)
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
        cw = csv.writer(buf, lineterminator="\n")
        for r in rows:
            cw.writerow(r)
        raw = buf.getvalue().encode()

        t0 = time.perf_counter(); xzb = I.xz(raw); xe = time.perf_counter() - t0
        t0 = time.perf_counter(); back = I.unxz(xzb); xd = time.perf_counter() - t0
        assert back == raw
        t0 = time.perf_counter(); blob = I.encode_E(rows); ee = time.perf_counter() - t0
        t0 = time.perf_counter(); got = I.decode_E(blob); ed = time.perf_counter() - t0
        assert got == rows, p

        n = os.path.basename(p).replace(".csv", "")[:23]
        print(f"{n:<24}{len(raw)/1e6:>9.1f}{len(xzb):>11,}{len(blob):>11,}"
              f"{len(blob)/len(xzb):>7.3f}{xe:>9.2f}{ee:>9.2f}{ee/xe:>7.2f}"
              f"{xd:>9.2f}{ed:>9.2f}{ed/xd:>7.2f}", flush=True)
        T['raw'] += len(raw); T['xz'] += len(xzb); T['e'] += len(blob)
        T['xe'] += xe; T['ee'] += ee; T['xd'] += xd; T['ed'] += ed

    print("-" * len(hdr))
    print(f"{'TOTAL':<24}{T['raw']/1e6:>9.1f}{T['xz']:>11,}{T['e']:>11,}"
          f"{T['e']/T['xz']:>7.3f}{T['xe']:>9.2f}{T['ee']:>9.2f}"
          f"{T['ee']/T['xe']:>7.2f}{T['xd']:>9.2f}{T['ed']:>9.2f}"
          f"{T['ed']/T['xd']:>7.2f}")
    print(f"\nthroughput  xz encode {T['raw']/1e6/T['xe']:.2f} MB/s   "
          f"E encode {T['raw']/1e6/T['ee']:.2f} MB/s")
    print(f"            xz decode {T['raw']/1e6/T['xd']:.2f} MB/s   "
          f"E decode {T['raw']/1e6/T['ed']:.2f} MB/s")


if __name__ == "__main__":
    main()
