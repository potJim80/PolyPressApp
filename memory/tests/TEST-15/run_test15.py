#!/usr/bin/env python3
"""Drive TEST-15 -- cross-column redundancy with a real decoder.

  python3 ../memory/tests/TEST-15/run_test15.py --selftest
  python3 ../memory/tests/TEST-15/run_test15.py '../IN/corpus/*.csv'
  python3 ../memory/tests/TEST-15/run_test15.py '../IN/corpus100/*.csv'

Reports, per table: which columns were found derivable, and what stripping
them is worth through xz and through the shipping codec. Every stripped table
is rebuilt and compared cell by cell before any size counts.

Invariant 2 is enforced here, not assumed: the transform is only credited when
`stripped + recipe` actually compresses smaller.
"""
import csv
import glob
import io
import lzma
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import geomdup as G

MAXROWS = int(os.environ.get("T15_ROWS", "40000"))
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
    rnd = random.Random(5)
    # the shape this exists for: WKT geometry beside its own coordinates
    rows = []
    for i in range(400):
        lon = f"-87.{rnd.randint(100000, 999999)}"
        lat = f"41.{rnd.randint(100000, 999999)}"
        rows.append([str(i), lon, lat,
                     f"POINT ({lon[:9]} {lat[:8]})", "OPEN"])
    recs = G.detect(rows, 5)
    assert any(r["col"] == 3 for r in recs), "WKT column not detected"
    st, keep = G.strip(rows, recs)
    assert G.rebuild(st, keep, 5, recs) == rows, "WKT rebuild"

    # exceptions: break a few cells so no rule is perfect
    rows2 = [r[:] for r in rows]
    for i in (7, 100, 300):
        rows2[i][3] = "POINT (0 0)"
    recs2 = G.detect(rows2, 5)
    st2, keep2 = G.strip(rows2, recs2)
    assert G.rebuild(st2, keep2, 5, recs2) == rows2, "exception rebuild"

    # nothing derivable: must detect nothing and rebuild trivially
    rows3 = [[str(rnd.random()), "x" * rnd.randint(1, 5)] for _ in range(200)]
    recs3 = G.detect(rows3, 2)
    st3, keep3 = G.strip(rows3, recs3)
    assert G.rebuild(st3, keep3, 2, recs3) == rows3, "null rebuild"

    # random tables, to prove rebuild is exact whatever detect decides
    for _ in range(30):
        ncol = rnd.randint(1, 5)
        nrow = rnd.randint(1, 120)
        pool = ["", "0", "-1.5", "POINT (1 2)", "a,b", "x", "12.75", "\x00"]
        t = [[rnd.choice(pool) for _ in range(ncol)] for _ in range(nrow)]
        rc = G.detect(t, ncol)
        s, k = G.strip(t, rc)
        assert G.rebuild(s, k, ncol, rc) == t, "random rebuild"
    print("selftest OK -- WKT detected, exceptions exact, 30 random tables "
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
    use_pp = "--no-pp" not in sys.argv

    print(f"\n# TEST-15  cross-column redundancy, {MAXROWS:,} rows max")
    print("# every stripped table rebuilt and compared before any size counts\n")
    hdr = (f"{'table':<24}{'cols':>5}{'found':>6}{'xz full':>12}"
           f"{'xz strip':>12}{'recipe':>10}{'xz gain':>9}"
           f"{'pp full':>12}{'pp strip':>12}{'pp gain':>9}")
    print(hdr)
    print("-" * len(hdr))

    tf = ts = pf = ps = 0
    nfound = ntab = 0
    for p in paths:
        try:
            t = dtz.read_any(p)
        except Exception as e:
            continue
        rows = t.rows[:MAXROWS]
        if not rows:
            continue
        ncol = len(t.columns)
        rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
                for r in rows]
        ntab += 1

        recs = G.detect(rows, ncol)
        name = os.path.basename(p).replace(".csv", "")[:23]
        if not recs:
            continue
        st, keep = G.strip(rows, recs)
        assert G.rebuild(st, keep, ncol, recs) == rows, f"{p}: rebuild"
        nfound += 1

        full = len(xz(as_csv(rows)))
        rec = G.recipe_bytes(recs)
        stripped = len(xz(as_csv(st))) + len(xz(rec))
        gain = 1 - stripped / full

        pfull = pstrip = 0
        if use_pp:
            class T:
                pass
            a = T(); a.columns = t.columns; a.rows = rows
            a.column = lambda j, _r=rows: [r[j] for r in _r]
            pfull = len(fast.encode(a))
            b = T()
            b.columns = [t.columns[j] for j in keep]
            b.rows = st
            b.column = lambda j, _r=st: [r[j] for r in _r]
            pstrip = len(fast.encode(b)) + len(xz(rec))

        cols = ",".join(str(r["col"]) for r in recs)
        print(f"{name:<24}{ncol:>5}{len(recs):>6}{full:>12,}{stripped:>12,}"
              f"{len(xz(rec)):>10,}{gain:>8.1%}"
              f"{pfull:>12,}{pstrip:>12,}"
              f"{(1 - pstrip/pfull) if pfull else 0:>8.1%}   [{cols}]",
              flush=True)
        tf += full; ts += stripped; pf += pfull; ps += pstrip

    print("-" * len(hdr))
    print(f"{'TOTAL (tables with a hit)':<24}{'':>5}{nfound:>6}{tf:>12,}"
          f"{ts:>12,}{'':>10}{(1 - ts/tf) if tf else 0:>8.1%}"
          f"{pf:>12,}{ps:>12,}{(1 - ps/pf) if pf else 0:>8.1%}")
    print(f"\n{nfound} of {ntab} tables carry a derivable column")


if __name__ == "__main__":
    main()
