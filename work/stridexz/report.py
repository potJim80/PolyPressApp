#!/usr/bin/env python3
"""Aggregate an stridexz sweep .jsonl into the claims.

    python3 stridexz/report.py sweep.jsonl
"""
import json
import sys


def main():
    rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
    if not rows:
        print("no results")
        return
    methods = list(rows[0]["results"])

    print(f"{len(rows)} tables, {sum(r['csv'] for r in rows) / 1e6:.0f} MB of CSV "
          f"(each capped at a row boundary)\n")

    broken = [(r["file"], m) for r in rows for m in methods
              if not r["results"][m]["ok"]]
    if broken:
        print("ROUND-TRIP FAILURES:")
        for f, m in broken:
            print(f"   {f}  {m}")
        print()
    else:
        print(f"Round-trip: {len(rows)}/{len(rows)} exact for every method.\n")

    print(f"{'method':<24}{'total bytes':>14}{'ratio':>8}{'vs PP':>8}{'secs':>9}")
    tot_csv = sum(r["csv"] for r in rows)
    pp = sum(r["results"]["POLYPRESS"]["bytes"] for r in rows)
    agg = sorted(methods, key=lambda m: sum(r["results"][m]["bytes"] for r in rows))
    for m in agg:
        b = sum(r["results"][m]["bytes"] for r in rows)
        s = sum(r["results"][m]["secs"] for r in rows)
        print(f"{m:<24}{b:>14,}{tot_csv / b:>7.2f}x{pp / b:>7.3f}x{s:>8.0f}s")

    xl = [m for m in methods if m.startswith("stridexz") and "BEST" in m]
    if xl:
        m = xl[0]
        wins = sum(1 for r in rows
                   if r["results"][m]["bytes"] < r["results"]["POLYPRESS"]["bytes"])
        print(f"\n{m} beats POLYPRESS on {wins} of {len(rows)} tables.")
        ratios = sorted(((r["results"]["POLYPRESS"]["bytes"]
                          / r["results"][m]["bytes"], r["file"]) for r in rows),
                        reverse=True)
        print(f"   best  {ratios[0][0]:.3f}x  {ratios[0][1]}")
        print(f"   worst {ratios[-1][0]:.3f}x  {ratios[-1][1]}")
        mid = ratios[len(ratios) // 2][0]
        print(f"   median {mid:.3f}x")

        print(f"\n   also beats plain xz on "
              f"{sum(1 for r in rows if r['results'][m]['bytes'] < r['results']['xz -9e']['bytes'])}"
              f" of {len(rows)}")

    print(f"\n{'table':<52}{'PP':>10}{'stridexz':>10}{'xz':>10}{'parquet':>10}")
    for r in sorted(rows, key=lambda r: r["file"]):
        g = r["results"]
        name = r["file"][:50]
        print(f"{name:<52}{g['POLYPRESS']['bytes']:>10,}"
              f"{g.get('stridexz BEST', {}).get('bytes', 0):>10,}"
              f"{g['xz -9e']['bytes']:>10,}"
              f"{g['parquet+zstd22']['bytes']:>10,}")


if __name__ == "__main__":
    main()
