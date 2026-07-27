"""Where the codec breaks. Two honest stress tests.

1. Row order. Smoothness is a property of the row *sequence*, not the data.
   Shuffle the rows and the polynomial predictor has nothing to extrapolate.
2. Printed precision. Fixed-decimal tables map onto small integers. Columns
   printed at full double precision do not, and the residuals stop being small.
"""

from __future__ import annotations

import lzma
import math
import random

import codec
import tables


def sizes(csv_text: str):
    raw = csv_text.encode("utf-8")
    blob, report = codec.encode(csv_text)
    assert codec.decode(blob) == csv_text, "round trip failed"
    return len(raw), len(lzma.compress(raw, preset=9)), len(blob), report


def report_line(label: str, csv_text: str) -> None:
    raw, xz, poly, report = sizes(csv_text)
    orders = ",".join("-" if o is None else str(o) for _, o, _ in report)
    print("  {:<26} raw {:>6}  xz {:>6} ({:>5.2f}x)  poly {:>6} ({:>5.2f}x)  orders [{}]".format(
        label, raw, xz, raw / xz, poly, raw / poly, orders))


def test_row_order() -> None:
    print("\n1. Row order sensitivity")
    print("   (same bytes of information, only the row sequence changes)\n")
    for table in (tables.saturated_steam(), tables.blackbody()):
        report_line(table.name + " (sorted)", table.to_csv())
        shuffled = tables.Table(
            table.name, table.note, table.columns, list(table.rows))
        random.Random(3).shuffle(shuffled.rows)
        report_line(table.name + " (shuffled)", shuffled.to_csv())
        print()


def test_precision() -> None:
    print("2. Printed-precision sensitivity")
    print("   (identical underlying function, different number of decimals)\n")
    for decimals in (2, 4, 6, 8, 12, 17):
        rows = []
        for i in range(400):
            t = 5.0 + 0.5 * i
            p = 10.0 ** (8.07131 - 1730.63 / (233.426 + t)) * 0.1333224
            h = 2500.9 + 1.82 * t - 0.0015 * t * t
            rows.append([
                "{:.{d}f}".format(t, d=decimals),
                "{:.{d}f}".format(p, d=decimals),
                "{:.{d}f}".format(h, d=decimals),
            ])
        table = tables.Table("steam", "", ["T_C", "P_kPa", "h_vap"], rows)
        report_line("{} decimals".format(decimals), table.to_csv())


def test_noise_floor() -> None:
    print("\n3. Noise floor")
    print("   (smooth signal + gaussian noise of growing amplitude, 3 decimals)\n")
    for sigma in (0.0, 0.001, 0.01, 0.1, 1.0, 10.0):
        rng = random.Random(5)
        rows = []
        for i in range(500):
            x = 0.05 * i
            value = 12.0 + 3.4 * math.sin(0.11 * x) + 0.02 * x * x
            rows.append([
                "{:.2f}".format(x),
                "{:.3f}".format(value + rng.gauss(0.0, sigma)),
            ])
        table = tables.Table("signal", "", ["t", "y"], rows)
        report_line("sigma = {:g}".format(sigma), table.to_csv())


def main() -> None:
    test_row_order()
    test_precision()
    test_noise_floor()
    print()


if __name__ == "__main__":
    main()
