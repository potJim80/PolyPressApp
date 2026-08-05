#!/usr/bin/env python3
"""Aggregate the archive sweep and the database sweep into one table.

    python3 stridexz/report_sql.py sweep.jsonl sql.jsonl

Totals are taken only over tables where every method produced a size, and any
table dropped for that reason is named. A silent exclusion reads as coverage
that was never there.
"""
import json
import sys

ARCHIVE = ["POLYPRESS", "stridexz BEST", "xz -9e", "brotli -q11", "parquet+zstd22"]
DATABASE = ["duckdb", "sqlite", "sqlite VACUUM", "sqlite+xz"]
QUERYABLE = {"duckdb", "sqlite", "sqlite VACUUM"}


def main():
    sw = {json.loads(l)["file"]: json.loads(l) for l in open(sys.argv[1]) if l.strip()}
    sq = {json.loads(l)["file"]: json.loads(l) for l in open(sys.argv[2]) if l.strip()}
    methods = ARCHIVE + DATABASE

    def cell(f, m):
        src = sw[f]["results"] if m in sw[f]["results"] else sq[f]["results"]
        return src.get(m, {})

    allf = [f for f in sw if f in sq]
    good = [f for f in allf
            if all(isinstance(cell(f, m).get("bytes"), int) for m in methods)]

    for f in allf:
        for m in methods:
            c = cell(f, m)
            if not isinstance(c.get("bytes"), int):
                print(f"EXCLUDED  {f}\n          {m}: {c.get('error', 'no result')}\n")
            elif not c.get("ok"):
                print(f"ROUND-TRIP FAILURE  {f}  {m}\n")

    csv = sum(sw[f]["csv"] for f in good)
    tot = {m: sum(cell(f, m)["bytes"] for f in good) for m in methods}
    sec = {m: sum(cell(f, m)["secs"] for f in good) for m in methods}
    pp = tot["POLYPRESS"]

    print(f"{len(good)} of {len(allf)} tables carry every method. "
          f"{csv / 1e6:.0f} MB of CSV.\n")
    print(f"{'method':<20}{'total bytes':>14}{'vs CSV':>9}{'vs PP':>8}"
          f"{'secs':>8}  queryable")
    for m in sorted(tot, key=lambda m: tot[m]):
        print(f"{m:<20}{tot[m]:>14,}{csv / tot[m]:>8.2f}x{pp / tot[m]:>7.3f}x"
              f"{sec[m]:>7.0f}s  {'YES' if m in QUERYABLE else 'no'}")

    print()
    def wins(a, b):
        return sum(1 for f in good if cell(f, a)["bytes"] < cell(f, b)["bytes"])
    for a, b in [("POLYPRESS", "duckdb"), ("stridexz BEST", "duckdb"),
                 ("duckdb", "parquet+zstd22"), ("duckdb", "xz -9e"),
                 ("sqlite+xz", "POLYPRESS")]:
        print(f"   {a} smaller than {b} on {wins(a, b)}/{len(good)}")

    big = [f for f in good if cell(f, "sqlite VACUUM")["bytes"] > sw[f]["csv"]]
    print(f"\n   sqlite file LARGER than its source CSV on {len(big)}/{len(good)}")
    ratios = sorted(cell(f, "duckdb")["bytes"] / cell(f, "POLYPRESS")["bytes"]
                    for f in good)
    print(f"   duckdb is {ratios[len(ratios) // 2]:.2f}x bigger than Polypress "
          f"at the median, {ratios[0]:.2f}x at best, {ratios[-1]:.2f}x at worst")

    print(f"\n{'table':<50}{'PP':>10}{'stridexz':>10}{'duckdb':>12}{'sqlite':>12}")
    for f in sorted(good):
        print(f"{f[:48]:<50}{cell(f, 'POLYPRESS')['bytes']:>10,}"
              f"{cell(f, 'stridexz BEST')['bytes']:>10,}"
              f"{cell(f, 'duckdb')['bytes']:>12,}"
              f"{cell(f, 'sqlite VACUUM')['bytes']:>12,}")


if __name__ == "__main__":
    main()
