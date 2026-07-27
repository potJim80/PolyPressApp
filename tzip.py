"""Polypress -- command line for the codec.

    python3 tzip.py compress data.csv              -> data.csv.ppz
    python3 tzip.py restore  data.csv.ppz          -> data.csv
    python3 tzip.py restore  data.csv.ppz -o x.parquet
    python3 tzip.py info     data.csv.ppz

Restoring writes whatever format the output extension asks for, so this
doubles as a converter. Compression verifies the round trip in memory before
writing anything.
"""

from __future__ import annotations

import argparse
import json
import lzma
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from polypress import dtz, fast

PACKED_EXT = ".ppz"
LEGACY_EXT = ".tcz"      # archives written before the rename


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "{:,.0f} {}".format(n, unit) if unit == "B" \
                else "{:,.1f} {}".format(n, unit)
        n /= 1024.0
    return str(n)


def cmd_compress(args) -> int:
    src = args.path
    dst = args.output or src + PACKED_EXT
    raw = os.path.getsize(src)

    t0 = time.time()
    table = dtz.read_any(src)
    blob = fast.encode(table)
    secs = time.time() - t0

    if not args.no_verify:
        back = fast.decode(blob)
        if back.columns != table.columns or back.rows != table.rows:
            print("verification FAILED -- nothing written", file=sys.stderr)
            return 1

    with open(dst, "wb") as fh:
        fh.write(blob)
    rows, cols = table.shape
    print("{:,} rows x {} cols   {} -> {}   {:.2f}x   {:.1f} MB/s".format(
        rows, cols, human(raw), human(len(blob)), raw / max(len(blob), 1),
        raw / 1e6 / max(secs, 1e-9)))
    print(dst)
    return 0


def cmd_restore(args) -> int:
    src = args.path
    if args.output:
        dst = args.output
    else:
        low = src.lower()
        if low.endswith(PACKED_EXT) or low.endswith(LEGACY_EXT):
            dst = src[:-4]
        else:
            dst = src + ".csv"
        if os.path.abspath(dst) == os.path.abspath(src):
            dst = dst + ".restored.csv"
    blob = open(src, "rb").read()
    t0 = time.time()
    table = fast.decode(blob)
    secs = time.time() - t0
    dtz.write_any(table, dst)
    rows, cols = table.shape
    out = os.path.getsize(dst)
    print("{:,} rows x {} cols   {} -> {}   {:.1f} MB/s".format(
        rows, cols, human(len(blob)), human(out),
        out / 1e6 / max(secs, 1e-9)))
    print(dst)
    return 0


def cmd_info(args) -> int:
    blob = open(args.path, "rb").read()
    if blob[:4] not in (fast.MAGIC, fast.MAGIC_V0):
        print("not a Polypress archive (bad magic)", file=sys.stderr)
        return 1
    ml = int.from_bytes(blob[4:8], "big")
    meta = json.loads(lzma.decompress(blob[16:16 + ml], **fast.XZ))
    kinds = {}
    for c in meta["cols"]:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    print("file        {}".format(args.path))
    print("size        {}".format(human(len(blob))))
    print("rows        {:,}".format(meta["nrows"]))
    print("columns     {}".format(len(meta["cols"])))
    print("plan        {}".format(", ".join(
        "{} {}".format(v, k) for k, v in sorted(kinds.items()))))
    if meta["groups"]:
        print("2D groups   {}".format("; ".join(
            ", ".join(meta["columns"][i] for i in g) for g in meta["groups"])))
    parented = sum(1 for c in meta["cols"]
                   if c["kind"] == "dict" and c.get("parent") is not None)
    print("reordered   {} columns sorted by a parent".format(parented))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="polypress", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compress", help="table -> .ppz")
    c.add_argument("path")
    c.add_argument("-o", "--output")
    c.add_argument("--no-verify", action="store_true",
                   help="skip the in-memory round-trip check (not advised)")
    c.set_defaults(fn=cmd_compress)

    r = sub.add_parser("restore", help=".ppz -> table")
    r.add_argument("path")
    r.add_argument("-o", "--output",
                   help="output path; the extension picks the format")
    r.set_defaults(fn=cmd_restore)

    i = sub.add_parser("info", help="what is inside an archive")
    i.add_argument("path")
    i.set_defaults(fn=cmd_info)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
