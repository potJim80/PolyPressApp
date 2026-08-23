#!/usr/bin/env python3
"""TEST-17 -- functional dependencies, and whether reordering already has them.

TEST-15 and TEST-16 both won on the same shape: a HIGH-cardinality column
derivable from others. TEST-16's README states the prediction this test exists
to check:

    A low-cardinality functional dependency -- `status_code` determines
    `status_description`, `zip` determines `city` -- should be worth much less,
    because this codec ALREADY exploits it. `pick_parents` scores conditional
    entropy and sorts the rows by the parent, which collapses the dependent
    column into runs. A perfect low-cardinality dependency is close to free.

If that is right, mining functional dependencies is not where the remaining
cross-column money is, and the detector should keep chasing high-cardinality
derived columns instead. If it is wrong, FD mining is the next build.

An FD here is exact and one-directional: column A determines column B when
every distinct value of A co-occurs with exactly one value of B. Detection is
one pass per ordered pair with a dict -- far cheaper than TEST-15's
prefix matching, which is the point.

  python3 ../memory/tests/TEST-17/run_test17.py --selftest
  python3 ../memory/tests/TEST-17/run_test17.py '../IN/corpus/*.csv'
"""
from __future__ import annotations

import csv
import glob
import io
import lzma
import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.getcwd())

MAXROWS = int(os.environ.get("T17_ROWS", "40000"))
NEAR_KEY = float(os.environ.get("T17_NEAR_KEY", "0.25"))
XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])


def xz(b):
    return lzma.compress(b, **XZ)


def find_fds(rows: List[List[str]], ncol: int) -> List[Tuple[int, int, int]]:
    """(determinant, dependent, distinct values of the determinant).

    One dict per ordered pair. A column that is constant determines everything
    and is worth nothing, so it is excluded; a column whose values are all
    distinct determines everything trivially and is also excluded -- both are
    real FDs and neither is compressible information."""
    nrow = len(rows)
    cols = [[r[j] for r in rows] for j in range(ncol)]
    card = [len(set(c)) for c in cols]
    out = []
    for a in range(ncol):
        # The near-key trap, named by CORDS (SIGMOD 2004) and rediscovered by
        # the research pass on this corpus: `latitude -> longitude` holds when
        # latitude has 28,656 distinct values in 40,000 rows, and the map costs
        # as much as the column. Gate on card(A)/n, not on the FD.
        if card[a] <= 1 or card[a] > NEAR_KEY * nrow:
            continue
        for b in range(ncol):
            if a == b or card[b] <= 1:
                continue
            if card[b] > card[a]:
                continue        # a determinant cannot have fewer classes
            m: Dict[str, str] = {}
            ok = True
            for va, vb in zip(cols[a], cols[b]):
                seen = m.get(va)
                if seen is None:
                    m[va] = vb
                elif seen != vb:
                    ok = False
                    break
            if ok:
                out.append((a, b, card[a]))
    return out


def choose(fds, ncol: int) -> List[Tuple[int, int]]:
    """Keep one determinant per dependent, preferring the coarsest (fewest
    distinct values -> smallest map). Never drop a column that is itself
    someone's determinant, so one pass rebuilds everything."""
    best: Dict[int, Tuple[int, int]] = {}
    for a, b, ca in fds:
        if b not in best or ca < best[b][1]:
            best[b] = (a, ca)
    # Incremental, in column order. A BIJECTION determines in both
    # directions, so taking every pair at once and then filtering drops
    # nothing at all -- each side disqualifies the other. Committing one at a
    # time breaks the tie and keeps exactly one of the pair.
    chosen, dropped = [], set()
    for b in sorted(best):
        a, _ = best[b]
        if a == b or a in dropped or b in dropped:
            continue
        chosen.append((a, b))
        dropped.add(b)
    # A determinant chosen early can be DROPPED later as someone else's
    # dependent, leaving the first pair with no source -- a KeyError on real
    # data. Same shape as TEST-15's cycle bug and the `turbo` branch's
    # dependency-order fix. Iterate to a fixed point.
    while True:
        drop = {b for _, b in chosen}
        keep = [(a, b) for a, b in chosen if a not in drop]
        if len(keep) == len(chosen):
            return keep
        chosen = keep


