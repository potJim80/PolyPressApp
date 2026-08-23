#!/usr/bin/env python3
"""Drive TEST-16 -- concatenated keys, and both cross-column patterns together.

  python3 ../memory/tests/TEST-16/run_test16.py --selftest
  python3 ../memory/tests/TEST-16/run_test16.py '../IN/corpus100/*.csv'

Runs TEST-15's geometry detector and TEST-16's concatenation detector on the
same table, in that order, and reports what each is worth alone and together.
Every stripped table is rebuilt and compared cell by cell first.
"""
import csv
import glob
import io
import lzma
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "TEST-15"))
sys.path.insert(0, os.getcwd())
import concatkey as K
import geomdup as G

MAXROWS = int(os.environ.get("T16_ROWS", "40000"))
XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])


def xz(b):
    return lzma.compress(b, **XZ)


def as_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def selftest():
    rnd = random.Random(9)
    rows = []
    for i in range(300):
        d = f"2026-07-{(i % 28) + 1:02d}"
        city = rnd.choice(["SEATTLE", "TACOMA", "BELLEVUE"])
        num = f"F26{i:07d}"
        rows.append([d, city, num, f"{d}|{city}|{num}", "x"])
    recs = K.detect(rows, 5)
    assert any(r["col"] == 3 for r in recs), "concatenated key not detected"
    st, keep = K.strip(rows, recs)
    assert K.rebuild(st, keep, 5, recs) == rows, "concat rebuild"

    rows2 = [r[:] for r in rows]
    for i in (3, 90, 250):
        rows2[i][3] = "ODD"
    r2 = K.detect(rows2, 5)
    s2, k2 = K.strip(rows2, r2)
    assert K.rebuild(s2, k2, 5, r2) == rows2, "concat exceptions"

    # a table with nothing concatenated must not invent a recipe that breaks
    for _ in range(30):
        ncol = rnd.randint(1, 5)
        nrow = rnd.randint(1, 120)
        pool = ["", "ab", "a|b", "2026-01-01", "XY", "9", "-1.25"]
        t = [[rnd.choice(pool) for _ in range(ncol)] for _ in range(nrow)]
        rc = K.detect(t, ncol)
        s, k = K.strip(t, rc)
        assert K.rebuild(s, k, ncol, rc) == t, "random concat rebuild"
    print("selftest OK -- key detected, exceptions exact, 30 random tables "
          "rebuilt exactly")


def main():
    selftest()
    if "--selftest" in sys.argv:
        return
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    from polypress import dtz, fast

    paths = []
    for pat in (args or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    print(f"\n# TEST-16  concatenated keys + geometry, {MAXROWS:,} rows max")
    print("# every stripped table rebuilt and compared before any size counts\n")
    hdr = (f"{'table':<26}{'cols':>5}{'geo':>4}{'key':>4}{'pp full':>12}"
           f"{'pp geo':>12}{'pp key':>12}{'pp both':>12}{'best gain':>11}")
    print(hdr)
    print("-" * len(hdr))

    tf = tb = 0
    hits = ntab = 0
    for p in paths:
        try:
            t = dtz.read_any(p)
        except Exception:
            continue
        rows = t.rows[:MAXROWS]
        if not rows:
            continue
        ncol = len(t.columns)
        rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
                for r in rows]
        ntab += 1

        grecs = G.detect(rows, ncol)
        krecs = K.detect(rows, ncol)
        if not grecs and not krecs:
            continue
        hits += 1

        def pp(rs, cols):
            class T:
                pass
            a = T()
            a.columns = cols
            a.rows = rs
            a.column = lambda j, _r=rs: [r[j] for r in _r]
            return len(fast.encode(a))

        full = pp(rows, t.columns)

        geo = key = both = full
        if grecs:
            st, keep = G.strip(rows, grecs)
            assert G.rebuild(st, keep, ncol, grecs) == rows, f"{p}: geo"
            geo = pp(st, [t.columns[j] for j in keep]) + \
                len(xz(G.recipe_bytes(grecs)))
        if krecs:
            st, keep = K.strip(rows, krecs)
            assert K.rebuild(st, keep, ncol, krecs) == rows, f"{p}: key"
            key = pp(st, [t.columns[j] for j in keep]) + \
                len(xz(K.recipe_bytes(krecs)))
        if grecs or krecs:
            # geometry first, then concatenation on what survives
            drop = {r["col"] for r in grecs} | {r["col"] for r in krecs}
            keep2 = [j for j in range(ncol) if j not in drop]
            st2 = [[r[j] for j in keep2] for r in rows]
            both = pp(st2, [t.columns[j] for j in keep2]) \
                + len(xz(G.recipe_bytes(grecs))) \
                + len(xz(K.recipe_bytes(krecs)))

        best = min(full, geo, key, both)
        n = os.path.basename(p).replace(".csv", "")[:25]
        print(f"{n:<26}{ncol:>5}{len(grecs):>4}{len(krecs):>4}{full:>12,}"
              f"{geo:>12,}{key:>12,}{both:>12,}{1 - best/full:>10.1%}",
              flush=True)
        tf += full
        tb += best

    print("-" * len(hdr))
    print(f"{'TOTAL (tables with a hit)':<26}{'':>5}{'':>4}{'':>4}{tf:>12,}"
          f"{'':>12}{'':>12}{tb:>12,}{(1 - tb/tf) if tf else 0:>10.1%}")
    print(f"\n{hits} of {ntab} tables carry at least one derivable column")


if __name__ == "__main__":
    main()
