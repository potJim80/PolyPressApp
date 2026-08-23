#!/usr/bin/env python3
"""Drive TEST-8.  Run from work/:  python3 ../memory/tests/TEST-8/run_test8.py

  python3 ../memory/tests/TEST-8/run_test8.py --selftest      # correctness only
  python3 ../memory/tests/TEST-8/run_test8.py '../IN/corpus/*.csv'
"""
import csv
import glob
import io
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import inlinedict as I

MAXROWS = int(os.environ.get("T8_ROWS", "40000"))

VARIANTS = [
    ("B out-of-band", I.encode_B, I.decode_B),
    ("C inline", I.encode_C, I.decode_C),
    ("D 1-pass", I.encode_D, I.decode_D),
    ("E 1-pass gate", I.encode_E, I.decode_E),
    ("F inline col", I.encode_F, I.decode_F),
]


# --------------------------------------------------------------------------
# correctness first: values chosen to break the escaping, not to compress
# --------------------------------------------------------------------------

def selftest() -> None:
    rnd = random.Random(7)
    nasty = ["", "0", "42", "007", "abc", "a,b", 'q"q', "x\x01y", "p\x02p",
             "l\nl", "\x01", "\x02", "\n", "\x01\x02b", "1\x022", "-3.5",
             "1e9", ".5", "+7", "NaN", "  ", "é", "\x01\x01\x01"]
    tables = []

    # every nasty value in a dictionary column, twice, so ids are exercised
    tables.append([[v, "1", v] for v in nasty] * 2)
    # numeric column that turns non-numeric late (the exception path)
    tables.append([["1", "a"], ["2", "b"], ["x\x02y", "a"], ["4", "42"]] * 30)
    # first cell numeric, rest not
    tables.append([["0"]] + [["z"]] * 10)
    # single empty cell
    tables.append([[""]])
    # high-cardinality column, to trip E's gate
    tables.append([[f"u{i}", "k", str(i % 3)] for i in range(400)])
    # random mixtures
    for _ in range(12):
        ncol = rnd.randint(1, 5)
        nrow = rnd.randint(1, 200)
        tables.append([[rnd.choice(nasty) for _ in range(ncol)]
                       for _ in range(nrow)])

    for ti, rows in enumerate(tables):
        for name, enc, dec in VARIANTS:
            blob = enc(rows)
            got = dec(blob)
            if got != rows:
                for i, (a, b) in enumerate(zip(got, rows)):
                    if a != b:
                        raise AssertionError(
                            f"table {ti} variant {name} row {i}: {a!r} != {b!r}")
                raise AssertionError(
                    f"table {ti} variant {name}: {len(got)} rows != {len(rows)}")
    print(f"selftest OK -- {len(tables)} adversarial tables x "
          f"{len(VARIANTS)} variants, all round-trip exact")


# --------------------------------------------------------------------------

def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    selftest()
    if "--selftest" in sys.argv:
        return

    from polypress import dtz

    paths = []
    for pat in (args or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    print(f"\n# TEST-8  inline dictionary vs out-of-band dictionary")
    print(f"# rows capped at {MAXROWS:,}. Every variant decoded and compared "
          f"cell by cell before its bytes were counted.\n")
    cols = ("table", "rows", "cols", "xz raw", "B out-band", "C inline",
            "D 1pass", "E 1p+gate", "F col", "C/B", "C/xz", "E/xz")
    w = [24, 7, 5, 11, 11, 11, 11, 11, 11, 7, 7, 7]
    hdr = "".join(c.rjust(x) if i else c.ljust(x)
                  for i, (c, x) in enumerate(zip(cols, w)))
    print(hdr)
    print("-" * len(hdr))

    tot = dict(xz=0, B=0, C=0, D=0, E=0, F=0)
    times = {k: 0.0 for k in ("B", "C", "D", "E", "F")}
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
        xzr = len(I.xz(buf.getvalue().encode()))

        size = {}
        for key, (name, enc, dec) in zip("BCDEF", VARIANTS):
            t0 = time.perf_counter()
            blob = enc(rows)
            times[key] += time.perf_counter() - t0
            assert dec(blob) == rows, f"{p}: {name} round-trip"
            size[key] = len(blob)

        n = os.path.basename(p).replace(".csv", "")[:23]
        print(f"{n:<24}{len(rows):>7}{ncol:>5}{xzr:>11,}"
              f"{size['B']:>11,}{size['C']:>11,}{size['D']:>11,}"
              f"{size['E']:>11,}{size['F']:>11,}"
              f"{size['C']/size['B']:>7.3f}{size['C']/xzr:>7.3f}"
              f"{size['E']/xzr:>7.3f}", flush=True)
        tot['xz'] += xzr
        for k in "BCDEF":
            tot[k] += size[k]

    print("-" * len(hdr))
    print(f"{'TOTAL':<24}{'':>7}{'':>5}{tot['xz']:>11,}"
          f"{tot['B']:>11,}{tot['C']:>11,}{tot['D']:>11,}"
          f"{tot['E']:>11,}{tot['F']:>11,}"
          f"{tot['C']/tot['B']:>7.3f}{tot['C']/tot['xz']:>7.3f}"
          f"{tot['E']/tot['xz']:>7.3f}")
    print("\nencode time, seconds over the whole corpus")
    for k in "BCDEF":
        print(f"  {k}  {times[k]:8.1f}   {times[k]/times['B']:5.2f}x of B")


if __name__ == "__main__":
    main()
