#!/usr/bin/env python3
"""Drive TEST-2: 200 real single columns, xz against ANS. Run from work/."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harvest import harvest, KINDS          # noqa: E402
from test2 import measure, VARIANTS         # noqa: E402

TARGET = int(os.environ.get("TEST2_N", "200"))
PATTERN = os.environ.get("TEST2_GLOB", "../IN/corpus500/*.csv")

HDR = (f"{'column':<46}{'kind':<12}{'n':>8}{'k':>8}"
       f"{'xz':>10}{'ans0':>10}{'ans1':>10}{'D-xz':>10}{'D-ans':>10}"
       f"{'best/xz':>9}")


def line(r):
    best = min(r[v] for v in VARIANTS if v != "xz")
    return (f"{r['name'][:45]:<46}{r['kind']:<12}{r['n']:>8}{r['k']:>8}"
            f"{r['xz']:>10}{r['ans0']:>10}{r['ans1']:>10}"
            f"{r['D-xz']:>10}{r['D-ans']:>10}{best / r['xz']:>9.3f}")


def table(rows, title, group_key):
    groups = {}
    for r in rows:
        groups.setdefault(group_key(r), []).append(r)
    print(f"\n{title}")
    head = (f"  {'group':<14}{'cols':>5}{'raw MB':>9}" +
            "".join(f"{v:>12}" for v in VARIANTS) +
            f"{'ANS/xz':>9}{'ANS wins':>10}")
    print(head)
    print("  " + "-" * (len(head) - 2))
    for g in sorted(groups, key=lambda g: -sum(x["raw"] for x in groups[g])):
        rs = groups[g]
        tot = {v: sum(r[v] for r in rs) for v in VARIANTS}
        bestans = sum(min(r["ans0"], r["ans1"]) for r in rs)
        wins = sum(1 for r in rs if min(r["ans0"], r["ans1"]) < r["xz"])
        print(f"  {g:<14}{len(rs):>5}{sum(r['raw'] for r in rs) / 1e6:>9.1f}" +
              "".join(f"{tot[v]:>12,}" for v in VARIANTS) +
              f"{bestans / tot['xz']:>9.2f}{wins:>7}/{len(rs):<3}")


def main():
    t0 = time.time()
    sys.stderr.write(f"harvesting {TARGET} columns from {PATTERN}\n")
    cols = harvest(PATTERN, TARGET)
    sys.stderr.write(f"harvested {len(cols)} columns in {time.time() - t0:.0f}s\n")

    print(f"# TEST-2  {len(cols)} real single columns from {PATTERN}")
    print(f"# case A = xz -9e on the column text; case B = static rANS on the "
          f"same bytes")
    print(f"# D-* = first-appearance dictionary + byte-plane codes, finished "
          f"with xz or rANS")
    print(f"# every size is a whole container; every variant was decoded and "
          f"compared before counting")
    print()
    print(HDR)
    print("-" * len(HDR))

    rows = []
    for name, kind, vals in cols:
        try:
            r = measure(name, kind, vals)
        except AssertionError as e:
            print(f"{name[:45]:<46}{kind:<12}  ROUND-TRIP FAILED: {e}",
                  flush=True)
            raise
        except Exception as e:
            print(f"{name[:45]:<46}{kind:<12}  SKIP {type(e).__name__}: {e}",
                  flush=True)
            continue
        rows.append(r)
        print(line(r), flush=True)

    print(f"\n{len(rows)} columns, all round-trip verified, "
          f"{time.time() - t0:.0f}s")

    table(rows, "BY KIND", lambda r: r["kind"])

    def band(r):
        f = r["k"] / r["n"]
        return ("k/n <0.001" if f < 0.001 else "k/n <0.01" if f < 0.01 else
                "k/n <0.1" if f < 0.1 else "k/n <0.5" if f < 0.5 else
                "k/n >=0.5")
    table(rows, "BY CARDINALITY", band)
    table(rows, "ALL", lambda r: "all")

    print("\nHOW CLOSE TO THE ORDER-0 FLOOR (median size / floor)")
    import statistics as st
    for k in KINDS + ("ALL",):
        rs = [r for r in rows if k == "ALL" or r["kind"] == k]
        rs = [r for r in rs if r["floor"] > 0]
        if not rs:
            continue
        med = {v: st.median(r[v] / r["floor"] for r in rs) for v in VARIANTS}
        print(f"  {k:<14}" + "".join(f"{v}={med[v]:.2f}  " for v in VARIANTS))

    print("\nWHERE ANS (order 0 or 1) BEATS xz OUTRIGHT")
    won = [r for r in rows if min(r["ans0"], r["ans1"]) < r["xz"]]
    for r in sorted(won, key=lambda r: min(r["ans0"], r["ans1"]) / r["xz"])[:25]:
        print(f"  {r['name'][:45]:<46}{r['kind']:<12}"
              f"ANS/xz {min(r['ans0'], r['ans1']) / r['xz']:.3f}  "
              f"k/n {r['k'] / r['n']:.4f}")
    print(f"  ({len(won)} of {len(rows)})")

    print("\nWHERE xz BEATS ANS WORST")
    for r in sorted(rows, key=lambda r: -min(r["ans0"], r["ans1"]) / r["xz"])[:15]:
        print(f"  {r['name'][:45]:<46}{r['kind']:<12}"
              f"ANS/xz {min(r['ans0'], r['ans1']) / r['xz']:.2f}  "
              f"k/n {r['k'] / r['n']:.4f}")

    print("\nD-ans vs D-xz -- same model, different coder")
    dans = sum(r["D-ans"] for r in rows)
    dxz = sum(r["D-xz"] for r in rows)
    w = sum(1 for r in rows if r["D-ans"] < r["D-xz"])
    print(f"  D-ans {dans:,}  D-xz {dxz:,}  ratio {dans / dxz:.3f}  "
          f"D-ans wins {w}/{len(rows)}")
    best = sum(min(r[v] for v in VARIANTS) for r in rows)
    axz = sum(r["xz"] for r in rows)
    print(f"  best-of-all {best:,} vs xz {axz:,}  ratio {best / axz:.3f}")


if __name__ == "__main__":
    main()
