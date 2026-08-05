"""Bounded-memory compression for files larger than RAM.

The single-shot codec in fast.py holds the whole table as Python strings.
Measured expansion is about 8x -- a 291 MB CSV peaks at 2.4 GB -- so a 50 GB
file would need hundreds of gigabytes and simply will not run.

This splits the table into row blocks, compresses each independently, and
concatenates them behind an index. Peak memory is one block, not one file,
and it is settable. Blocks are independent, so this is also the thing that
makes parallelism possible later.

The cost is real and worth stating: the cross-column reordering only sees
correlations *inside* a block, so smaller blocks compress slightly worse.
Measure with `--rows` before choosing a small one.

    python3 tzip.py stream-compress big.csv -o big.ppz --budget 1.0
    python3 tzip.py stream-restore  big.ppz -o back.csv
    python3 tzip.py stream-info     big.ppz

Or as a module, which is the same code path:

    python3 -m polypress.stream compress big.csv --budget 1.0
"""

from __future__ import annotations

import argparse
import csv
import json
import lzma
import os
import sys
import time
from typing import Iterator, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from . import dtz
from . import fast

MAGIC = b"PPZS"
DEFAULT_BUDGET_GB = 1.0
# Python strings cost roughly this much more than the CSV bytes they came
# from; measured at 8.1x on NHAMCS (291 MB file -> 2.36 GB resident).
EXPANSION = 8.5
# Verification decodes the block and compares, so the original block and the
# decoded copy are both resident at once. Without this the budget lied by
# roughly 2x: asking for 1 GB gave a 2.07 GB peak.
VERIFY_FACTOR = 2.2
MIN_ROWS = 1000
MAX_ROWS = 2_000_000

csv.field_size_limit(1 << 31)


def _decode_guard(rows, path: str, enc: str):
    """Turn a lazy decode failure into the same refusal dtz raises.

    The single-shot reader decodes the whole file inside one call, so a bad
    byte surfaces there. Here the file is read block by block, so it surfaces
    hundreds of rows in -- possibly after several blocks have already been
    written. Same error, same message, wherever it lands."""
    try:
        for row in rows:
            yield row
    except UnicodeDecodeError as exc:
        raise dtz.EncodingRefused(
            dtz.encoding_error(path, exc, enc)) from None


def _reader(path: str, encoding: Optional[str] = None):
    ext = os.path.splitext(path)[1].lower()
    enc = encoding or dtz.sniff_encoding(path)
    fh = dtz.open_text(path, enc)
    try:
        delimiter = dtz.EXT_DELIMITER.get(ext)
        if delimiter is None:
            delimiter = dtz._sniff_delimiter(fh.read(65536))
            fh.seek(0)
    except UnicodeDecodeError as exc:
        fh.close()
        raise dtz.EncodingRefused(
            dtz.encoding_error(path, exc, enc)) from None
    return fh, _decode_guard(csv.reader(fh, delimiter=delimiter), path, enc)


def plan_rows(path: str, budget_gb: float, verify: bool = True,
              encoding: Optional[str] = None) -> tuple:
    """Rows per block that keep peak memory near the budget.

    Estimated from the real average row width of the file rather than a
    guess, because a 209-column survey row and a 5-column sensor row differ
    by two orders of magnitude."""
    size = os.path.getsize(path)
    fh, rd = _reader(path, encoding)
    try:
        header = next(rd, [])
        n, seen = 0, 0
        for row in rd:
            seen += sum(len(c) for c in row) + len(row)
            n += 1
            if n >= 2000:
                break
    finally:
        fh.close()
    per_row = max(1.0, seen / max(n, 1))
    factor = EXPANSION * (VERIFY_FACTOR if verify else 1.0)
    rows = int((budget_gb * (1 << 30)) / (per_row * factor))
    rows = max(MIN_ROWS, min(MAX_ROWS, rows))
    est_blocks = max(1, int(size / (per_row * rows)) + 1)
    return rows, per_row, est_blocks, header


def _blocks(path: str, rows_per_block: int,
            encoding: Optional[str] = None) -> Iterator[dtz.Table]:
    fh, rd = _reader(path, encoding)
    try:
        header = next(rd, None)
        if header is None:
            return
        width = len(header)
        batch: List[List[str]] = []
        yielded = False
        for row in rd:
            if len(row) != width:            # ragged rows, same rule as dtz
                row = (row + [""] * width)[:width]
            batch.append(row)
            if len(batch) >= rows_per_block:
                yield dtz.Table(list(header), batch)
                yielded = True
                batch = []
        # Only emit a trailing block if it holds rows. The exception is a file
        # with a header and no rows at all: that still needs one empty block,
        # or the archive would record no columns. Yielding unconditionally --
        # as an earlier `if batch or True` did -- appended a wasted ~178-byte
        # encoding of nothing whenever the row count divided evenly.
        if batch or not yielded:
            yield dtz.Table(list(header), batch)
    finally:
        fh.close()


