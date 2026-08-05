#!/usr/bin/env python3
"""How much room is left in the permutation?  Measure the ceiling first.

probe_sortreg established that after the sorted values are run-length
collapsed, the row numbers are 85-95% of the archive.  Before building a
cleverer coder for them, this measures what a cleverer coder could possibly
be worth.  Three numbers per column:

  FLOOR   log2( n! / prod c_v! ), the exact size of the multiset permutation.
          This is Cover's enumerative code: number every distinct arrangement
          and write down which one you have.  No model, no redundancy, and
          nothing that only reads the counts can ever beat it.  The counts are
          already stored by the RLE stage, so the decoder has them for free.

  GAP     what probe_sortreg's gap coder actually spends.
          FLOOR/GAP is the whole prize for a smarter order-0 scheme.

  ORDER-1 n * H(value | value in the previous row), in ROW order.  The floor
          is an order-0 quantity: it assumes the rows are exchangeable.  Real
          tables arrive clustered, and clustering is the ONLY thing that lets
          anything beat the floor.  FLOOR - ORDER1 is the prize for a context
          model, and it is the bigger of the two.

    python3 benchmarks/probe_permfloor.py ../IN/corpus/cdc_nndss.csv
"""
import math
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "old"))

import probe_sortreg as P                          # noqa: E402

LOG2E = 1.0 / math.log(2.0)


def floor_bits(counts, n):
    """log2( n! / prod c! ) -- exact, via lgamma."""
    v = math.lgamma(n + 1) - sum(math.lgamma(c + 1) for c in counts)
    return v * LOG2E


def order1_bits(vals, n):
    """n * H(x_i | x_{i-1}) measured in the file's own row order."""
    trans = defaultdict(Counter)
    for i in range(1, n):
        trans[vals[i - 1]][vals[i]] += 1
    total = 0.0
    for prev, nxt in trans.items():
        m = sum(nxt.values())
        for c in nxt.values():
            total -= c * math.log(c / m)
    return total * LOG2E


def main():
    cap = float(os.environ.get("PROBE_MAX_MB", "1"))
    for path in sys.argv[1:]:
        t = P.load(path, cap)[0]
        n = len(t.rows)
        print(f"\n{os.path.basename(path)}  {n:,} rows x {len(t.columns)} cols")
        print(f"   {'column':<34}{'FLOOR':>10}{'GAP':>10}{'slack':>8}"
              f"{'ORDER-1':>10}{'vs floor':>10}")
        tf = tg = to = 0
        for ci, cname in enumerate(t.columns):
            cells = t.column(ci)
            num = P.numeric_plan(cells)
            vals = num[0] if num is not None else P.dict_plan(cells, "lex")[1]
            cnt = Counter(vals)
            order = sorted(range(n), key=vals.__getitem__)
            sv = [vals[i] for i in order]

            st = P.OutStream()
            P._emit_gap(st, order, sv, n)
            gap = len(st.blob())
            fl = floor_bits(cnt.values(), n) / 8.0
            o1 = order1_bits(vals, n) / 8.0
            tf += fl
            tg += gap
            to += o1
            print(f"   {cname[:34]:<34}{fl:>10,.0f}{gap:>10,}"
                  f"{gap / fl if fl else 0:>7.2f}x{o1:>10,.0f}"
                  f"{o1 / fl if fl else 0:>9.2f}x")
        print(f"   {'TOTAL':<34}{tf:>10,.0f}{tg:>10,}{tg / tf:>7.2f}x"
              f"{to:>10,.0f}{to / tf:>9.2f}x")
        print(f"   gap coder is {tg / tf:.2f}x the enumerative floor; "
              f"an order-1 context model would reach {to / tf:.2f}x of it.")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
