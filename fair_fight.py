"""A fair fight.

The earlier benchmark compared the polynomial codec against xz applied to
row-major CSV *text*. That is a weak baseline and flatters the codec: byte
oriented LZ compressors do badly on interleaved decimal text through no fault
of their own.

This script gives the general-purpose compressors the preprocessing that real
columnar formats give them, and asks whether the polynomial part still earns
its keep:

  csv + xz              row-major text            (the strawman)
  colmajor + xz         columns concatenated      (what transposition alone buys)
  binary + xz           fixed-point ints, fixed-width little-endian, column major
  delta + xz            first differences, column major   ~ Parquet DELTA_BINARY_PACKED
  delta2 + xz           second differences, column major  ~ Gorilla delta-of-delta
  poly codec            this project
  poly + xz             this project, then xz

If "delta + xz" matches the poly codec, the polynomial machinery is decoration.
"""

from __future__ import annotations

import lzma
from typing import List, Sequence

import codec
import tables

PRESET = 9 | lzma.PRESET_EXTREME


def xz(data: bytes) -> int:
    return len(lzma.compress(data, preset=PRESET))


def numeric_columns(table) -> List[List[int]]:
    """Fixed-point integer form of every numeric column."""
    out = []
    for col in range(len(table.columns)):
        cells = [row[col] for row in table.rows]
        parsed = codec.as_numeric_column(cells)
        if parsed is not None:
            out.append(parsed[0])
    return out


def pack(values: Sequence[int]) -> bytes:
    """Fixed-width signed little-endian packing, width set by the extreme."""
    width = 1
    for v in values:
        width = max(width, (v.bit_length() // 8) + 1 if v >= 0
                    else ((-v - 1).bit_length() // 8) + 1)
    return b"".join(v.to_bytes(width, "little", signed=True) for v in values)


def diff(values: Sequence[int], order: int) -> List[int]:
    cur = list(values)
    for _ in range(order):
        cur = [cur[0]] + [cur[i] - cur[i - 1] for i in range(1, len(cur))]
    return cur


def colmajor_text(table) -> bytes:
    chunks = []
    for col in range(len(table.columns)):
        chunks.append("\n".join(row[col] for row in table.rows))
    return "\n".join(chunks).encode("utf-8")


def run(table) -> None:
    csv_text = table.to_csv()
    raw = csv_text.encode("utf-8")
    cols = numeric_columns(table)

    blob, _ = codec.encode(csv_text)
    assert codec.decode(blob) == csv_text

    results = [
        ("csv + xz", xz(raw)),
        ("colmajor + xz", xz(colmajor_text(table))),
        ("binary + xz", xz(b"".join(pack(c) for c in cols))),
        ("delta + xz", xz(b"".join(pack(diff(c, 1)) for c in cols))),
        ("delta2 + xz", xz(b"".join(pack(diff(c, 2)) for c in cols))),
        ("poly codec", len(blob)),
        ("poly + xz", xz(blob)),
    ]
    best = min(size for _, size in results)

    print("=" * 58)
    print("{}   raw {} B".format(table.name, len(raw)))
    for label, size in results:
        mark = "  <-- best" if size == best else ""
        print("  {:<15} {:>7} B   {:>6.2f}x{}".format(
            label, size, len(raw) / size, mark))
    print()


def main() -> None:
    for table in tables.all_tables():
        run(table)


if __name__ == "__main__":
    main()
