"""Measure one table against every competitor, and print one JSON line.

    python3 benchmarks/measure_one.py table.csv            # human readable
    python3 benchmarks/measure_one.py table.csv --json     # one JSON line

This is the per-dataset worker behind `sweep.py`. It exists as a separate
process on purpose, for two reasons that both cost a session to learn:

  * **Memory is released between datasets.** A hundred tables in one process
    accumulates numpy arrays, pyarrow buffers and interned Python strings
    until the machine swaps. A fresh process per table means peak RSS is
    whatever the single largest table needs and nothing more.
  * **A crash costs one dataset, not the run.** Some public CSVs are not
    UTF-8, some are not rectangular, and pyarrow segfaults on a few. The
    parent records the failure and moves on.

Peak RSS is measured, not estimated, and reported in the JSON. The estimate
in CLAUDE.md -- (input MB x 22) + 700 -- was itself wrong by 18% once, so
nothing here trusts it for anything except deciding whether to start.

The lineup
----------
Three families, because "compressed CSV" is not what a competitor would
actually do with a table:

  general purpose  gzip, bzip2, xz, zstd at three levels, brotli, lz4
  columnar files   Parquet at four codecs, ORC at three, Arrow/Feather at two
  polypress        the shipping encoder, plus the same modelled streams
                   re-finished with zstd and brotli

That last one is the point of the whole file. Polypress finishes with xz and
Parquet cannot use xz at all -- pyarrow answers `Unsupported compression: xz`
-- so a raw size comparison is partly a comparison of finishers. Re-finishing
the *same* modelled streams with the *same* codec the competitor used
separates the modelling from the entropy coder. If the win survives that, it
is a real win.
"""

from __future__ import annotations

import argparse
import bz2
import io
import json
import lzma
import os
import resource
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polypress import dtz, fast

CLI_TOOLS = [
    ("gzip -9",        ["gzip", "-9", "-c"],                   ["gzip", "-dc"]),
    ("bzip2 -9",       ["bzip2", "-9", "-c"],                  ["bzip2", "-dc"]),
    ("xz -9e",         ["xz", "-9e", "-c"],                    ["xz", "-dc"]),
    ("lz4 -9",         ["lz4", "-9", "-c"],                    ["lz4", "-dc"]),
    ("zstd -3",        ["zstd", "-3", "-c", "-q"],             ["zstd", "-dc", "-q"]),
    ("zstd -19",       ["zstd", "-19", "-c", "-q"],            ["zstd", "-dc", "-q"]),
    ("zstd -22",       ["zstd", "--ultra", "-22", "-c", "-q"], ["zstd", "-dc", "-q"]),
    ("brotli -q11",    ["brotli", "-q", "11", "-c"],           ["brotli", "-dc"]),
]

PARQUET_CODECS = (("snappy", None), ("gzip", 9), ("brotli", 11), ("zstd", 22))
ORC_CODECS = ("SNAPPY", "ZLIB", "ZSTD")
ORC_MAX_COLUMNS = 1000      # see the note in arrow_rows(); ORC is a memory hog
FEATHER_CODECS = ("lz4", "zstd")


def peak_mb() -> float:
    """Peak RSS of this process. macOS reports bytes, Linux kilobytes."""
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v / 1e6 if sys.platform == "darwin" else v / 1e3


