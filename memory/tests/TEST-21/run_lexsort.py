#!/usr/bin/env python3
"""TEST-21 -- composite (multi-key) sort parents. Candidate C1, finally measured.

Three measurements this session point at the same missing feature:

  TEST-17  `austin_incidents` gains 8.7% from stripping functional dependencies
           and nothing anywhere else -- it is the one table with SEVERAL
           INDEPENDENT FDs, against LAW 1's single shared row order.
  TEST-18  `ucr_code` and `crime_type` carry 58% of the kanzi gap on that
           table, and `crime_type` determines `ucr_code`.
  C1       `pick_parents` returns Dict[int, Optional[int]] -- exactly ONE
           parent per column -- and `np.lexsort` appears nowhere in fast.py.

Sorting by `(A, then B)` costs **zero bytes**, for the same reason sorting by
`A` does: the decoder holds both columns already and recomputes the argsort.
The single-parent search structurally cannot see the case where `H(C|A)` and
`H(C|B)` are both mediocre while `H(C|A,B)` is near zero.

WHAT THIS MEASURES

A mini-codec, deliberately small enough that the row order is the only thing
that varies: dictionary-encode every column, emit column-major id streams, one
xz. Then try row orders:

    identity          the file as it arrives
    sort by A         every single column, stable
    lexsort by (A,B)  the most promising pairs

The permutation is never stored in any variant -- the decoder recomputes it by
stably sorting the key columns, which it has already decoded. So the comparison
is pure: same bytes, same coder, different order.

This is a ceiling probe on the ORDER only. Per LAW 4 it nominates; it does not
decide. A real composite parent has to be measured through `fast.encode`.

  python3 ../memory/tests/TEST-21/run_lexsort.py austin_incidents cdc_nndss
"""
from __future__ import annotations

import lzma
import os
import sys
from typing import List, Tuple

import numpy as np

sys.path.insert(0, os.getcwd())

MAXROWS = int(os.environ.get("T21_ROWS", "40000"))
TOPK = int(os.environ.get("T21_TOPK", "6"))
NOM = int(os.environ.get("T21_NOM", "12"))
XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])


def xz(b: bytes) -> bytes:
    return lzma.compress(b, **XZ)


def encode_ids(ids: List[np.ndarray], order: np.ndarray) -> int:
    """Column-major id streams under a row order, one xz. The permutation is
    NOT stored -- the decoder recomputes it from the key columns."""
    parts = []
    for col in ids:
        a = col[order]
        w = 1 if a.max() < 256 else (2 if a.max() < 65536 else 4)
        parts.append(a.astype(f"<u{w}").tobytes())
    return len(xz(b"".join(parts)))


def cond_entropy(a: np.ndarray, b: np.ndarray) -> float:
    """H(b | a) in bits, quantised the way fast.py quantises scores so ties
    fall the same way (see the numpy/SIMD note in CLAUDE.md)."""
    n = len(a)
    key = a.astype(np.int64) * (b.max() + 1) + b
    _, cj = np.unique(key, return_counts=True)
    _, ca = np.unique(a, return_counts=True)
    hj = -(cj / n * np.log2(cj / n)).sum()
    ha = -(ca / n * np.log2(ca / n)).sum()
    return float(hj - ha)


def main():
    from polypress import dtz
    names = sys.argv[1:] or ["austin_incidents"]
    for name in names:
        t = dtz.read_any(f"../IN/corpus/{name}.csv")
        rows = t.rows[:MAXROWS]
        ncol = len(t.columns)
        rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
                for r in rows]
        n = len(rows)
        ids = []
        for j in range(ncol):
            vals = [r[j] for r in rows]
            uniq = {v: i for i, v in enumerate(sorted(set(vals)))}
            ids.append(np.array([uniq[v] for v in vals], dtype=np.int64))
        card = [int(c.max()) + 1 for c in ids]

        base = encode_ids(ids, np.arange(n))
        print(f"\n# TEST-21  {name}: {ncol} columns, {n:,} rows")
        print(f"# identity order: {base:,} bytes\n")

        # single keys. Measuring every column is O(ncol) whole-payload xz
        # calls, which is minutes on the 116-column table -- so entropy
        # NOMINATES (sum of H(C|A) over all C, cheap in numpy) and the
        # measurement decides, which is this repo's standing rule.
        usable = [a for a in range(ncol) if 1 < card[a] < n]
        if len(usable) > NOM:
            score = []
            for a in usable:
                tot = sum(cond_entropy(ids[a], ids[c])
                          for c in range(ncol) if c != a)
                score.append((tot, a))
            score.sort()
            usable = [a for _, a in score[:NOM]]
        singles = []
        for a in usable:
            order = np.argsort(ids[a], kind="stable")
            singles.append((encode_ids(ids, order), a))
        singles.sort()
        print(f"{'best single keys':<44}{'bytes':>12}{'vs identity':>13}")
        print("-" * 69)
        for sz, a in singles[:5]:
            print(f"  sort by {t.columns[a][:34]:<34}{sz:>12,}"
                  f"{sz/base:>13.3f}")

        # composite keys: nominate by conditional entropy, then MEASURE
        cand: List[Tuple[float, int, int]] = []
        keys = [a for _, a in singles[:TOPK]]
        for a in keys:
            for b in usable:
                if b == a:
                    continue
                # a pair is interesting when B adds information A lacks
                cand.append((-cond_entropy(ids[a], ids[b]), a, b))
        cand.sort()
        seen = set()
        pairs = []
        for _, a, b in cand:
            if (a, b) in seen or (b, a) in seen:
                continue
            seen.add((a, b))
            pairs.append((a, b))
            if len(pairs) >= 12:
                break

        out = []
        for a, b in pairs:
            order = np.lexsort((ids[b], ids[a]))
            out.append((encode_ids(ids, order), a, b))
        out.sort()
        print(f"\n{'best composite keys':<44}{'bytes':>12}{'vs identity':>13}"
              f"{'vs best single':>16}")
        print("-" * 85)
        bs = singles[0][0] if singles else base
        for sz, a, b in out[:6]:
            lbl = f"{t.columns[a][:16]} + {t.columns[b][:16]}"
            print(f"  lexsort {lbl:<34}{sz:>12,}{sz/base:>13.3f}"
                  f"{sz/bs:>16.3f}")
        if out and singles:
            best = out[0][0]
            print(f"\nbest single {bs:,}  best pair {best:,}  "
                  f"pair/single {best/bs:.4f}"
                  f"  -> {'PAIRS WIN' if best < bs else 'no gain from pairs'}")


if __name__ == "__main__":
    main()