def compress(src: str, dst: str, budget_gb: float = DEFAULT_BUDGET_GB,
             rows: Optional[int] = None, verify: bool = True,
             progress=None, encoding: Optional[str] = None) -> dict:
    if rows is None:
        rows, per_row, est, _ = plan_rows(src, budget_gb, verify, encoding)
    else:
        per_row, est = 0.0, 0
    sizes: List[int] = []
    total_rows = 0
    columns: List[str] = []

    with open(dst, "wb") as out:
        out.write(b"\0" * 8)                 # header length patched at the end
        for i, table in enumerate(_blocks(src, rows, encoding)):
            if not columns:
                columns = table.columns
            blob = fast.encode(table)
            if verify:
                back = fast.decode(blob)
                if back.columns != table.columns or back.rows != table.rows:
                    raise RuntimeError(
                        "block {} failed verification; nothing usable written"
                        .format(i))
            out.write(blob)
            sizes.append(len(blob))
            total_rows += len(table.rows)
            if progress:
                progress(i + 1, total_rows, sum(sizes))

        header = {"columns": columns, "nrows": total_rows,
                  "rows_per_block": rows, "blocks": sizes}
        hb = lzma.compress(json.dumps(header, separators=(",", ":")).encode(),
                           **fast.XZ)
        out.write(hb)
        out.write(len(hb).to_bytes(8, "big"))

    # Rewrite the magic now that the file is complete: a truncated run leaves
    # eight zero bytes at the front rather than something that looks valid.
    with open(dst, "r+b") as out:
        out.seek(0)
        out.write(MAGIC + b"\0\0\0\0")
    return {"rows": total_rows, "blocks": len(sizes),
            "rows_per_block": rows, "bytes": os.path.getsize(dst),
            "per_row": per_row}


def _read_header(fh):
    fh.seek(0)
    if fh.read(4) != MAGIC:
        raise ValueError("not a Polypress stream archive")
    fh.seek(-8, os.SEEK_END)
    hlen = int.from_bytes(fh.read(8), "big")
    fh.seek(-8 - hlen, os.SEEK_END)
    return json.loads(lzma.decompress(fh.read(hlen), **fast.XZ))


def iter_blocks(src: str) -> Iterator[dtz.Table]:
    """Yield each block's table in order, holding one block at a time."""
    with open(src, "rb") as fh:
        header = _read_header(fh)
        fh.seek(8)
        for size in header["blocks"]:
            yield fast.decode(fh.read(size))


# Restoring has to honour the output extension the same way tzip.py does --
# `restore -o out.parquet` must produce parquet, not a CSV wearing the name.
# But it cannot call dtz.write_any, which needs the whole table in memory; the
# entire point here is that the table does not fit. So each format gets a
# writer that consumes one block at a time and holds nothing else.