def have(binary: str) -> bool:
    return subprocess.call(["which", binary], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


REPS = 1


def clock(fn):
    """Best of REPS. Best, not mean: we want the tool's speed, not the
    machine's background noise.

    One pass is right for a corpus sweep and wrong for a single file. Three
    passes triples a run already dominated by brotli -q 11 at about 1 MB/s,
    and sizes are identical either way, so the sweep leaves this at 1 and
    says so in the report rather than pretending the clock is exact."""
    best, out = None, None
    for _ in range(REPS):
        t0 = time.time()
        out = fn()
        dt = max(time.time() - t0, 1e-6)
        best = dt if best is None else min(best, dt)
    return out, best


# --------------------------------------------------------------------------
# re-finishing: the same modelled streams, a different entropy coder

def _zstd22(b: bytes) -> bytes:
    import zstandard
    return zstandard.ZstdCompressor(level=22).compress(b)


def _brotli11(b: bytes) -> bytes:
    return subprocess.run(["brotli", "-q", "11", "-c"], input=b,
                          stdout=subprocess.PIPE).stdout


def refinish(blob: bytes, comp) -> int:
    """Size of the polypress archive with `comp` in place of xz.

    Walks the container, undoes each xz section, and recompresses it. The
    header bytes are counted unchanged -- they are the same few bytes either
    way, and pretending otherwise would flatter the result.
    """
    head = blob[:4]
    if head == fast.MAGIC_RAW_XZ:
        return 4 + len(comp(lzma.decompress(blob[4:], **fast.XZ)))
    if head == fast.MAGIC_RAW_BZ:
        return 4 + len(comp(bz2.decompress(blob[4:])))
    if head not in (fast.MAGIC, fast.MAGIC_V0):
        raise ValueError("not a polypress archive")
    ml = int.from_bytes(blob[4:8], "big")
    bl = int.from_bytes(blob[8:12], "big")
    o = 16
    total = 16
    for size in (ml, bl, None):
        section = blob[o:o + size] if size is not None else blob[o:]
        total += len(comp(lzma.decompress(section, **fast.XZ)))
        if size is not None:
            o += size
    return total


# --------------------------------------------------------------------------
# columnar competitors

def arrow_rows(path: str, mb: float, rows: list) -> bool:
    """Parquet / ORC / Feather. Returns whether the typed read was exact."""
    try:
        import pyarrow as pa
        import pyarrow.csv as pacsv
        import pyarrow.feather as pf
        import pyarrow.orc as porc
        import pyarrow.parquet as pq
    except ImportError:
        return None
    try:
        table = pacsv.read_csv(path)
    except Exception:
        return None

    # An all-blank column reads back as Arrow type `null`, and ORC refuses it
    # outright ("Unknown or unsupported Arrow type: null"). Blank columns are
    # common in administrative extracts, so dropping ORC over them would quietly
    # remove a whole competitor from most of the corpus. Cast to string, which
    # is what the CSV actually held.
    if any(pa.types.is_null(f.type) for f in table.schema):
        table = table.cast(pa.schema([
            f.with_type(pa.string()) if pa.types.is_null(f.type) else f
            for f in table.schema]))

    for name, level in PARQUET_CODECS:
        kw = {"compression": name}
        if level is not None:
            kw["compression_level"] = level
        try:
            def write():
                buf = io.BytesIO()
                pq.write_table(table, buf, use_dictionary=True, **kw)
                return buf.getvalue()
            blob, te = clock(write)
            _, td = clock(lambda: pq.read_table(io.BytesIO(blob)))
            rows.append(["parquet+" + name, len(blob), mb / te, mb / td])
        except Exception:
            pass

    # ORC buffers roughly half a megabyte per column while writing a stripe, so
    # a very wide table costs gigabytes regardless of how little data it holds.
    # Measured: the 78 KB, 1-row x 5,000-column `single_wide_row` peaks at
    # 2,363 MB in pyarrow's ORC writer, against 98 MB for Parquet, 77 MB for
    # Feather and 115 MB for Polypress. That aborted a sweep on its RSS ceiling.
    #
    # Skipping is recorded, not silent: `report.py` prints an `n` column per
    # competitor, so a format that sat out some datasets is visible rather than
    # quietly averaged over the ones it managed.
    orc_ok = len(table.schema) <= ORC_MAX_COLUMNS
    for name in (ORC_CODECS if orc_ok else ()):
        try:
            def write():
                buf = io.BytesIO()
                porc.write_table(table, buf, compression=name)
                return buf.getvalue()
            blob, te = clock(write)
            _, td = clock(lambda: porc.read_table(io.BytesIO(blob)))
            rows.append(["orc+" + name.lower(), len(blob), mb / te, mb / td])
        except Exception:
            pass

    for name in FEATHER_CODECS:
        try:
            def write():
                buf = io.BytesIO()
                pf.write_feather(table, buf, compression=name)
                return buf.getvalue()
            blob, te = clock(write)
            # read_table, not read_feather: the latter returns a pandas frame
            # and pandas need not be installed. That import error was being
            # swallowed and silently deleting Feather from the lineup.
            _, td = clock(lambda: pf.read_table(io.BytesIO(blob)))
            rows.append(["feather+" + name, len(blob), mb / te, mb / td])
        except Exception:
            pass

    # Does the typed columnar path give back the exact printed cells? If not,
    # part of any size win is discarded formatting, not compression -- "1.50"
    # silently becoming 1.5 is a smaller file for a reason that is not
    # compression, and a size table without this verdict is not a comparison.
    try:
        original = dtz.read_any(path)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="zstd", compression_level=22)
        buf.seek(0)
        back = pq.read_table(buf)
        cols = {c: back.column(c).to_pylist() for c in back.column_names}
        for j, name in enumerate(original.columns):
            if name not in cols:
                return False
            got = ["" if v is None else dtz._scalar(v) for v in cols[name]]
            if got != original.column(j):
                return False
        return True
    except Exception:
        return None


