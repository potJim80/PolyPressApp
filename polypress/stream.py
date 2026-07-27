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

    python3 stream.py compress big.csv -o big.ppz --budget 1.0
    python3 stream.py restore  big.ppz -o back.csv
    python3 stream.py info     big.ppz
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


def _reader(path: str):
    ext = os.path.splitext(path)[1].lower()
    fh = open(path, newline="", encoding="utf-8", errors="replace")
    delimiter = dtz.EXT_DELIMITER.get(ext)
    if delimiter is None:
        delimiter = dtz._sniff_delimiter(fh.read(65536))
        fh.seek(0)
    return fh, csv.reader(fh, delimiter=delimiter)


def plan_rows(path: str, budget_gb: float, verify: bool = True) -> tuple:
    """Rows per block that keep peak memory near the budget.

    Estimated from the real average row width of the file rather than a
    guess, because a 209-column survey row and a 5-column sensor row differ
    by two orders of magnitude."""
    size = os.path.getsize(path)
    fh, rd = _reader(path)
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


def _blocks(path: str, rows_per_block: int) -> Iterator[dtz.Table]:
    fh, rd = _reader(path)
    try:
        header = next(rd, None)
        if header is None:
            return
        width = len(header)
        batch: List[List[str]] = []
        for row in rd:
            if len(row) != width:            # ragged rows, same rule as dtz
                row = (row + [""] * width)[:width]
            batch.append(row)
            if len(batch) >= rows_per_block:
                yield dtz.Table(list(header), batch)
                batch = []
        if batch or True:
            yield dtz.Table(list(header), batch)
    finally:
        fh.close()


def compress(src: str, dst: str, budget_gb: float = DEFAULT_BUDGET_GB,
             rows: Optional[int] = None, verify: bool = True,
             progress=None) -> dict:
    if rows is None:
        rows, per_row, est, _ = plan_rows(src, budget_gb, verify)
    else:
        per_row, est = 0.0, 0
    sizes: List[int] = []
    total_rows = 0
    columns: List[str] = []

    with open(dst, "wb") as out:
        out.write(b"\0" * 8)                 # header length patched at the end
        for i, table in enumerate(_blocks(src, rows)):
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


def restore(src: str, dst: str, progress=None) -> dict:
    with open(src, "rb") as fh:
        header = _read_header(fh)
    ext = os.path.splitext(dst)[1].lower()
    delim = dtz.EXT_DELIMITER.get(ext, ",")
    written = 0
    with open(dst, "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out, delimiter=delim, lineterminator="\n")
        first = True
        for table in iter_blocks(src):
            if first:
                w.writerow(table.columns)
                first = False
            w.writerows(table.rows)
            written += len(table.rows)
            if progress:
                progress(written)
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

        st = compress(a.path, dst, a.budget, a.rows, not a.no_verify, prog)
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
