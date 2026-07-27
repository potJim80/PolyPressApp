"""Generate tables designed to make Polypress lose.

    python3 benchmarks/make_hostile.py outdir/

The README claims a win "on every table tested so far" across six datasets,
and then says plainly that six is not a claim and that it needs "tables that
are hostile to it". This builds those. Every case here targets one specific
assumption in the codec, so a loss is informative rather than embarrassing --
it tells you which assumption failed.

What each case attacks:

  random_text        no structure at all. Nothing to predict, nothing to
                     dictionary-encode. The floor: we should roughly tie the
                     general compressors, never lose badly.
  uuid_keys          high-cardinality strings. Defeats dictionary encoding --
                     len(uniq)*2 > nrows, so classify() falls to "text".
  base64_blob        already-compressed bytes. Nobody can win; the question is
                     whether our container overhead makes us lose.
  random_floats      full-precision floats with no polynomial structure. The
                     finite-difference predictor should find k=0 and residuals
                     should not blow up. If diff_order picks k>0 here it is
                     fooling itself.
  shuffled_cats      dictionary columns with NO cross-column correlation. The
                     parent search should find nothing and cost us nothing.
  anticorrelated     columns that look correlated on a sample but are not,
                     which is exactly what the Miller-Madow correction exists
                     to catch.
  wide_random        200 numeric columns, mutually independent. The O(cols^2)
                     parent search does its most work for the least reward.
  mixed_types        a numeric column poisoned by one non-numeric cell, so the
                     whole column falls back to text. Tests the cliff.
  high_precision     many decimal places, varying per row, so no fixed scale
                     works and the 2D grouping rule should refuse to group.
  single_wide_row    one row, many columns. Every per-column overhead is paid
                     with nothing to amortise it over.

These are deliberately small -- a few MB each -- so the whole suite runs in
minutes and well inside a 1-2 GB memory budget.
"""

from __future__ import annotations

import csv
import os
import random
import string
import sys

N = 40_000            # rows for most cases; keeps each file in the low MBs
SEED = 20260727       # fixed, so results are comparable across runs


def _w(path, columns, rowgen, n=N):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(columns)
        for i in range(n):
            w.writerow(rowgen(i))
    return path


def random_text(path, rnd):
    al = string.ascii_letters + string.digits
    return _w(path, ["a", "b", "c"],
              lambda i: ["".join(rnd.choice(al) for _ in range(12))
                         for _ in range(3)])


def uuid_keys(path, rnd):
    hx = "0123456789abcdef"

    def row(i):
        u = "".join(rnd.choice(hx) for _ in range(32))
        return ["{}-{}-{}-{}-{}".format(u[:8], u[8:12], u[12:16],
                                        u[16:20], u[20:]), str(i)]
    return _w(path, ["uuid", "n"], row)


def base64_blob(path, rnd):
    al = string.ascii_letters + string.digits + "+/"
    return _w(path, ["blob"],
              lambda i: ["".join(rnd.choice(al) for _ in range(64))])


def random_floats(path, rnd):
    return _w(path, ["x", "y", "z"],
              lambda i: ["{:.6f}".format(rnd.uniform(-1e6, 1e6))
                         for _ in range(3)])


def shuffled_cats(path, rnd):
    # eight independent categorical columns, no column predicts another
    pools = [["{}{}".format(chr(65 + c), k) for k in range(50)]
             for c in range(8)]
    return _w(path, ["c{}".format(c) for c in range(8)],
              lambda i: [rnd.choice(p) for p in pools])


def anticorrelated(path, rnd):
    # `parent` has one distinct value per handful of rows, so a naive
    # conditional-entropy score reads near zero and calls it a great parent
    # even though it predicts nothing at all
    def row(i):
        return [str(i // 3), rnd.choice(["alpha", "beta", "gamma", "delta"])]
    return _w(path, ["parent", "child"], row)


def wide_random(path, rnd):
    cols = 200
    return _w(path, ["v{}".format(c) for c in range(cols)],
              lambda i: [str(rnd.randint(0, 10 ** 6)) for _ in range(cols)],
              n=4000)


def mixed_types(path, rnd):
    def row(i):
        # one poisoned cell in the middle kills the numeric parse for the
        # whole column
        return ["n/a" if i == N // 2 else str(i * 3),
                str(rnd.randint(0, 999))]
    return _w(path, ["poisoned", "clean"], row)


def high_precision(path, rnd):
    def row(i):
        d = 1 + (i % 9)
        return ["{:.{}f}".format(rnd.uniform(0, 1000), d),
                "{:.{}f}".format(rnd.uniform(0, 1000), 10 - d)]
    return _w(path, ["a", "b"], row)


def single_wide_row(path, rnd):
    cols = 5000
    return _w(path, ["c{}".format(c) for c in range(cols)],
              lambda i: [str(rnd.randint(0, 10 ** 9)) for _ in range(cols)],
              n=1)


CASES = [
    ("random_text", random_text),
    ("uuid_keys", uuid_keys),
    ("base64_blob", base64_blob),
    ("random_floats", random_floats),
    ("shuffled_cats", shuffled_cats),
    ("anticorrelated", anticorrelated),
    ("wide_random", wide_random),
    ("mixed_types", mixed_types),
    ("high_precision", high_precision),
    ("single_wide_row", single_wide_row),
]


def main(argv) -> int:
    outdir = argv[1] if len(argv) > 1 else "hostile"
    os.makedirs(outdir, exist_ok=True)
    for name, fn in CASES:
        rnd = random.Random(SEED)      # same seed per case, reproducible
        path = os.path.join(outdir, name + ".csv")
        fn(path, rnd)
        print("{:<18} {:>12,} B   {}".format(
            name, os.path.getsize(path), path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
