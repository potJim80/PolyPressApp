"""Polypress command line.

Installed as the `polypress` console script; `python3 tzip.py` still works and
calls straight into here, so nothing that used the old entry point breaks.

    polypress compress data.csv              -> data.csv.ppz
    polypress restore  data.csv.ppz          -> data.csv
    polypress restore  data.csv.ppz -o x.parquet
    polypress info     data.csv.ppz

For a file too large to hold in memory, the same three commands in a
block-at-a-time form, with a settable memory budget:

    polypress stream-compress big.csv --budget 1.0
    polypress stream-restore  big.csv.ppz -o back.csv
    polypress stream-info     big.csv.ppz

Restoring writes whatever format the output extension asks for, so this
doubles as a converter. Compression verifies the round trip in memory before
writing anything.
"""

from __future__ import annotations

import argparse
import json
import lzma
import os
import struct
import sys
import time

from . import dtz, fast, stream

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

    # The streaming variants share stream.py's implementation rather than
    # reimplementing it, so there is one code path and one archive format.
    sc = sub.add_parser("stream-compress",
                        help="table -> .ppz, one block at a time")
    sc.add_argument("path")
    sc.add_argument("-o", "--output")
    sc.add_argument("--budget", type=float, default=stream.DEFAULT_BUDGET_GB,
                    help="approximate peak memory in GB (default 1.0)")
    sc.add_argument("--rows", type=int,
                    help="rows per block, overrides --budget")
    sc.add_argument("--no-verify", action="store_true")
    sc.set_defaults(fn=lambda a: stream.main(
        ["compress", a.path] + (["-o", a.output] if a.output else [])
        + ["--budget", str(a.budget)]
        + (["--rows", str(a.rows)] if a.rows else [])
        + (["--no-verify"] if a.no_verify else [])))

    sr = sub.add_parser("stream-restore",
                        help="streamed .ppz -> table, one block at a time")
    sr.add_argument("path")
    sr.add_argument("-o", "--output",
                    help="output path; the extension picks the format")
    sr.set_defaults(fn=lambda a: stream.main(
        ["restore", a.path] + (["-o", a.output] if a.output else [])))

    si = sub.add_parser("stream-info", help="blocks and sizes in a stream archive")
    si.add_argument("path")
    si.set_defaults(fn=lambda a: stream.main(["info", a.path]))

    args = ap.parse_args(argv)
    return _run(args)


# Everything a damaged archive can raise on its way up. lzma and bz2 report
# corruption through their own exception types, and a header that decompresses
# into something that is not the JSON we expect surfaces as a KeyError or an
# IndexError several frames further in. None of these are bugs -- they are the
# decoder correctly refusing input someone else's disk or mail client mangled.
_CORRUPT = (ValueError, lzma.LZMAError, EOFError, KeyError, IndexError,
            TypeError, UnicodeDecodeError, OverflowError, MemoryError,
            struct.error)


def _run(args) -> int:
    """Dispatch, turning an expected failure into one line instead of a dump.

    `info` already did this by checking the magic itself; `restore` did not,
    so the same damaged file produced a clean message from one command and a
    twelve-line traceback from the other. A traceback reads as "this tool is
    broken" rather than "your file is damaged", which is exactly backwards
    when the whole point is that the decoder reads files other people made.
    """
    path = getattr(args, "path", None)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        raise
    except FileNotFoundError:
        print("polypress: no such file: {}".format(path), file=sys.stderr)
        return 1
    except IsADirectoryError:
        print("polypress: {} is a directory, not a file".format(path),
              file=sys.stderr)
        return 1
    except PermissionError:
        print("polypress: not allowed to read {}".format(path),
              file=sys.stderr)
        return 1
    except _CORRUPT as exc:
        if args.cmd in ("compress", "stream-compress"):
            print("polypress: cannot read {} as a table.".format(path),
                  file=sys.stderr)
        else:
            print("polypress: cannot read {} -- it is not a Polypress "
                  "archive, or it is damaged.".format(path), file=sys.stderr)
        print("           ({}: {})".format(type(exc).__name__, exc),
              file=sys.stderr)
        return 1
    except OSError as exc:
        # bz2 reports a corrupt stream as a plain OSError with no dedicated
        # class, so a damaged bzip2-fallback archive lands here rather than in
        # _CORRUPT above. Say what it means for the file the user named.
        if args.cmd in ("restore", "info", "stream-restore", "stream-info"):
            print("polypress: cannot read {} -- it is not a Polypress "
                  "archive, or it is damaged.".format(path), file=sys.stderr)
            print("           (OSError: {})".format(exc), file=sys.stderr)
        else:
            print("polypress: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
