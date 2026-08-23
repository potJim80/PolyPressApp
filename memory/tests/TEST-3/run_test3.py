#!/usr/bin/env python3
"""TEST-3 -- Huffman over VALUES against plain xz. 10 real columns.

The proposal: forget the permutation and the pancake. Treat each distinct
cell value as one Huffman symbol and let the tree carry the code assignment.

Variants, all decoded and compared value by value before counting:

  A  xz-9e        plain `xz -9e` on the column text, one value per line
  B  huff-val     canonical Huffman over VALUES; alphabet xz'd, lengths xz'd
  B' huff-val-raw the same with the alphabet NOT xz'd -- shows how much of the
                  container is the dictionary rather than the codes
  C  huff-byte    canonical Huffman over BYTES -- the ordinary reading
  D  rANS-val     rANS order-0 over the same value codes; same model, a coder
                  that can spend fractional bits. Isolates Huffman's loss
  E  dict+xz      dictionary + fixed-width code planes, both xz'd (TEST-2's
                  reference, the thing to beat)

Run from work/:  python3 ../memory/tests/TEST-3/run_test3.py
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "TEST-2"))

import huff                                    # noqa: E402
import rans                                    # noqa: E402
from harvest import harvest                    # noqa: E402
from test2 import flatten, dict_encode, dict_decode, order0_floor  # noqa: E402

TARGET = int(os.environ.get("TEST3_N", "10"))
PATTERN = os.environ.get("TEST3_GLOB", "../IN/corpus500/*.csv")
CAP = int(os.environ.get("TEST3_CAP", "400000"))

VARIANTS = ("xz-9e", "huff-val", "huff-val-raw", "huff-byte",
            "rANS-val", "dict+xz")


def value_entropy(values):
    """n*H(value) in bytes -- the floor any order-0 coder over values obeys.
    Huffman cannot go below it; the difference is Huffman's whole-bit loss."""
    n = len(values)
    if not n:
        return 0.0
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    bits = -sum(c * math.log2(c / n) for c in counts.values())
    return bits / 8.0


def rans_values(values):
    """Same model as huff-val, coded with rANS instead. Byte planes, because
    rANS works on a byte alphabet."""
    return dict_encode(values, "ans")


def measure(name, kind, values):
    raw = flatten(values)
    r = {"name": name, "kind": kind, "n": len(values), "k": len(set(values)),
         "raw": len(raw), "vfloor": value_entropy(values),
         "bfloor": order0_floor(raw)}

    a = huff.xz(raw)
    assert huff.unxz(a) == raw
    r["xz-9e"] = len(a)

    b = huff.huff_values(values, xz_alphabet=True)
    assert huff.unhuff_values(b, xz_alphabet=True) == values, f"{name} huff-val"
    r["huff-val"] = len(b)

    b2 = huff.huff_values(values, xz_alphabet=False)
    assert huff.unhuff_values(b2, xz_alphabet=False) == values
    r["huff-val-raw"] = len(b2)

    c = huff.huff_bytes(raw)
    assert huff.unhuff_bytes(c) == raw, f"{name} huff-byte"
    r["huff-byte"] = len(c)

    d = rans_values(values)
    assert dict_decode(d, "ans") == values, f"{name} rANS-val"
    r["rANS-val"] = len(d)

    e = dict_encode(values, "xz")
    assert dict_decode(e, "xz") == values
    r["dict+xz"] = len(e)
    return r


def main():
    sys.stderr.write(f"harvesting {TARGET} columns\n")
    cols = harvest(PATTERN, TARGET, per_file=2, max_bytes=CAP)
    sys.stderr.write(f"{len(cols)} columns\n")

    print(f"# TEST-3  Huffman over VALUES vs plain xz, {len(cols)} real "
          f"columns, capped {CAP:,} B")
    print("# Every variant decoded and compared value by value before "
          "counting.")
    print("# Sizes are whole containers: alphabet, code lengths and stream "
          "all charged.")
    print()

    hdr = (f"{'column':<34}{'kind':<12}{'n':>7}{'k':>6}" +
           "".join(f"{v:>13}" for v in VARIANTS) + f"{'best/xz':>9}")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for name, kind, vals in cols:
        r = measure(name, kind, vals)
        rows.append(r)
        best = min(r[v] for v in VARIANTS if v != "xz-9e")
        print(f"{r['name'][:33]:<34}{r['kind']:<12}{r['n']:>7}{r['k']:>6}" +
              "".join(f"{r[v]:>13,}" for v in VARIANTS) +
              f"{best / r['xz-9e']:>9.3f}", flush=True)

    print()
    tot = {v: sum(r[v] for r in rows) for v in VARIANTS}
    axz = tot["xz-9e"]
    print(f"{'TOTALS':<34}{'':<12}{'':>7}{'':>6}" +
          "".join(f"{tot[v]:>13,}" for v in VARIANTS))
    print(f"{'vs xz':<34}{'':<12}{'':>7}{'':>6}" +
          "".join(f"{tot[v] / axz:>13.3f}" for v in VARIANTS))
    print(f"{'columns beating xz':<34}{'':<12}{'':>7}{'':>6}" +
          "".join(f"{sum(1 for r in rows if r[v] < r['xz-9e']):>10}/"
                  f"{len(rows):<2}" for v in VARIANTS))

    print("\nWHERE THE BYTES GO IN huff-val")
    print(f"  {'column':<34}{'alphabet xz':>13}{'lengths':>10}"
          f"{'bitstream':>11}{'stream/vfloor':>15}")
    for r, (name, kind, vals) in zip(rows, cols):
        blob = huff.huff_values(vals, xz_alphabet=True)
        parts = huff._uncontainer(blob)
        stream = len(parts[3])
        print(f"  {r['name'][:33]:<34}{len(parts[1]):>13,}"
              f"{len(parts[2]):>10,}{stream:>11,}"
              f"{stream / r['vfloor'] if r['vfloor'] else 0:>15.3f}")

    print("\nFLOORS -- what each scheme is allowed to reach")
    print(f"  {'column':<34}{'value H':>12}{'byte H':>12}{'xz':>11}"
          f"{'huff-val':>11}{'huffloss':>10}")
    for r in rows:
        print(f"  {r['name'][:33]:<34}{r['vfloor']:>12,.0f}"
              f"{r['bfloor']:>12,.0f}{r['xz-9e']:>11,}{r['huff-val']:>11,}"
              f"{r['huff-val'] / r['vfloor'] if r['vfloor'] else 0:>10.3f}")
    tv = sum(r["vfloor"] for r in rows)
    tb = sum(r["bfloor"] for r in rows)
    print(f"  {'TOTAL':<34}{tv:>12,.0f}{tb:>12,.0f}{axz:>11,}"
          f"{tot['huff-val']:>11,}{tot['huff-val'] / tv:>10.3f}")
    print(f"\n  value entropy / xz = {tv / axz:.3f}   "
          f"(below 1.0 means the VALUE floor is under xz -- the idea has room)")


if __name__ == "__main__":
    main()
