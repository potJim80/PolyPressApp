#!/usr/bin/env python3
"""Drive LAW 1 over synthetic controls and real single columns."""
import sys, os, csv, random, math, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from law1 import measure, HDR, line

csv.field_size_limit(10 ** 9)
N = 100_000

def synth():
    rnd = random.Random(7)
    cases = []

    cases.append(("syn: constant (k=1)", ["ALPHA"] * N))
    cases.append(("syn: 2 values, random", [rnd.choice(["Y", "N"]) for _ in range(N)]))
    cases.append(("syn: 10 codes, uniform",
                  [f"C{rnd.randrange(10):02d}" for _ in range(N)]))
    cases.append(("syn: 10 codes, zipf-ish",
                  [f"C{min(9, int(rnd.paretovariate(1.2)) - 1):02d}" for _ in range(N)]))
    cases.append(("syn: 1000 codes, uniform",
                  [f"CODE{rnd.randrange(1000):04d}" for _ in range(N)]))

    # all-distinct random ids: order-0 entropy is the whole content
    cases.append(("syn: distinct 9-digit ids, shuffled",
                  [str(x) for x in rnd.sample(range(100_000_000, 999_999_999), N)]))
    ids = sorted(rnd.sample(range(100_000_000, 999_999_999), N))
    cases.append(("syn: distinct 9-digit ids, SORTED", [str(x) for x in ids]))

    # a smooth numeric series -- the shape a predictor is meant to love
    v = 100.0; ser = []
    for _ in range(N):
        v += rnd.gauss(0, 0.25); ser.append(f"{v:.2f}")
    cases.append(("syn: random walk, 2dp", ser))

    # already grouped by value (the cdc_nndss shape)
    grouped = []
    for c in range(200):
        grouped.extend([f"G{c:03d}"] * (N // 200))
    cases.append(("syn: 200 codes, already grouped", grouped))

    # low-cardinality but with strong ORDER structure xz can see and B cannot
    seq = []
    for i in range(N):
        seq.append(["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][i % 7])
    cases.append(("syn: 7 codes, perfectly cyclic", seq))
    return cases


def real_columns(paths, max_rows=200_000, per_file=6):
    out = []
    for p in paths:
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                rd = csv.reader(fh)
                hdr = next(rd)
                cols = [[] for _ in hdr]
                for i, row in enumerate(rd):
                    if i >= max_rows:
                        break
                    for j in range(min(len(row), len(hdr))):
                        cols[j].append(row[j])
                    for j in range(len(row), len(hdr)):
                        cols[j].append("")
        except (UnicodeDecodeError, StopIteration, csv.Error):
            continue
        base = os.path.basename(p).replace(".csv", "")
        n = len(cols[0]) if cols else 0
        if n < 1000:
            continue
        # a spread of cardinalities, not just the first few columns
        idxs = sorted(range(len(hdr)), key=lambda j: len(set(cols[j])))
        pick = []
        if idxs:
            step = max(1, len(idxs) // per_file)
            pick = idxs[::step][:per_file]
        for j in pick:
            out.append((f"{base[:24]}:{hdr[j][:16]}", cols[j]))
    return out


def main():
    rows = []
    print(HDR); print("-" * len(HDR))
    for name, vals in synth():
        r = measure(name, vals); rows.append(r); print(line(r), flush=True)

    print()
    paths = sorted(glob.glob("../IN/corpus100/*.csv"))[:12]
    print(f"# real columns from {len(paths)} files", flush=True)
    print(HDR); print("-" * len(HDR))
    for name, vals in real_columns(paths):
        try:
            r = measure(name, vals)
        except Exception as e:
            print(f"{name:<44} SKIP {e}"); continue
        rows.append(r); print(line(r), flush=True)

    real = [r for r in rows if not r["name"].startswith("syn:")]
    print()
    for tag, group in (("SYNTHETIC", [r for r in rows if r["name"].startswith("syn:")]),
                       ("REAL", real), ("ALL", rows)):
        if not group:
            continue
        A = sum(r["A"] for r in group); B = sum(r["Bbest"] for r in group)
        wins = sum(1 for r in group if r["Bbest"] < r["A"])
        print(f"{tag:<10} tables {len(group):>4}  xz {A:>12,}  B {B:>12,}"
              f"  B/A {B/A:>5.2f}   B wins {wins}/{len(group)}")

    print("\n--- where B wins ---")
    for r in sorted(rows, key=lambda r: r["Bbest"] / r["A"]):
        if r["Bbest"] < r["A"]:
            print(f"  {r['name'][:43]:<44} B/A {r['Bbest']/r['A']:.3f}"
                  f"  k/n {r['k']/r['n']:.4f}")

if __name__ == "__main__":
    main()
