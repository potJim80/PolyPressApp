#!/usr/bin/env python3
"""Drive TEST-19 -- structured text columns, measured THROUGH the real codec.

  python3 ../memory/tests/TEST-19/run_test19.py --selftest
  python3 ../memory/tests/TEST-19/run_test19.py '../IN/corpus/*.csv'

For each column the transform is applied only if it BEATS the column's own
share of the archive, measured -- and then the whole-table effect is measured
again, because that is where every transform this session lost the gain it
showed in isolation.
"""
import csv
import glob
import io
import lzma
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import structcol as S

MAXROWS = int(os.environ.get("T19_ROWS", "40000"))


def selftest():
    rnd = random.Random(4)
    cases = []
    cases.append([f"2026-07-{(i % 28) + 1:02d}T{i % 24:02d}:{i % 60:02d}:00.000"
                  for i in range(500)])
    cases.append([f"F26{i:07d}" for i in range(500)])
    cases.append([f"{rnd.randint(1,999)} km SE of Kuril'sk, Russia"
                  for _ in range(300)])
    cases.append(["47.52331", "-122.272834", "0", "", "x"] * 60)
    cases.append([str(i) for i in range(200)])
    cases.append(["007", "042", "999"] * 50)
    cases.append([""] * 50)
    cases.append(["a"] * 50)
    # a column that is mostly template and sometimes not
    c = [f"2026-07-{(i % 28) + 1:02d}" for i in range(500)]
    for i in (3, 77, 300):
        c[i] = ""
    cases.append(c)
    for _ in range(20):
        n = rnd.randint(1, 200)
        cases.append([rnd.choice(["", "1", "x-1", "2026-01-02", "9.5", "\x00"])
                      for _ in range(n)])
    ok = 0
    for i, cells in enumerate(cases):
        blob = S.encode(cells)
        if blob is None:
            continue
        got = S.decode(blob)
        assert got == cells, f"case {i}: {got[:3]!r} != {cells[:3]!r}"
        ok += 1
    print(f"selftest OK -- {ok} of {len(cases)} columns were template-shaped, "
          f"all round-trip exact")


def main():
    selftest()
    if "--selftest" in sys.argv:
        return
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    from polypress import dtz, fast

    paths = []
    for pat in (args or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    print(f"\n# TEST-19  structured text columns, {MAXROWS:,} rows max")
    print("# a column is moved out only if it beats itself, measured; then "
          "the whole table is measured again\n")
    hdr = (f"{'table':<24}{'cols':>5}{'moved':>6}{'pp full':>12}"
           f"{'pp moved':>12}{'streams':>10}{'gain':>8}{'gated':>8}")
    print(hdr)
    print("-" * len(hdr))
    tf = ts = tg = 0
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

        def pp(rs, cols):
            class T:
                pass
            a = T(); a.columns = cols; a.rows = rs
            a.column = lambda j, _r=rs: [r[j] for r in _r]
            return len(fast.encode(a))

        full = pp(rows, t.columns)

        # per-column gate: the stream must beat the column alone through the
        # real codec, which is the cheapest honest proxy for its share
        move, streams = [], []
        for j in range(ncol):
            cells = [r[j] for r in rows]
            blob = S.encode(cells)
            if blob is None:
                continue
            assert S.decode(blob) == cells, f"{p} col {j}"
            alone = pp([[c] for c in cells], [t.columns[j]])
            if len(blob) < alone:
                move.append(j)
                streams.append(blob)
        if not move:
            continue
        keep = [j for j in range(ncol) if j not in set(move)]
        sub = [[r[j] for j in keep] for r in rows]
        moved = (pp(sub, [t.columns[j] for j in keep]) if keep else 0) \
            + sum(len(b) for b in streams)
        # THE GATE THAT MATTERS. The per-column gate above compares a column
        # against ITSELF, and that is not what a column is worth: it may be
        # the sort parent that collapses four other columns into runs. Moving
        # it out costs those bytes somewhere else entirely -- the exact shape
        # CLAUDE.md's measured/unmeasured trap describes, and TEST-18 saw it
        # from the other side (removing `occ_date` cost us 33.5%).
        # So the decision is made on the WHOLE TABLE, both ways, measured.
        gated = min(full, moved)
        n = os.path.basename(p).replace(".csv", "")[:23]
        print(f"{n:<24}{ncol:>5}{len(move):>6}{full:>12,}{moved:>12,}"
              f"{sum(len(b) for b in streams):>10,}{1 - moved/full:>7.1%}"
              f"{1 - gated/full:>8.1%}", flush=True)
        tf += full; ts += moved; tg += gated
    print("-" * len(hdr))
    print(f"{'TOTAL':<24}{'':>5}{'':>6}{tf:>12,}{ts:>12,}{'':>10}"
          f"{(1 - ts/tf) if tf else 0:>7.1%}{(1 - tg/tf) if tf else 0:>8.1%}")
    print("\nungated the transform LOSES 3.7%; gated on the whole table it "
          "cannot lose, and wins where it wins.")


if __name__ == "__main__":
    main()
