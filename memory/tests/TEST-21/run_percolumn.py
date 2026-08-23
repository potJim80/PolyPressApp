#!/usr/bin/env python3
"""TEST-21b -- composite parents, against what the codec ACTUALLY does.

TEST-21's first run compared a two-key global row order against a one-key
global row order. **That is not this codec's mechanism**, and the first reading
of the result was wrong.

`fast.decode` does, per column:

    perm = np.argsort(ids_by_pos[parent], kind="stable")
    out[perm] = ids

So parents form a TREE and **every column is stored under its own parent's
order**, each permutation free because the decoder recomputes it. There is no
single shared row order to improve on. The right question is per column:

    does sorting column C by lexsort(A, B) beat sorting it by argsort(A),
    where A is the best single parent C could have had?

That is what this measures. Each column is scored on its own, exactly as the
codec stores it, with the same free-permutation accounting.

  python3 ../memory/tests/TEST-21/run_percolumn.py austin_incidents ...
"""
from __future__ import annotations

import lzma
import os
import sys
from typing import List

import numpy as np

sys.path.insert(0, os.getcwd())

MAXROWS = int(os.environ.get("T21_ROWS", "40000"))
NOM = int(os.environ.get("T21_NOM", "10"))
XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])


def size(a: np.ndarray) -> int:
    w = 1 if a.max() < 256 else (2 if a.max() < 65536 else 4)
    return len(lzma.compress(a.astype(f"<u{w}").tobytes(), **XZ))


def cond_entropy(a: np.ndarray, b: np.ndarray) -> float:
    n = len(a)
    key = a.astype(np.int64) * (int(b.max()) + 1) + b
    _, cj = np.unique(key, return_counts=True)
    _, ca = np.unique(a, return_counts=True)
    return float(-(cj / n * np.log2(cj / n)).sum()
                 + (ca / n * np.log2(ca / n)).sum())


def main():
    from polypress import dtz
    names = sys.argv[1:] or ["austin_incidents"]
    gtot_s = gtot_p = 0
    for name in names:
        t = dtz.read_any(f"../IN/corpus/{name}.csv")
        rows = t.rows[:MAXROWS]
        ncol = len(t.columns)
        rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
                for r in rows]
        n = len(rows)
        ids: List[np.ndarray] = []
        for j in range(ncol):
            vals = [r[j] for r in rows]
            uniq = {v: i for i, v in enumerate(sorted(set(vals)))}
            ids.append(np.array([uniq[v] for v in vals], dtype=np.int64))
        card = [int(c.max()) + 1 for c in ids]
        usable = [a for a in range(ncol) if 1 < card[a] < n]

        print(f"\n# TEST-21b  {name}: {ncol} columns, {n:,} rows")
        print(f"{'column':<26}{'unsorted':>11}{'best single':>13}"
              f"{'best pair':>12}{'pair/single':>13}")
        print("-" * 75)
        tot_s = tot_p = 0
        for c in range(ncol):
            if card[c] <= 1:
                continue
            plain = size(ids[c])
            # entropy nominates the parents worth measuring
            cands = sorted(((cond_entropy(ids[a], ids[c]), a)
                            for a in usable if a != c))[:NOM]
            keys = [a for _, a in cands]
            if not keys:
                continue
            bs = min(size(ids[c][np.argsort(ids[a], kind="stable")])
                     for a in keys)
            bp = bs
            for a in keys[:4]:
                for b in keys:
                    if b == a:
                        continue
                    o = np.lexsort((ids[b], ids[a]))
                    v = size(ids[c][o])
                    if v < bp:
                        bp = v
            tot_s += min(plain, bs)
            tot_p += min(plain, bs, bp)
            if bp < bs:
                print(f"{t.columns[c][:25]:<26}{plain:>11,}{bs:>13,}"
                      f"{bp:>12,}{bp/bs:>13.3f}")
        print("-" * 75)
        print(f"{'TOTAL over all columns':<26}{'':>11}{tot_s:>13,}"
              f"{tot_p:>12,}{tot_p/tot_s if tot_s else 1:>13.4f}")
        gtot_s += tot_s
        gtot_p += tot_p
    print(f"\nALL TABLES  best-single {gtot_s:,}  with-pairs {gtot_p:,}  "
          f"ratio {gtot_p/gtot_s if gtot_s else 1:.4f}")


if __name__ == "__main__":
    main()