def strip(rows, pairs):
    drop = {b for _, b in pairs}
    keep = [j for j in range(len(rows[0])) if j not in drop]
    return [[r[j] for j in keep] for r in rows], keep


def maps_bytes(rows, pairs) -> bytes:
    out = bytearray()
    for a, b in pairs:
        m = {}
        for r in rows:
            m.setdefault(r[a], r[b])
        out += f"{a}\t{b}\t{len(m)}\n".encode()
        for k, v in m.items():
            out += f"{k}\t{v}\n".encode()
    return bytes(out)


def rebuild(stripped, keep, ncol, rows_orig, pairs):
    nrow = len(stripped)
    full = [[None] * ncol for _ in range(nrow)]
    for i, row in enumerate(stripped):
        for pos, j in enumerate(keep):
            full[i][j] = row[pos]
    for a, b in pairs:
        m = {}
        for r in rows_orig:
            m.setdefault(r[a], r[b])
        for i in range(nrow):
            full[i][b] = m[full[i][a]]
    return full


def selftest():
    rows = []
    for i in range(500):
        code = ["A", "B", "C"][i % 3]
        desc = {"A": "Approved", "B": "Blocked", "C": "Closed"}[code]
        rows.append([str(i), code, desc, f"{i%7}"])
    fds = find_fds(rows, 4)
    assert any(a == 1 and b == 2 for a, b, _ in fds), "code->desc not found"
    pairs = choose(fds, 4)
    st, keep = strip(rows, pairs)
    assert rebuild(st, keep, 4, rows, pairs) == rows, "fd rebuild"
    print(f"selftest OK -- {len(fds)} FDs found, {len(pairs)} dropped, "
          f"rebuild exact")


def main():
    selftest()
    if "--selftest" in sys.argv:
        return
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    from polypress import dtz, fast

    paths = []
    for pat in (args or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    print(f"\n# TEST-17  functional dependencies, {MAXROWS:,} rows max")
    print("# does dropping a determined column beat what reordering already "
          "does?\n")
    hdr = (f"{'table':<24}{'cols':>5}{'FDs':>5}{'drop':>5}{'pp full':>12}"
           f"{'pp strip':>12}{'maps':>10}{'pp gain':>9}")
    print(hdr)
    print("-" * len(hdr))
    tf = ts = 0
    for p in paths:
        try:
            t = dtz.read_any(p)
        except Exception:
            continue
        rows = t.rows[:MAXROWS]
        if not rows:
            continue
        ncol = len(t.columns)
        rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
                for r in rows]
        fds = find_fds(rows, ncol)
        pairs = choose(fds, ncol)
        if not pairs:
            continue
        st, keep = strip(rows, pairs)
        assert rebuild(st, keep, ncol, rows, pairs) == rows, f"{p}: rebuild"

        def pp(rs, cols):
            class T:
                pass
            a = T(); a.columns = cols; a.rows = rs
            a.column = lambda j, _r=rs: [r[j] for r in _r]
            return len(fast.encode(a))

        full = pp(rows, t.columns)
        mp = len(xz(maps_bytes(rows, pairs)))
        stp = pp(st, [t.columns[j] for j in keep]) + mp
        n = os.path.basename(p).replace(".csv", "")[:23]
        print(f"{n:<24}{ncol:>5}{len(fds):>5}{len(pairs):>5}{full:>12,}"
              f"{stp:>12,}{mp:>10,}{1 - stp/full:>8.1%}", flush=True)
        tf += full; ts += stp
    print("-" * len(hdr))
    print(f"{'TOTAL':<24}{'':>5}{'':>5}{'':>5}{tf:>12,}{ts:>12,}{'':>10}"
          f"{(1 - ts/tf) if tf else 0:>8.1%}")


if __name__ == "__main__":
    main()
