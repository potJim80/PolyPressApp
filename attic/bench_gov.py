"""Benchmark dtz against the actual industry compressors on a real dataset.

Contenders are real binaries and real pyarrow, not proxies:

  general purpose   gzip -9, bzip2 -9, xz -9e, zstd --ultra -22 (+ --long),
                    brotli -q 11
  columnar          Parquet with snappy / gzip / brotli / zstd-22, written by
                    pyarrow from a type-inferred CSV read -- i.e. how a data
                    engineer would actually store this table

Reported honestly, because several dtz strategies ARE standard tools wearing a
different hat. A dtz win only means something if the strategy that won is one
this project contributed (poly*, colmajor*, fd*) rather than a repackaged xz.

Fidelity is not symmetric and the script checks it: dtz guarantees the exact
printed text round-trips. Parquet-with-type-inference often does not -- it may
turn "1.50" into 1.5 -- so part of any Parquet size advantage can come from
quietly discarding formatting.

    python3 bench_gov.py <file.csv> [--unordered]
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from typing import List, Optional, Tuple

from polypress import dtz

OURS = ("poly", "colmajor", "fd.")          # strategy prefixes we contributed

CLI_TOOLS = [
    ("gzip -9", ["gzip", "-9", "-c"]),
    ("bzip2 -9", ["bzip2", "-9", "-c"]),
    ("xz -9e", ["xz", "-9e", "-c"]),
    ("zstd -19", ["zstd", "-19", "-c", "-q"]),
    ("zstd -22 --ultra", ["zstd", "--ultra", "-22", "-c", "-q"]),
    ("zstd -22 --long", ["zstd", "--ultra", "-22", "--long=27", "-c", "-q"]),
    ("brotli -q 11", ["brotli", "-q", "11", "-c"]),
]


def have(binary: str) -> bool:
    return subprocess.call(["which", binary], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def run_cli(cmd: List[str], path: str) -> Tuple[Optional[int], float]:
    start = time.time()
    try:
        with open(path, "rb") as fh:
            proc = subprocess.run(cmd, stdin=fh, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=900)
        if proc.returncode != 0:
            return None, 0.0
        return len(proc.stdout), time.time() - start
    except Exception:
        return None, 0.0


def parquet_variants(path: str):
    """Read the CSV the way a data engineer would (type inference) and write
    Parquet with each codec. Also report whether that round-trips the exact
    printed text."""
    try:
        import pyarrow as pa
        import pyarrow.csv as pacsv
        import pyarrow.parquet as pq
    except ImportError:
        return [], None

    table = pacsv.read_csv(path)
    results = []
    for codec_name, level in (("snappy", None), ("gzip", 9),
                              ("brotli", 11), ("zstd", 22)):
        start = time.time()
        buf = io.BytesIO()
        kw = {"compression": codec_name}
        if level is not None:
            kw["compression_level"] = level
        try:
            pq.write_table(table, buf, use_dictionary=True, **kw)
        except Exception:
            continue
        results.append(("parquet+" + codec_name, buf.getbuffer().nbytes,
                        time.time() - start))

    # does the typed Parquet path preserve the exact printed cells?
    exact = None
    try:
        original = dtz.read_any(path)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="zstd", compression_level=22)
        buf.seek(0)
        back = pq.read_table(buf)
        cols = {c: back.column(c).to_pylist() for c in back.column_names}
        exact = True
        for j, name in enumerate(original.columns):
            if name not in cols:
                exact = False
                break
            got = ["" if v is None else dtz._scalar(v) for v in cols[name]]
            if got != original.column(j):
                exact = False
                break
    except Exception:
        exact = None
    return results, exact


def main(argv) -> int:
    path = argv[1]
    unordered = "--unordered" in argv
    raw = os.path.getsize(path)

    print("\n" + "=" * 74)
    print("dataset: {}".format(os.path.basename(path)))
    table = dtz.read_any(path)
    rows, cols = table.shape
    print("  {:,} rows x {} columns   {:,} bytes on disk".format(
        rows, cols, raw))
    print("=" * 74)

    rows_out = []

    for label, cmd in CLI_TOOLS:
        if not have(cmd[0]):
            continue
        size, secs = run_cli(cmd, path)
        if size:
            rows_out.append((label, size, secs, "general"))

    pq_results, pq_exact = parquet_variants(path)
    for label, size, secs in pq_results:
        rows_out.append((label, size, secs, "columnar"))

    start = time.time()
    attempts, blobs = dtz.try_all(table, unordered=unordered, verify=True)
    dtz_secs = time.time() - start
    ok = sorted((a for a in attempts if a.size), key=lambda a: a.size)
    if not ok:
        print("dtz produced no valid output")
        return 1
    best = ok[0]
    rows_out.append(("dtz ({})".format(best.name), best.size, dtz_secs, "dtz"))

    rows_out.sort(key=lambda r: r[1])
    print("\n  {:<26} {:>12} {:>8} {:>9}  {}".format(
        "compressor", "bytes", "ratio", "seconds", "class"))
    print("  " + "-" * 70)
    for label, size, secs, kind in rows_out:
        print("  {:<26} {:>12,} {:>7.2f}x {:>9.1f}  {}".format(
            label, size, raw / size, secs, kind))

    best_general = min((r for r in rows_out if r[3] == "general"),
                       key=lambda r: r[1], default=None)
    best_columnar = min((r for r in rows_out if r[3] == "columnar"),
                        key=lambda r: r[1], default=None)
    industry = [r for r in rows_out if r[3] in ("general", "columnar")]
    best_industry = min(industry, key=lambda r: r[1]) if industry else None

    print("\n  verdict")
    if best_general:
        print("    best general-purpose : {:<20} {:>12,} B".format(
            best_general[0], best_general[1]))
    if best_columnar:
        print("    best columnar        : {:<20} {:>12,} B".format(
            best_columnar[0], best_columnar[1]))
    print("    dtz                  : {:<20} {:>12,} B".format(
        best.name, best.size))
    if best_industry:
        delta = best_industry[1] / best.size
        verb = "smaller than" if delta > 1 else "LARGER than"
        print("    dtz is {:.2f}x {} the best industry entry ({})".format(
            delta if delta > 1 else 1 / delta, verb, best_industry[0]))

    contributed = best.name.startswith(OURS)
    print("\n    winning dtz strategy is {}".format(
        "one this project contributed" if contributed
        else "a repackaged standard tool -- NOT a win for this project"))
    if pq_exact is False:
        print("    note: the typed Parquet path does NOT reproduce the exact")
        print("          printed text, so its sizes buy something dtz does not"
              " discard")
    elif pq_exact is True:
        print("    note: the typed Parquet path does reproduce the exact text "
              "here")

    print("\n  full dtz strategy breakdown")
    for a in ok:
        flag = "" if a.ordered else "  [unordered]"
        print("    {:<26} {:>12,} {:>7.2f}x{}".format(
            a.name, a.size, raw / a.size, flag))
    for a in attempts:
        if a.size is None:
            print("    {:<26} {:>12}  {}".format(a.name, "failed", a.error))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
