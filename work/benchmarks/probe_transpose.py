#!/usr/bin/env python3
"""Probe: does xz do better if the CSV is fed to it DOWN the columns?

The question this isolates: probe_numberise conflated two changes -- stacking
by column AND replacing values with numbers. This changes only the order. The
bytes handed to xz are the same cell strings, just written column by column
instead of row by row.

  xz -9e            the original CSV, rows across          (baseline)
  brotli -q11       the original CSV, rows across          (competitor)
  parquet+zstd22    columnar, typed                        (competitor)
  polypress         the real codec                         (ours)
  T-csv + xz        the table TRANSPOSED, still CSV, then xz
  T-lines + xz      each column's cells one per line, no commas, then xz

T-csv and T-lines are not codecs -- neither carries the escaping needed to
get the original file back in every case. They are a ceiling measurement.
"""
import io
import lzma
import os
import subprocess
import sys
import time

sys.path.insert(0, "/Users/mahdiakbarin/Desktop/polypress/work")
from polypress import dtz, fast  # noqa: E402

XZ = dict(format=lzma.FORMAT_XZ,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])


def timed(fn):
    t0 = time.time()
    out = fn()
    return out, time.time() - t0


def xz_bytes(data: bytes) -> bytes:
    return lzma.compress(data, **XZ)


def brotli_bytes(data: bytes) -> bytes:
    return subprocess.run(["brotli", "-q", "11", "-c"], input=data,
                          stdout=subprocess.PIPE, check=True).stdout


def transpose_csv(t) -> bytes:
    """Same cells, written as CSV with the grid turned on its side."""
    flipped = dtz.Table(
        [str(i) for i in range(t.shape[0])],
        [[t.columns[c]] + [row[c] for row in t.rows]
         for c in range(len(t.columns))],
    )
    return dtz.canonical_csv(flipped)


def transpose_lines(t) -> bytes:
    """Each column's cells, one per line, columns back to back. No commas."""
    out = io.BytesIO()
    for c in range(len(t.columns)):
        out.write(t.columns[c].encode("utf-8", "surrogatepass"))
        out.write(b"\n")
        for row in t.rows:
            out.write(row[c].encode("utf-8", "surrogatepass"))
            out.write(b"\n")
    return out.getvalue()


def parquet_zstd(t):
    import pyarrow as pa
    import pyarrow.parquet as pq
    arrays = {}
    for i, name in enumerate(t.columns):
        arrays[f"{name}__{i}"] = pa.array(t.column(i), type=pa.string())
    tbl = pa.table(arrays)
    buf = io.BytesIO()
    pq.write_table(tbl, buf, compression="zstd", compression_level=22)
    return buf.getvalue()


def load(path, max_mb):
    cap = int(max_mb * 1024 * 1024)
    if os.path.getsize(path) <= cap:
        return dtz.read_any(path).normalise(), None
    with open(path, "rb") as fh:
        head = fh.read(cap)
    head = head[:head.rfind(b"\n") + 1]
    tmp = path + ".__probe_cut"
    with open(tmp, "wb") as fh:
        fh.write(head)
    try:
        return dtz.read_any(tmp).normalise(), len(head)
    finally:
        os.unlink(tmp)


def run(path, max_mb):
    t, cut = load(path, max_mb)
    canon = dtz.canonical_csv(t)
    rows = []

    blob, s = timed(lambda: xz_bytes(canon))
    rows.append(("xz -9e (rows across)", len(blob), s))

    blob, s = timed(lambda: brotli_bytes(canon))
    rows.append(("brotli -q11 (rows across)", len(blob), s))

    blob, s = timed(lambda: parquet_zstd(t))
    rows.append(("parquet + zstd-22", len(blob), s))

    blob, s = timed(lambda: fast.encode(t))
    rows.append(("polypress", len(blob), s))

    tc, s0 = timed(lambda: transpose_csv(t))
    blob, s = timed(lambda: xz_bytes(tc))
    rows.append(("TRANSPOSED csv + xz", len(blob), s + s0))
    del tc

    tl, s0 = timed(lambda: transpose_lines(t))
    blob, s = timed(lambda: xz_bytes(tl))
    rows.append(("TRANSPOSED lines + xz", len(blob), s + s0))
    del tl

    return t.shape, len(canon), cut, rows


def main():
    max_mb = float(os.environ.get("PROBE_MAX_MB", "16"))
    for path in sys.argv[1:]:
        shape, raw, cut, rows = run(path, max_mb)
        note = f" (truncated to {cut / 1e6:.1f} MB)" if cut else ""
        print(f"\n{os.path.basename(path)}  {shape[0]:,} rows x {shape[1]} "
              f"cols, {raw:,} bytes as CSV{note}")
        print(f"   {'method':<28}{'bytes':>12}{'ratio':>9}{'secs':>9}")
        for name, size, secs in sorted(rows, key=lambda r: r[1]):
            print(f"   {name:<28}{size:>12,}{raw / size:>8.2f}x{secs:>9.1f}")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
