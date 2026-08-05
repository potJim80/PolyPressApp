"""Tests the literal idea: replace a row of n values with the polynomial that
passes through (1, y1) ... (n, yn), and store the coefficients instead.

Uses exact rational arithmetic so the accounting cannot be blamed on floats.
The question is only: do the coefficients take fewer bits than the values?
"""

from __future__ import annotations

from fractions import Fraction
from typing import List, Sequence

import tables


def interpolating_coeffs(ys: Sequence[Fraction]) -> List[Fraction]:
    """Exact coefficients of the unique degree<=n-1 polynomial through
    (1, ys[0]), (2, ys[1]), ... Returned lowest-order first.

    Built by Newton's forward-difference form, then expanded to the monomial
    basis. Newton's form is the discrete Taylor series: the k-th finite
    difference stands in for the k-th derivative.
    """
    n = len(ys)
    # divided differences on the integer grid x = 1..n
    coef = [Fraction(y) for y in ys]
    for level in range(1, n):
        for i in range(n - 1, level - 1, -1):
            coef[i] = (coef[i] - coef[i - 1]) / Fraction(level)

    # expand sum_k coef[k] * prod_{j<k} (x - (j+1)) into monomial basis
    out = [Fraction(0)] * n
    basis = [Fraction(1)] + [Fraction(0)] * (n - 1)  # product so far
    for k in range(n):
        for j in range(n):
            if basis[j]:
                out[j] += coef[k] * basis[j]
        if k < n - 1:
            root = Fraction(k + 1)
            new = [Fraction(0)] * n
            for j in range(n - 1):
                if basis[j]:
                    new[j + 1] += basis[j]        # x * term
                    new[j] -= basis[j] * root     # -root * term
            basis = new
    return out


def bits_for_int(v: int) -> int:
    return max(1, abs(v).bit_length()) + 1  # magnitude + sign bit


def bits_for_fraction(f: Fraction) -> int:
    return bits_for_int(f.numerator) + bits_for_int(f.denominator)


def analyse_row(cells: Sequence[str], decimals: Sequence[int]):
    """Compare storing the row's integers vs the exact interpolant."""
    ints = []
    for cell, dec in zip(cells, decimals):
        ints.append(int(round(float(cell) * 10 ** dec)))

    raw_bits = sum(bits_for_int(v) for v in ints)
    coeffs = interpolating_coeffs([Fraction(v) for v in ints])
    poly_bits = sum(bits_for_fraction(c) for c in coeffs)
    return raw_bits, poly_bits, coeffs


def main() -> None:
    table = tables.saturated_steam()
    decimals = [1, 4, 2, 2, 5]

    print("Literal idea: one interpolating polynomial per row")
    print("Table: saturated_steam, 5 columns\n")
    print("  row   raw bits   poly bits   ratio")
    for idx in (0, 1, 100, 399):
        raw, poly, _ = analyse_row(table.rows[idx], decimals)
        print("  {:>3}   {:>8}   {:>9}   {:>5.1f}x worse".format(
            idx, raw, poly, poly / raw))

    print("\nHow it scales with the number of columns")
    print("(same trick applied to the first k values of one long smooth row)\n")
    wide = [Fraction(int(round(float(r[1]) * 10 ** 4))) for r in table.rows[:24]]
    print("    n   raw bits   poly bits   ratio")
    for n in (4, 8, 12, 16, 20, 24):
        ys = wide[:n]
        raw = sum(bits_for_int(int(y)) for y in ys)
        poly = sum(bits_for_fraction(c) for c in interpolating_coeffs(ys))
        print("  {:>3}   {:>8}   {:>9}   {:>5.1f}x".format(n, raw, poly, poly / raw))

    print("\nWhy: interpolation is an invertible linear map (the Vandermonde")
    print("matrix). n values in, n coefficients out, no information removed.")
    print("The inverse has large denominators, so the coefficients need MORE")
    print("bits than the values did. Exact interpolation can never compress.")


if __name__ == "__main__":
    main()
