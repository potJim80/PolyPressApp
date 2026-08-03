"""Round-trip and robustness for the turbo fork.

Turbo makes decisions from samples and stores columns as formulas over other
columns, so the failure it can produce is not a crash -- it is a table that
decodes without error and is WRONG. Every check here compares cell for cell.

    python3 tests/test_turbo.py
"""

from __future__ import annotations

import os
import random
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polypress import dtz, turbo  # noqa: E402

FAILED = []


def check(name, table):
    try:
        blob = turbo.encode(table)
        back = turbo.decode(blob)
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"{name}: raised {type(exc).__name__}: {exc}")
        return
    if list(back.columns) != list(table.columns):
        FAILED.append(f"{name}: columns differ")
        return
    if back.rows != table.rows:
        n = sum(1 for a, b in zip(back.rows, table.rows) if a != b)
        FAILED.append(f"{name}: {n} of {len(table.rows)} rows differ")
        return
    print(f"  ok  {name:<44} {len(blob):>9,} B")


def t(cols, rows):
    return dtz.Table(list(cols), [list(r) for r in rows])


def main() -> int:
    print("edge cases")
    check("empty table", t([], []))
    check("no rows", t(["a", "b"], []))
    check("one row", t(["a", "b"], [["1", "x"]]))
    check("one column", t(["a"], [[str(i)] for i in range(500)]))
    check("all identical", t(["a", "b"], [["7", "z"]] * 400))
    check("empty strings", t(["a", "b"], [["", ""]] * 300))
    check("newline in cell", t(["a"], [["x\ny"], ["p"], ["q\nr"]] * 100))
    check("comma and quote", t(["a"], [['a,b'], ['"q"'], ["p"]] * 100))
    check("unicode", t(["a"], [["café"], ["日本語"], ["emoji 🎈"]] * 100))
    check("leading zeros", t(["a"], [["007"], ["0042"], ["1"]] * 200))
    check("negative zero", t(["a"], [["-0.0"], ["1.5"], ["2.5"]] * 200))
    check("ragged decimals", t(["a"], [["1.5"], ["1.50"], ["1.500"]] * 200))
    check("huge ints", t(["a"], [[str(2 ** 61)], [str(-2 ** 61)]] * 200))
    check("beyond int64", t(["a"], [[str(2 ** 70)], ["1"]] * 200))

    print("\nthe models, each on data built to trigger it")
    n = 3000
    # numeric cross-column prediction: nested aggregates
    base = [random.randint(1000, 90000) for _ in range(n)]
    check("nested aggregates", t(
        ["total", "sub1", "sub2"],
        [[str(b), str(b - random.randint(0, 40)), str(b - random.randint(0, 9))]
         for b in base]))
    # derived string column: concatenation
    lon = [f"-114.{random.randint(10**10, 10**11 - 1)}" for _ in range(n)]
    lat = [f"51.{random.randint(10**10, 10**11 - 1)}" for _ in range(n)]
    check("derived: WKT point", t(
        ["lon", "lat", "point"],
        [[a, b, f"POINT ({a} {b})"] for a, b in zip(lon, lat)]))
    check("derived: concatenated key", t(
        ["lon", "lat", "key"],
        [[a, b, a + "|" + b] for a, b in zip(lon, lat)]))
    # a formula that holds for only part of the table -- the exception path
    half = [[a, b, (f"POINT ({a} {b})" if i % 3 else f"junk{i}")]
            for i, (a, b) in enumerate(zip(lon, lat))]
    check("derived: 1/3 exceptions", t(["lon", "lat", "point"], half))
    # reorder parents
    zips = [f"{random.randint(10000, 10040)}" for _ in range(n)]
    city = {z: f"city_{z}" for z in set(zips)}
    check("reorder parent", t(["zip", "city"],
                              [[z, city[z]] for z in zips]))
    # numeric with a few unrepresentable cells
    vals = [f"{i * 3}" for i in range(n)]
    for i in range(0, n, 700):
        vals[i] = ""
    check("numeric with blanks", t(["a", "b"],
                                   [[v, str(i)] for i, v in enumerate(vals)]))

    print("\nadversarial corpus")
    hostile = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "corpus_hostile")
    if os.path.isdir(hostile):
        for f in sorted(os.listdir(hostile)):
            if not f.endswith(".csv"):
                continue
            try:
                tab = dtz.read_any(os.path.join(hostile, f))
            except Exception as exc:  # noqa: BLE001
                print(f"  skip {f}: {exc}")
                continue
            check(f, tab)
            del tab
    else:
        print("  (corpus_hostile not present)")

    print("\nrandom fuzz")
    rnd = random.Random(20260803)
    for i in range(40):
        ncol = rnd.randint(1, 8)
        nrow = rnd.randint(0, 400)
        alpha = string.printable[:60] + "é日\n,\""
        rows = [[("".join(rnd.choice(alpha) for _ in range(rnd.randint(0, 12)))
                  if rnd.random() < 0.5
                  else str(rnd.randint(-10 ** 12, 10 ** 12)))
                 for _ in range(ncol)] for _ in range(nrow)]
        check(f"fuzz {i} ({nrow}x{ncol})", t([f"c{j}" for j in range(ncol)],
                                             rows))

    print()
    if FAILED:
        print(f"*** {len(FAILED)} FAILURES ***")
        for f in FAILED:
            print("   " + f)
        return 1
    print("all turbo round-trips exact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
