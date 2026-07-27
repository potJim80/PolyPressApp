"""Walk one real column through every stage of the codec, showing the bits.

Run this to see exactly where the compression comes from:

    python3 explain.py
"""

from __future__ import annotations

import lzma

import codec
import tables


def bits_of_text(cells) -> int:
    return sum(len(c.encode("utf-8")) for c in cells) * 8


def show(title: str) -> None:
    print("\n" + title)
    print("-" * len(title))


def main() -> None:
    table = tables.saturated_steam()
    col_index = 4                      # s_vap: entropy, 5 decimals
    name = table.columns[col_index]
    cells = [row[col_index] for row in table.rows]

    show("stage 0 -- the column as printed text")
    print("  column: {}   {} rows".format(name, len(cells)))
    print("  first 8: {}".format(", ".join(cells[:8])))
    print("  stored as text: {:,} bits ({:.1f} bits per value)".format(
        bits_of_text(cells), bits_of_text(cells) / len(cells)))

    show("stage 1 -- text to fixed-point integers (exact, reversible)")
    ints, decimals = codec.as_numeric_column(cells)
    print("  every cell has {} decimals, so value = integer / 10^{}".format(
        decimals, decimals))
    print("  first 8: {}".format(", ".join(str(v) for v in ints[:8])))
    raw_int_bits = sum(max(1, v.bit_length()) for v in ints)
    print("  as plain integers: {:,} bits ({:.1f} per value)".format(
        raw_int_bits, raw_int_bits / len(ints)))
    print("  already {:.2f}x smaller than the text, and now arithmetic works"
          .format(bits_of_text(cells) / raw_int_bits))

    show("stage 2 -- polynomial prediction (the residuals shrink)")
    print("  predict each value by extending a degree-k polynomial through")
    print("  the k values before it, then store only the error.\n")
    print("   order   predictor                        first 6 residuals"
          "                 total bits")
    labels = {
        0: "store the value itself",
        1: "x[i-1]                 (flat)",
        2: "2x[i-1] - x[i-2]       (line)",
        3: "3x[i-1] -3x[i-2] +x[i-3]  (parabola)",
        4: "4x[i-1] -6x[i-2] +4x[i-3] -x[i-4]",
    }
    best = None
    for order in range(5):
        res = codec.residuals(ints, order)
        cost = codec._column_cost(ints, order)
        head = ", ".join("{:>6}".format(r) for r in res[:6])
        if best is None or cost < best[1]:
            best = (order, cost)
        print("     {}     {:<33} {}   {:>8,}".format(
            order, labels[order], head, cost))
    print("\n  the residual at order k is the (k+1)-th finite difference --")
    print("  the discrete version of a Taylor remainder. It gets small exactly")
    print("  when the column is locally a low-degree polynomial.")

    order, cost = best
    print("\n  chosen: order {} at {:,} bits ({:.2f} bits per value)".format(
        order, cost, cost / len(ints)))

    show("stage 3 -- Rice coding turns small numbers into few bits")
    res = codec.residuals(ints, order)
    block = [codec.zigzag(r) for r in res[:codec.BLOCK]]
    k, block_cost = codec._best_k(block)
    print("  zigzag maps signed to unsigned:  0,-1,1,-2,2 -> 0,1,2,3,4")
    print("  Rice with parameter k writes (u >> k) in unary, then k raw bits.")
    print("  small u costs few bits; k is retuned every {} values.".format(
        codec.BLOCK))
    print("\n  first block: k={}, {} values in {} bits ({:.2f} bits each)".format(
        k, len(block), block_cost, block_cost / len(block)))
    for u in block[:6]:
        q = u >> k
        print("    u={:<5} -> {} ones + 0 + {} low bits = {} bits".format(
            u, q, k, q + 1 + k))

    show("summary for this one column")
    text_bits = bits_of_text(cells)
    print("  as text                    {:>8,} bits   1.00x".format(text_bits))
    print("  as fixed-point integers    {:>8,} bits   {:.2f}x".format(
        raw_int_bits, text_bits / raw_int_bits))
    print("  + polynomial + Rice        {:>8,} bits   {:.2f}x".format(
        cost, text_bits / cost))
    xz_bits = len(lzma.compress(
        "\n".join(cells).encode(), preset=9 | lzma.PRESET_EXTREME)) * 8
    print("  (xz -9e on the same text   {:>8,} bits   {:.2f}x)".format(
        xz_bits, text_bits / xz_bits))


if __name__ == "__main__":
    main()
