#!/usr/bin/env python3
"""TEST-2b: every type of ANS against plain xz, on 200 real single columns.

Case A is `xz -9e`. Case B is ANS -- but ANS is a family, so B is eight
coders spanning the two axes that actually distinguish them:

  the ANS variant   uABS / rABS / rANS / tANS  (+ interleaved rANS, which is
                    a speed arrangement of rANS and is here to prove that)
  the model         order 0 or 1; static (table sent, charged) or adaptive
                    (nothing sent, both sides learn)

Columns are capped smaller than in TEST-2 because the binary coders run at
about 0.25 MB/s in Python. Totals are therefore NOT comparable with
results.txt; ratios are.

Run from work/:  python3 ../memory/tests/TEST-2/run_test2b.py
"""
from __future__ import annotations

import lzma
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rans                     # noqa: E402
import ansfam as A              # noqa: E402
from harvest import harvest, KINDS   # noqa: E402
from test2 import flatten, order0_floor   # noqa: E402

TARGET = int(os.environ.get("TEST2_N", "200"))
PATTERN = os.environ.get("TEST2_GLOB", "../IN/corpus500/*.csv")
CAP = int(os.environ.get("TEST2_CAP", "150000"))

# label, encoder, decoder, ANS variant, alphabet, model
CODERS = [
    ("B1 rANS-o0",   rans.rans0, rans.unrans0, "rANS", "byte", "order-0 static"),
    ("B2 rANS-o1",   rans.rans1, rans.unrans1, "rANS", "byte", "order-1 static"),
    ("B3 tANS-o0",   A.tans0,    A.untans0,    "tANS", "byte", "order-0 static"),
    ("B4 tANS-o1",   A.tans1,    A.untans1,    "tANS", "byte", "order-1 static"),
    ("B5 rANSx4-o0", A.rans0x4,  A.unrans0x4,  "rANS x4", "byte", "order-0 static"),
    ("B6 rABS-o0",   A.rabs0,    A.unrabs0,    "rABS", "bit tree", "order-0 adaptive"),
    ("B7 rABS-o1",   A.rabs1,    A.unrabs1,    "rABS", "bit tree", "order-1 adaptive"),
    ("B8 uABS-o0",   A.uabs0,    A.ununabs0,   "uABS", "bit tree", "order-0 adaptive"),
]
LABELS = [c[0] for c in CODERS]
SHORT = [l.split()[1] for l in LABELS]


def measure(name, kind, values):
    raw = flatten(values)
    row = {"name": name, "kind": kind, "n": len(values),
           "k": len(set(values)), "raw": len(raw),
           "floor": order0_floor(raw)}
    a = lzma.compress(raw, **rans.XZ)
    assert lzma.decompress(a, **rans.XZ) == raw
    row["xz"] = len(a)
    for label, enc, dec, *_ in CODERS:
        blob = enc(raw)
        assert dec(blob) == raw, f"{name}: {label} round-trip failed"
        row[label] = len(blob)
    return row


def summary(rows, title, key):
    groups = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    print(f"\n{title}")
    head = f"  {'group':<14}{'cols':>5}{'rawKB':>8}{'xz':>11}" + \
        "".join(f"{s:>11}" for s in SHORT) + f"{'bestB/xz':>10}"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for g in sorted(groups, key=lambda g: -sum(x["raw"] for x in groups[g])):
        rs = groups[g]
        tot = {l: sum(r[l] for r in rs) for l in LABELS}
        axz = sum(r["xz"] for r in rs)
        bestb = sum(min(r[l] for l in LABELS) for r in rs)
        print(f"  {g:<14}{len(rs):>5}{sum(r['raw'] for r in rs) // 1024:>8}"
              f"{axz:>11,}" +
              "".join(f"{tot[l]:>11,}" for l in LABELS) +
              f"{bestb / axz:>10.2f}")


