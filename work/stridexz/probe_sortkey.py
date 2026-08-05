#!/usr/bin/env python3
"""Which sort key wins, and can a measured check tell before paying for it?

The naive probe sorted by every column at once, in cardinality order, and lost
on two tables of five. Two things could explain that:

  (a) the original row order already carries grouping that the sort destroys,
  (b) the sort key was simply the wrong one.

This measures both. For each table it reports how pre-grouped the incoming
rows already are, then encodes once per candidate sort key -- including "no
sort at all" -- and names the winner. Refusing to sort is a candidate like any
other, which is the whole point: Polypress reorders *and can decline*, and
stridexz has no mechanism to decline.

Sizes here are FREE-mode: the permutation is not stored. That isolates the
value of the ordering from the cost of remembering it, which the reorder probe
already measured separately at 3-4%.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "old"))

from polypress import dtz, fast          # noqa: E402
from stridexz import codec                  # noqa: E402

BEST = dict(pool=True, fixed=True, planes=True, tune={"lc": 4, "pb": 0})
NCAND = 6


def breaks(cells):
    """Adjacent positions where the value changes. Low = already grouped."""
    return sum(1 for i in range(1, len(cells)) if cells[i] != cells[i - 1])


def load(path, max_mb):
    cap = int(max_mb * 1024 * 1024)
    if os.path.getsize(path) <= cap:
        return dtz.read_any(path).normalise()
    with open(path, "rb") as fh:
        head = fh.read(cap)
    head = head[:head.rfind(b"\n") + 1]
    tmp = path + ".__sortkey_cut"
    with open(tmp, "wb") as fh:
        fh.write(head)
    try:
        return dtz.read_any(tmp).normalise()
    finally:
        os.unlink(tmp)


def main():
    max_mb = float(os.environ.get("PROBE_MAX_MB", "16"))
    for path in sys.argv[1:]:
        t = load(path, max_mb)
        n = len(t.rows)
        cols = [t.column(i) for i in range(len(t.columns))]
        card = [len(set(c)) for c in cols]
        brk = [breaks(c) for c in cols]

        # How much grouping is already in the file? For each column, the ideal
        # number of breaks if sorted is card-1; compare with what is there.
        pre = [(brk[i] / max(card[i] - 1, 1), i) for i in range(len(cols))
               if 1 < card[i] < n]
        pre.sort()
        print(f"\n{os.path.basename(path)[:56]}  {n:,} rows x {len(cols)} cols")
        print("   already-grouped columns (1.0 = as grouped as sorting could "
              "make it):")
        for ratio, i in pre[:4]:
            print(f"      {t.columns[i][:30]:<32} card {card[i]:>7,}  "
                  f"breaks {brk[i]:>7,}  = {ratio:>7.1f}x ideal")

        pp = len(fast.encode(t))
        base = len(codec.encode(t, **BEST))
        print(f"   polypress {pp:,}   stridexz unsorted {base:,}")

        # Candidates: lowest-cardinality columns, plus the all-columns sort.
        cands = [i for _, i in sorted((card[i], i) for i in range(len(cols))
                                      if 1 < card[i])][:NCAND]
        results = [("no sort", base)]
        for i in cands:
            idx = sorted(range(n), key=lambda r: t.rows[r][i])
            st = dtz.Table(list(t.columns), [t.rows[r] for r in idx])
            results.append((f"by {t.columns[i][:24]} (card {card[i]})",
                            len(codec.encode(st, **BEST))))
        allc = [i for _, i in sorted((card[i], i) for i in range(len(cols)))]
        idx = sorted(range(n), key=lambda r: [t.rows[r][c] for c in allc])
        st = dtz.Table(list(t.columns), [t.rows[r] for r in idx])
        results.append(("by ALL columns", len(codec.encode(st, **BEST))))

        results.sort(key=lambda kv: kv[1])
        print("   candidates, best first:")
        for name, size in results:
            mark = "  <-- unsorted" if name == "no sort" else ""
            print(f"      {name:<40}{size:>12,}  {base / size:5.3f}x{mark}")
        win = results[0]
        print(f"   VERDICT: {'REFUSE (no sort wins)' if win[0] == 'no sort' else win[0]}"
              f"  {base / win[1]:.3f}x, still {win[1] / pp:.3f}x of polypress")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