class _DelimitedOut:
    def __init__(self, path, delim):
        self.fh = open(path, "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.fh, delimiter=delim, lineterminator="\n")
        self.first = True

    def block(self, table):
        if self.first:
            self.w.writerow(table.columns)
            self.first = False
        self.w.writerows(table.rows)

    def close(self):
        self.fh.close()


class _JsonlOut:
    def __init__(self, path, columns):
        dtz._require_unique_columns(dtz.Table(list(columns), []), "jsonl")
        self.columns = columns
        self.path = path
        self.fh = open(path, "w", encoding="utf-8")
        self.rows = 0

    def block(self, table):
        dump = json.dumps
        for row in table.rows:
            self.fh.write(dump(dict(zip(self.columns, row)),
                               ensure_ascii=False, separators=(",", ":")))
            self.fh.write("\n")
            self.rows += 1

    def close(self):
        self.fh.close()
        if not self.rows and self.columns:
            # dtz's contract is that a FormatLimit leaves nothing behind. It
            # can check up front; we only learn the table was empty after the
            # last block, so remove the file we opened.
            os.unlink(self.path)
            raise dtz.FormatLimit(
                "jsonl cannot carry column names for a zero-row table; "
                "write .csv, .json or .parquet instead")


class _JsonOut:
    """A JSON array emitted incrementally, so the records never coexist."""

    def __init__(self, path, columns):
        dtz._require_unique_columns(dtz.Table(list(columns), []), "json")
        self.columns = columns
        self.path = path
        self.fh = open(path, "w", encoding="utf-8")
        self.rows = 0

    def block(self, table):
        dump = json.dumps
        for row in table.rows:
            self.fh.write("[\n " if not self.rows else ",\n ")
            self.fh.write(dump(dict(zip(self.columns, row)),
                               ensure_ascii=False))
            self.rows += 1

    def close(self):
        if self.rows:
            self.fh.write("\n]")
            self.fh.close()
            return
        # zero rows: a record list cannot carry headers, so use the column
        # form, exactly as dtz.write_json does
        self.fh.close()
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({c: [] for c in self.columns}, fh, ensure_ascii=False)


class _ParquetOut:
    """One row group per block -- the block structure maps straight onto it."""

    def __init__(self, path, columns):
        if dtz.pq is None:
            raise RuntimeError("writing parquet needs pyarrow installed")
        dtz._require_unique_columns(dtz.Table(list(columns), []), "parquet")
        self.columns = columns
        # Every cell is a string in a Table, so the schema is fixed up front
        # and stays identical across blocks -- which is what lets the row
        # groups append without buffering.
        self.schema = dtz.pa.schema([(c, dtz.pa.string()) for c in columns])
        self.writer = dtz.pq.ParquetWriter(path, self.schema,
                                           compression="zstd")

    def block(self, table):
        if not table.rows:
            return
        arrays = [dtz.pa.array(table.column(i))
                  for i in range(len(self.columns))]
        self.writer.write_table(dtz.pa.table(arrays, schema=self.schema))

    def close(self):
        self.writer.close()


def _open_writer(dst: str, columns: List[str]):
    ext = os.path.splitext(dst)[1].lower()
    if ext == ".tsv":
        return _DelimitedOut(dst, "\t")
    if ext == ".json":
        return _JsonOut(dst, columns)
    if ext in (".jsonl", ".ndjson"):
        return _JsonlOut(dst, columns)
    if ext in (".parquet", ".pq"):
        return _ParquetOut(dst, columns)
    return _DelimitedOut(dst, dtz.EXT_DELIMITER.get(ext, ","))


def restore(src: str, dst: str, progress=None) -> dict:
    with open(src, "rb") as fh:
        header = _read_header(fh)
    out = _open_writer(dst, header["columns"])
    written = 0
    try:
        for table in iter_blocks(src):
            out.block(table)
            written += len(table.rows)
            if progress:
                progress(written)
    finally:
        out.close()
    return {"rows": written, "blocks": len(header["blocks"]),
            "bytes": os.path.getsize(dst)}


def info(src: str) -> dict:
    with open(src, "rb") as fh:
        header = _read_header(fh)
    header["size"] = os.path.getsize(src)
    return header


# ---------------------------------------------------------------------- cli

def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "{:,.0f} {}".format(n, unit) if unit == "B" \
                else "{:,.1f} {}".format(n, unit)
        n /= 1024.0
    return str(n)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="polypress-stream",
                                 description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compress")
    c.add_argument("path")
    c.add_argument("-o", "--output")
    c.add_argument("--budget", type=float, default=DEFAULT_BUDGET_GB,
                   help="approximate peak memory in GB (default 1.0)")
    c.add_argument("--rows", type=int, help="rows per block, overrides budget")
    c.add_argument("--no-verify", action="store_true")
    c.add_argument("--encoding",
                   help="text encoding of the input (default: utf-8, or "
                        "whatever a byte-order mark says)")

    r = sub.add_parser("restore")
    r.add_argument("path")
    r.add_argument("-o", "--output")

    i = sub.add_parser("info")
    i.add_argument("path")

    a = ap.parse_args(argv)

    if a.cmd == "compress":
        dst = a.output or a.path + ".ppz"
        raw = os.path.getsize(a.path)
        t0 = time.time()

        def prog(nblk, nrows, nbytes):
            sys.stderr.write("\r  block {:>4}   {:>10,} rows   {:>10}"
                             .format(nblk, nrows, human(nbytes)))
            sys.stderr.flush()

        st = compress(a.path, dst, a.budget, a.rows, not a.no_verify, prog,
                      a.encoding)
        sys.stderr.write("\r" + " " * 60 + "\r")
        secs = time.time() - t0
        print("{:,} rows in {} blocks of {:,}   {} -> {}   {:.2f}x   "
              "{:.1f} MB/s".format(
                  st["rows"], st["blocks"], st["rows_per_block"], human(raw),
                  human(st["bytes"]), raw / max(st["bytes"], 1),
                  raw / 1e6 / max(secs, 1e-9)))
        print(dst)
        return 0

    if a.cmd == "restore":
        dst = a.output or (a.path[:-4] if a.path.lower().endswith(".ppz")
                           else a.path + ".csv")
        t0 = time.time()
        st = restore(a.path, dst)
        secs = time.time() - t0
        print("{:,} rows from {} blocks   {}   {:.1f} MB/s".format(
            st["rows"], st["blocks"], human(st["bytes"]),
            st["bytes"] / 1e6 / max(secs, 1e-9)))
        print(dst)
        return 0

    h = info(a.path)
    print("file          {}".format(a.path))
    print("size          {}".format(human(h["size"])))
    print("rows          {:,}".format(h["nrows"]))
    print("columns       {}".format(len(h["columns"])))
    print("blocks        {} of {:,} rows".format(
        len(h["blocks"]), h["rows_per_block"]))
    if h["blocks"]:
        print("block sizes   min {}  median {}  max {}".format(
            human(min(h["blocks"])),
            human(sorted(h["blocks"])[len(h["blocks"]) // 2]),
            human(max(h["blocks"]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
