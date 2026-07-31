"""Measure what it would cost to stop assuming the modelled encoding wins.

    python3 benchmarks/probe_fallback_gate.py corpus100/*.csv --limit 24

`fast.encode` only builds the plain xz/bzip2 fallbacks when **no** modelling
trick fired, on the assumption that a table where one fired cannot lose to
them. That assumption is false on 3 of 100 unselected datasets, once by 28.1%,
so invariant 2 -- "never worse, *measured*, not assumed" -- does not hold.

Running both fallbacks unconditionally would fix it and is not free: the
canonical CSV has to be built, compressed at xz preset 9e and bzip2 -9, and
parsed back for the round-trip check. This script prices that, and prices the
alternative: a cheap LZMA preset-1 probe used as a *nominator*, with the real
compression kept as the decider -- the pattern `_probe_len` already follows
elsewhere in the codec.

The gate would be: build the real fallbacks only when

    probe1(canonical) * DEN < modelled_bytes * NUM

The rule is sound as long as `r = probe1(canonical) / xz9e(canonical)` never
exceeds `NUM/DEN`, because if the fallback really is smaller
(xz9e < modelled) then probe1 = r * xz9e < (NUM/DEN) * modelled. So the number
this script actually has to establish is **the maximum observed r**, and the
threshold has to be set above it with margin. Everything else here is cost.
"""

from __future__ import annotations

import argparse
import bz2
import lzma
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polypress import dtz, fast


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="probe_fallback_gate")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--limit", type=int, default=24)
    ap.add_argument("--max-mb", type=float, default=30.0)
    a = ap.parse_args(argv)

    paths = [p for p in a.paths if os.path.getsize(p) <= a.max_mb * 1e6]
    paths.sort(key=os.path.getsize)
    # spread across the size range rather than taking the smallest N
    if len(paths) > a.limit:
        step = len(paths) / float(a.limit)
        paths = [paths[int(i * step)] for i in range(a.limit)]

    print("{:<44} {:>10} {:>10} {:>10} {:>6} {:>7} {:>7} {:>7}".format(
        "dataset", "modelled", "xz9e", "probe1", "r", "t_enc", "t_full",
        "t_probe"))
    print("-" * 110)

    worst_r = 0.0
    rows = []
    for p in paths:
        try:
            t = dtz.read_any(p)
        except Exception as exc:
            print("{:<44} SKIP {}".format(os.path.basename(p)[:44],
                                          str(exc)[:40]))
            continue

        t0 = time.time()
        blob = fast.encode(t)
        t_enc = time.time() - t0

        t0 = time.time()
        canon = fast._canonical_bytes(t)
        xz = lzma.compress(canon, **fast.XZ)
        bz = bz2.compress(canon, 9)
        t_full = time.time() - t0

        t0 = time.time()
        canon2 = fast._canonical_bytes(t)
        probe = lzma.compress(canon2, **fast._PROBE)
        t_probe = time.time() - t0

        r = len(probe) / float(len(xz))
        worst_r = max(worst_r, r)
        best_fb = min(len(xz), len(bz)) + 4
        rows.append((os.path.basename(p), len(blob), len(xz), len(probe), r,
                     t_enc, t_full, t_probe, best_fb))
        print("{:<44} {:>10,} {:>10,} {:>10,} {:>6.2f} {:>6.1f}s {:>6.1f}s "
              "{:>6.1f}s".format(os.path.basename(p)[:44], len(blob), len(xz),
                                 len(probe), r, t_enc, t_full, t_probe))
        del t, blob, canon, canon2, xz, bz, probe

    if not rows:
        return 1

    print("\n{} datasets".format(len(rows)))
    print("worst probe1/xz9e ratio r = {:.3f}   (the gate must sit above this)"
          .format(worst_r))

    te = sum(x[5] for x in rows)
    tf = sum(x[6] for x in rows)
    tp = sum(x[7] for x in rows)
    print("\ncost of always running the real fallbacks : +{:.0f}% encode time"
          .format(100.0 * tf / te))
    print("cost of running only the preset-1 probe   : +{:.0f}% encode time"
          .format(100.0 * tp / te))

    # How often would each policy do the expensive thing, and does the gate
    # ever miss a real win?
    for num, den in ((3, 2), (2, 1), (5, 2), (3, 1)):
        fires = sum(1 for x in rows if x[3] * den < x[1] * num)
        misses = sum(1 for x in rows
                     if x[8] < x[1] and not (x[3] * den < x[1] * num))
        print("  gate at {}/{} = {:.2f}x : builds the fallback on {:>2}/{} "
              "datasets, MISSES {}".format(num, den, num / den, fires,
                                           len(rows), misses))
    wins = [x for x in rows if x[8] < x[1]]
    print("\nfallback would actually win on {} of {} datasets here"
          .format(len(wins), len(rows)))
    for x in wins:
        print("  {:<44} modelled {:,} vs fallback {:,}  ({:+.1f}%)".format(
            x[0][:44], x[1], x[8], 100.0 * (x[1] - x[8]) / x[8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