def main():
    t0 = time.time()
    sys.stderr.write(f"harvesting {TARGET} columns (cap {CAP} B)\n")
    cols = harvest(PATTERN, TARGET, max_bytes=CAP)
    sys.stderr.write(f"{len(cols)} columns in {time.time() - t0:.0f}s\n")

    print(f"# TEST-2b  {len(cols)} real single columns, {PATTERN}, "
          f"capped at {CAP:,} B each")
    print("# A = xz -9e on the column text. B1..B8 = the ANS family.")
    print("# Every coder decoded and compared before its size was counted.")
    print("#")
    print(f"# {'label':<14}{'ANS variant':<12}{'alphabet':<11}{'model':<20}")
    for label, _, _, var, alpha, model in CODERS:
        print(f"# {label:<14}{var:<12}{alpha:<11}{model:<20}")
    print()

    hdr = (f"{'column':<40}{'kind':<12}{'n':>7}{'k':>7}{'xz':>10}" +
           "".join(f"{s:>10}" for s in SHORT) + f"{'bestB/xz':>10}")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for name, kind, vals in cols:
        try:
            r = measure(name, kind, vals)
        except Exception as e:
            print(f"{name[:39]:<40}{kind:<12}  FAILED {type(e).__name__}: {e}",
                  flush=True)
            raise
        rows.append(r)
        best = min(r[l] for l in LABELS)
        print(f"{r['name'][:39]:<40}{r['kind']:<12}{r['n']:>7}{r['k']:>7}"
              f"{r['xz']:>10}" +
              "".join(f"{r[l]:>10}" for l in LABELS) +
              f"{best / r['xz']:>10.3f}", flush=True)

    print(f"\n{len(rows)} columns, all round-trip verified, "
          f"{time.time() - t0:.0f}s")

    summary(rows, "BY KIND", lambda r: r["kind"])
    summary(rows, "ALL", lambda r: "all")

    axz = sum(r["xz"] for r in rows)
    print("\nEACH ANS TYPE AGAINST xz -- whole corpus")
    print(f"  {'coder':<14}{'variant':<10}{'model':<20}{'total':>13}"
          f"{'vs xz':>8}{'wins':>9}{'med/floor':>11}")
    print("  " + "-" * 83)
    ranked = sorted(CODERS, key=lambda c: sum(r[c[0]] for r in rows))
    for label, _, _, var, alpha, model in ranked:
        tot = sum(r[label] for r in rows)
        wins = sum(1 for r in rows if r[label] < r["xz"])
        mf = st.median(r[label] / r["floor"] for r in rows if r["floor"] > 0)
        print(f"  {label:<14}{var:<10}{model:<20}{tot:>13,}"
              f"{tot / axz:>8.2f}{wins:>6}/{len(rows):<3}{mf:>11.2f}")
    print(f"  {'A xz -9e':<14}{'--':<10}{'LZ77 + range coder':<20}{axz:>13,}"
          f"{1.00:>8.2f}{'--':>10}"
          f"{st.median(r['xz'] / r['floor'] for r in rows if r['floor'] > 0):>11.2f}")

    print("\nWHAT EACH AXIS IS WORTH (aggregate bytes, same 200 columns)")

    def tot(l):
        return sum(r[l] for r in rows)

    def cmp(a, b, note):
        wa = sum(1 for r in rows if r[a] < r[b])
        print(f"  {a:<14} vs {b:<14} {tot(a) / tot(b):>6.3f}   "
              f"{a.split()[1]} wins {wa:>3}/{len(rows)}   {note}")

    cmp("B3 tANS-o0", "B1 rANS-o0", "table vs range, same model")
    cmp("B4 tANS-o1", "B2 rANS-o1", "table vs range, same model")
    cmp("B5 rANSx4-o0", "B1 rANS-o0", "interleaving: speed, not size")
    cmp("B8 uABS-o0", "B6 rABS-o0", "uniform vs range, binary")
    cmp("B6 rABS-o0", "B1 rANS-o0", "bit tree adaptive vs byte static")
    cmp("B7 rABS-o1", "B2 rANS-o1", "bit tree adaptive vs byte static, order 1")
    cmp("B2 rANS-o1", "B1 rANS-o0", "order 1 vs order 0, static")
    cmp("B7 rABS-o1", "B6 rABS-o0", "order 1 vs order 0, adaptive")

    print("\nSMALL COLUMNS ONLY (raw < 20 KB) -- where the static table hurts")
    small = [r for r in rows if r["raw"] < 20480]
    if small:
        sxz = sum(r["xz"] for r in small)
        for label, _, _, var, alpha, model in CODERS:
            t = sum(r[label] for r in small)
            print(f"  {label:<14}{t:>10,}  vs xz {t / sxz:>6.2f}  "
                  f"wins {sum(1 for r in small if r[label] < r['xz'])}/{len(small)}")
        print(f"  {'A xz -9e':<14}{sxz:>10,}   ({len(small)} columns)")

    print("\nBEST ANS OF ANY TYPE BEATS xz ON")
    won = [r for r in rows if min(r[l] for l in LABELS) < r["xz"]]
    for r in sorted(won, key=lambda r: min(r[l] for l in LABELS) / r["xz"])[:20]:
        b = min(LABELS, key=lambda l: r[l])
        print(f"  {r['name'][:39]:<40}{r['kind']:<12}"
              f"{min(r[l] for l in LABELS) / r['xz']:>6.3f}  by {b}")
    print(f"  ({len(won)} of {len(rows)})")


if __name__ == "__main__":
    main()