# --------------------------------------------------------------------------

def measure(path: str) -> dict:
    raw = os.path.getsize(path)
    mb = raw / 1e6
    out = {"file": os.path.basename(path), "bytes": raw}
    data = open(path, "rb").read()

    rows = []
    for label, ce, cd in CLI_TOOLS:
        if not have(ce[0]):
            continue
        comp, te = clock(lambda: subprocess.run(
            ce, input=data, stdout=subprocess.PIPE).stdout)
        _, td = clock(lambda: subprocess.run(
            cd, input=comp, stdout=subprocess.PIPE).stdout)
        rows.append([label, len(comp), mb / te, mb / td])

    out["parquet_exact"] = arrow_rows(path, mb, rows)

    t = dtz.read_any(path)
    out["rows"], out["cols"] = t.shape
    del data

    blob, te = clock(lambda: fast.encode(t))
    back, td = clock(lambda: fast.decode(blob))
    out["roundtrip"] = (back.columns == t.columns and back.rows == t.rows)
    del back
    rows.append(["polypress", len(blob), mb / te, mb / td])

    # Like for like: strip xz out and use the competitor's own finisher.
    for label, comp in (("zstd", _zstd22), ("brotli", _brotli11)):
        try:
            rows.append(["polypress+" + label, refinish(blob, comp),
                         None, None])
        except Exception:
            pass
    del blob, t

    out["results"] = {r[0]: {"bytes": r[1], "enc_mbs": r[2], "dec_mbs": r[3]}
                      for r in rows}
    out["peak_mb"] = round(peak_mb(), 1)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="measure_one")
    ap.add_argument("path")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--reps", type=int, default=REPS,
                    help="timing passes per codec (default {}); raise it for "
                         "a single file, leave it for a corpus".format(REPS))
    a = ap.parse_args(argv)
    globals()["REPS"] = max(1, a.reps)
    try:
        rec = measure(a.path)
    except Exception as exc:
        rec = {"file": os.path.basename(a.path),
               "bytes": os.path.getsize(a.path),
               "error": "{}: {}".format(type(exc).__name__, str(exc)[:160]),
               "peak_mb": round(peak_mb(), 1)}
    if a.json:
        print(json.dumps(rec))
        return 0

    print("\n{}  {:,} B".format(rec["file"], rec["bytes"]))
    if "error" in rec:
        print("  ERROR: " + rec["error"])
        return 1
    print("{:,} rows x {} columns, peak RSS {} MB".format(
        rec["rows"], rec["cols"], rec["peak_mb"]))
    print("  {:<18} {:>12} {:>7} {:>9} {:>9}".format(
        "codec", "bytes", "ratio", "enc MB/s", "dec MB/s"))
    for label, r in sorted(rec["results"].items(), key=lambda kv: kv[1]["bytes"]):
        print("  {:<18} {:>12,} {:>6.2f}x {:>9} {:>9}".format(
            label, r["bytes"], rec["bytes"] / r["bytes"],
            "-" if r["enc_mbs"] is None else "{:.1f}".format(r["enc_mbs"]),
            "-" if r["dec_mbs"] is None else "{:.1f}".format(r["dec_mbs"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
