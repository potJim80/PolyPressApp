#!/usr/bin/env python3
"""How much of the stridexz-vs-Polypress gap is the missing row reorder?

stridexz rearranges columns and the bytes inside a column. It never touches the
order of the rows -- and reordering rows so a column collapses into runs is
Polypress's central idea. This measures what that omission costs.

Three numbers per table:

  stridexz              as shipped, original row order
  stridexz +sort +perm  rows sorted, and the permutation stored so the original
                     order comes back. Lossless and order-preserving, i.e. a
                     real candidate encoding. Round-trip verified.
  stridexz +sort FREE   rows sorted, permutation NOT stored. Not a codec -- it
                     cannot restore the input. It is the ceiling: what the
                     reorder would be worth if the permutation were free.

The gap between the last two is the price of remembering where the rows went,
which is the part that makes row reordering hard rather than obvious.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "old"))

from polypress import dtz, fast          # noqa: E402
from stridexz import codec                  # noqa: E402

BEST = dict(pool=True, fixed=True, planes=True, tune={"lc": 4, "pb": 0})


def sort_key_columns(t):
    """Columns by increasing distinct count -- the ones that form runs first."""
    card = [(len(set(t.column(i))), i) for i in range(len(t.columns))]
    card.sort()
    return [i for _, i in card]


def sorted_table(t):
    """-> (table with rows sorted, permutation back to the original order)."""
    order = sort_key_columns(t)
    idx = sorted(range(len(t.rows)), key=lambda r: [t.rows[r][c] for c in order])
    rows = [t.rows[r] for r in idx]
    return dtz.Table(list(t.columns), rows), idx


def load(path, max_mb):
    cap = int(max_mb * 1024 * 1024)
    if os.path.getsize(path) <= cap:
        return dtz.read_any(path).normalise()
    with open(path, "rb") as fh:
        head = fh.read(cap)
    head = head[:head.rfind(b"\n") + 1]
    tmp = path + ".__reorder_cut"
    with open(tmp, "wb") as fh:
        fh.write(head)
    try:
        return dtz.read_any(tmp).normalise()
    finally:
        os.unlink(tmp)


def main():
    max_mb = float(os.environ.get("PROBE_MAX_MB", "16"))
    print(f"{'table':<34}{'polypress':>11}{'stridexz':>11}"
          f"{'+sort+perm':>12}{'+sort FREE':>12}   closes")
    for path in sys.argv[1:]:
        t = load(path, max_mb)
        pp = len(fast.encode(t))
        plain = len(codec.encode(t, **BEST))

        st, perm = sorted_table(t)
        free = len(codec.encode(st, **BEST))

        # The permutation as an extra integer column, so the planes path packs
        # it exactly as it would any other integer.
        withperm = dtz.Table(list(st.columns) + ["__perm"],
                             [r + [str(p)] for r, p in zip(st.rows, perm)])
        blob = codec.encode(withperm, **BEST)
        kept = len(blob)

        back = codec.decode(blob)
        restored = [None] * len(t.rows)
        for row in back.rows:
            restored[int(row[-1])] = row[:-1]
        ok = restored == t.rows

        gap = pp - plain                       # negative: stridexz is behind
        closed = (plain - kept) / (plain - pp) * 100 if plain != pp else 0.0
        print(f"{os.path.basename(path)[:32]:<34}{pp:>11,}{plain:>11,}"
              f"{kept:>12,}{free:>12,}   {closed:>5.0f}%"
              f"{'' if ok else '   ROUND-TRIP FAILED'}")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
