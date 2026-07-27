"""Round-trip the polynomial codec on every test table and compare against
the standard general-purpose compressors.
"""

from __future__ import annotations

import bz2
import gzip
import lzma

import codec
import tables


def run(table) -> None:
    csv_text = table.to_csv()
    raw = csv_text.encode("utf-8")

    blob, report = codec.encode(csv_text)
    restored = codec.decode(blob)
    assert restored == csv_text, "ROUND TRIP FAILED for " + table.name

    sizes = [
        ("raw csv", len(raw)),
        ("gzip -9", len(gzip.compress(raw, 9))),
        ("bzip2 -9", len(bz2.compress(raw, 9))),
        ("xz -9", len(lzma.compress(raw, preset=9))),
        ("poly codec", len(blob)),
        ("poly + xz", len(lzma.compress(blob, preset=9))),
    ]

    rows, cols = table.shape
    print("=" * 62)
    print("{}  ({} rows x {} cols)".format(table.name, rows, cols))
    print(table.note)
    print()
    for label, size in sizes:
        ratio = len(raw) / size
        bar = "#" * int(round(38 * size / len(raw)))
        print("  {:<11} {:>7} B  {:>5.2f}x  {}".format(label, size, ratio, bar))

    print("\n  per-column predictor order chosen:")
    for name, order, nbytes in report:
        if order is None:
            print("    {:<14} text fallback".format(name))
        else:
            print("    {:<14} order {}   {:>7.0f} B".format(name, order, nbytes))
    print()


def main() -> None:
    print("\nlossless round-trip verified for every table below\n")
    for table in tables.all_tables():
        run(table)


if __name__ == "__main__":
    main()
