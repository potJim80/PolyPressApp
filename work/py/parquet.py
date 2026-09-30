"""Parquet <-> CSV, for the one format the C program cannot read.

Parquet is a binary columnar container with its own encodings and
compressors; reading it properly means the Arrow library, and Arrow's C API
is a far heavier dependency than this whole project. So Parquet stays on the
Python side, as a converter at the edge, and everything else is the C
program:

    python3 py/parquet.py to-csv   data.parquet  [out.csv | -]
    python3 py/parquet.py from-csv [in.csv | -]  out.parquet

    python3 py/parquet.py to-csv data.parquet | polypress compress - -o data.ppz
    polypress restore data.ppz -o - | python3 py/parquet.py from-csv - out.parquet

Cells are written by Arrow's own CSV writer, in C++, a row group at a time
-- ten times faster than formatting them in Python, which took six minutes
on a 1 GB file. So a double prints shortest-round-trip without a spare ".0"
(63.0 -> 63, 1.0333333333333334 as is), nulls are empty cells, timestamps
ISO-like, strings quoted. Reading CSV back into Parquet keeps every column
a string, so no cell is reinterpreted on the way.

    --progress   report "progress <row groups done> <total>" on stderr

Needs pyarrow (`pip install pyarrow`).
"""

from __future__ import annotations

import csv
import datetime
import decimal
import io
import json
import sys


def _need_pyarrow():
    try:
        import pyarrow            # noqa: F401
        import pyarrow.parquet    # noqa: F401
    except ImportError:
        sys.exit("parquet.py: this needs pyarrow -- pip install pyarrow")
    import pyarrow as pa
    import pyarrow.parquet as pq
    return pa, pq


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (datetime.timedelta, decimal.Decimal)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False,
                          default=str)
    except TypeError:
        return str(value)


def to_csv(src: str, dst: str, progress: bool = False) -> int:
    _, pq = _need_pyarrow()
    import pyarrow.csv as pc
    pf = pq.ParquetFile(src)
    total = pf.num_row_groups
    out = sys.stdout.buffer if dst == "-" else open(dst, "wb")
    try:
        # a row group at a time, so a large file is never whole in memory
        for i in range(total):
            t = pf.read_row_group(i)
            pc.write_csv(t, out, pc.WriteOptions(include_header=(i == 0),
                                                 quoting_style="needed"))
            if progress:
                sys.stderr.write("progress {} {}\n".format(i + 1, total))
                sys.stderr.flush()
        if total == 0:        # no row groups: still write the header
            header = ",".join(_quote(n) for n in pf.schema_arrow.names)
            out.write((header + "\n").encode("utf-8"))
    finally:
        if out is not sys.stdout.buffer:
            out.close()
    return 0


def _quote(name: str) -> str:
    if any(c in name for c in ',"\n\r'):
        return '"' + name.replace('"', '""') + '"'
    return name


def from_csv(src: str, dst: str, progress: bool = False) -> int:
    pa, pq = _need_pyarrow()
    import pyarrow.csv as pc
    # read the header ourselves, so every column can be declared a string
    fh = sys.stdin.buffer if src == "-" else open(src, "rb")
    try:
        first = fh.readline()
        header = next(csv.reader([first.decode("utf-8")]), None)
        if header is None:
            sys.exit("parquet.py: {} is empty".format(src))
        if len(set(header)) != len(header):
            sys.exit("parquet.py: Parquet cannot hold two columns with the "
                     "same name; write .csv instead")
        schema = pa.schema([(c, pa.string()) for c in header])
        reader = pc.open_csv(
            fh,
            read_options=pc.ReadOptions(column_names=header, block_size=16 << 20),
            parse_options=pc.ParseOptions(newlines_in_values=True),
            convert_options=pc.ConvertOptions(
                column_types={c: pa.string() for c in header},
                strings_can_be_null=False, quoted_strings_can_be_null=False))
        writer = pq.ParquetWriter(dst, schema, compression="zstd")
        try:
            done = 0
            for batch in reader:
                writer.write_table(pa.Table.from_batches([batch], schema=schema))
                done += batch.num_rows
                if progress:
                    sys.stderr.write("stage writing {}\n".format(done))
                    sys.stderr.flush()
        finally:
            writer.close()
    finally:
        if fh is not sys.stdin.buffer:
            fh.close()
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    progress = "--progress" in argv
    argv = [a for a in argv if a != "--progress"]
    if len(argv) >= 2 and argv[0] == "to-csv":
        return to_csv(argv[1], argv[2] if len(argv) > 2 else "-", progress)
    if len(argv) == 3 and argv[0] == "from-csv":
        return from_csv(argv[1], argv[2], progress)
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
