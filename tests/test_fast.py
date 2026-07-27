"""Fidelity tests for fast.py -- the codec that actually ships.

Reuses the awkward cases from test_dtz (embedded delimiters, newlines,
unicode, ragged shapes) and adds the ones that specifically stress this
codec's machinery: the numeric parser, 2D column grouping, parent-sorted
reordering, and the varint escape path.

    python3 test_fast.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from polypress import dtz, fast, caccel
from test_dtz import CASES as BASE_CASES

EXTRA = {
    # numeric edge cases -- these decide whether a column is parsed as a
    # scaled integer or falls back to text
    "leading_zeros": dtz.Table(["z"], [["007"], ["010"], ["000"]]),
    "negative_zero": dtz.Table(["z"], [["-0.0"], ["0.0"], ["-1.5"]]),
    "ragged_decimals": dtz.Table(["d"], [["1.5"], ["1.50"], ["1.500"]]),
    "big_ints": dtz.Table(
        ["n"], [[str(2 ** 62 - 1)], [str(-(2 ** 62) + 1)], ["0"]]),
    "huge_ints": dtz.Table(["n"], [["9" * 40], ["1"], ["2"]]),
    "mixed_sign": dtz.Table(
        ["v"], [["-0.01"], ["0.00"], ["0.01"], ["-99999.99"]]),
    "varint_escape": dtz.Table(
        ["v"], [[str(i * 7919)] for i in range(300)]),

    # 2D grouping: commensurable columns should be grouped, and the codec
    # must still round-trip when they are not
    "commensurable_2d": dtz.Table(
        ["a", "b", "c", "d"],
        [["{:.2f}".format(1.0 + i * 0.01 + j * 0.05) for j in range(4)]
         for i in range(200)]),
    "incommensurable": dtz.Table(
        ["year", "tiny", "huge"],
        [[str(2000 + i), "{:.3f}".format(i / 1000.0), str(i * 10 ** 9)]
         for i in range(120)]),

    # parent-sorted reordering: a child column strongly determined by a
    # parent, plus a tie-heavy parent to exercise the stable sort
    "parent_child": dtz.Table(
        ["zip", "city", "noise"],
        [["{:05d}".format(90000 + (i % 40)),
          "city{}".format(i % 40),
          "n{}".format(i % 7)] for i in range(500)]),
    "constant_parent": dtz.Table(
        ["k", "v"], [["same", "v{}".format(i % 3)] for i in range(200)]),
    "all_unique": dtz.Table(
        ["u"], [["value-{}".format(i)] for i in range(400)]),
    "single_distinct": dtz.Table(["s"], [["x"] for _ in range(100)]),
}


def check(name: str, table) -> bool:
    try:
        blob = fast.encode(table)
        back = fast.decode(blob)
    except Exception as exc:
        print("  {:<22} RAISED {}: {}".format(name, type(exc).__name__, exc))
        return False
    if back.columns != table.columns:
        print("  {:<22} COLUMN MISMATCH".format(name))
        return False
    if back.rows != table.rows:
        print("  {:<22} ROW MISMATCH".format(name))
        for i, (a, b) in enumerate(zip(table.rows, back.rows)):
            if a != b:
                print("      row {}: want {!r}".format(i, a))
                print("             got  {!r}".format(b))
                break
        else:
            print("      row count {} vs {}".format(
                len(table.rows), len(back.rows)))
        return False
    return True


def main() -> int:
    cases = dict(BASE_CASES)
    cases.update(EXTRA)
    print("C acceleration: {}".format(
        "on" if caccel.HAVE_C else "off (numpy fallback)"))
    print("{} cases\n".format(len(cases)))

    failed = [n for n, t in sorted(cases.items()) if not check(n, t)]

    # The numpy fallback must agree with the C path, or the accelerator is
    # not an accelerator -- it is a second, different codec.
    if caccel.HAVE_C:
        caccel.HAVE_C = False
        try:
            fallback = [n for n, t in sorted(cases.items()) if not check(n, t)]
        finally:
            caccel.HAVE_C = True
        if fallback:
            print("\nnumpy-fallback failures: {}".format(fallback))
            failed += ["fallback:" + n for n in fallback]
        else:
            print("numpy fallback agrees on all cases")

    if failed:
        print("\nFAILED {} of {}: {}".format(
            len(failed), len(cases) * 2, ", ".join(failed)))
        return 1
    print("\nall cases round-trip exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
