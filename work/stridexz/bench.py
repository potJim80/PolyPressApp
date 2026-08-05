#!/usr/bin/env python3
"""Measure stridexz against the field, one idea at a time.

    python3 stridexz/bench.py ../IN/corpus/cdc_nndss.csv ...

Every stridexz variant is decoded and compared cell by cell before its size is
reported. A variant that does not round-trip is printed as BROKEN and its
number is not used -- Polypress's invariant 4, kept here for the same reason.

PROBE_MAX_MB caps the input at a row boundary so every method sees the
identical file (default 8).
"""
import io
import lzma
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "old"))

from polypress import dtz, fast          # noqa: E402
from stridexz import codec                  # noqa: E402

_XZ = dict(format=lzma.FORMAT_XZ,
           filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])

# Cumulative: each row switches on one more idea than the row above. Ideas
# that measured worse are kept in the list, switched off downstream, so the
# negative results stay visible rather than being quietly deleted.
VARIANTS = [
    ("stridexz v0  baseline",     {}),
    ("stridexz v1  +pool",        {"pool": True}),
    ("stridexz v2  +fixed",       {"pool": True, "fixed": True}),
    ("stridexz v3  +planes",      {"pool": True, "fixed": True, "planes": True}),
    ("stridexz v4  +interleave",  {"pool": True, "fixed": True, "planes": True,
                                "interleave": True}),
    # interleave measured a net loss, so v5 drops it again
    ("stridexz v5  +template",    {"pool": True, "fixed": True, "planes": True,
                                "template": True}),
    # template lost too; v6 goes back to v3 and adds row blocking
    ("stridexz v6a +chunk 50k",   {"pool": True, "fixed": True, "planes": True,
                                "chunk": 50_000}),
    ("stridexz v6b +chunk 10k",   {"pool": True, "fixed": True, "planes": True,
                                "chunk": 10_000}),
    # chunking lost too; v7 goes back to v3 and retunes the entropy stage
    ("stridexz v7a +tune pb0",    {"pool": True, "fixed": True, "planes": True,
                                "tune": {"pb": 0}}),
    ("stridexz v7b +tune lc0pb0", {"pool": True, "fixed": True, "planes": True,
                                "tune": {"lc": 0, "pb": 0}}),
    ("stridexz v7c +tune lc4pb0", {"pool": True, "fixed": True, "planes": True,
                                "tune": {"lc": 4, "pb": 0}}),
    ("stridexz v7d +tune lc1pb0", {"pool": True, "fixed": True, "planes": True,
                                "tune": {"lc": 1, "pb": 0}}),
    # everything that survived measurement
    ("stridexz BEST",             {"pool": True, "fixed": True, "planes": True,
                                "tune": {"lc": 4, "pb": 0}}),
    # round two: selective dictionary, and row sorting on top of it
    ("stridexz v8 +dict",         {"pool": True, "fixed": True, "planes": True,
                                "tune": {"lc": 4, "pb": 0}, "dict": True}),
    ("stridexz v9 +dict+sort",    {"pool": True, "fixed": True, "planes": True,
                                "tune": {"lc": 4, "pb": 0}, "dict": True,
                                "sort": True}),
]

if os.environ.get("STRIDEXZ_ONLY"):
    keep = os.environ["STRIDEXZ_ONLY"].split(",")
    VARIANTS = [v for v in VARIANTS if any(k in v[0] for k in keep)]


def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def same(a, b) -> bool:
    return a.columns == b.columns and a.rows == b.rows


def load(path, max_mb):
    cap = int(max_mb * 1024 * 1024)
    if os.path.getsize(path) <= cap:
        return dtz.read_any(path).normalise(), False
    with open(path, "rb") as fh:
        head = fh.read(cap)
    head = head[:head.rfind(b"\n") + 1]
    tmp = path + ".__stridexz_cut"
    with open(tmp, "wb") as fh:
        fh.write(head)
    try:
        return dtz.read_any(tmp).normalise(), True
    finally:
        os.unlink(tmp)


def competitors(t, canon):
    rows = []
    blob, s = timed(lambda: lzma.compress(canon, **_XZ))
    rows.append(("xz -9e", len(blob), s, True))

    blob, s = timed(lambda: subprocess.run(
        ["brotli", "-q", "11", "-c"], input=canon,
        stdout=subprocess.PIPE, check=True).stdout)
    rows.append(("brotli -q11", len(blob), s, True))

    def parquet():
        import pyarrow as pa
        import pyarrow.parquet as pq
        arrays = {f"{n}__{i}": pa.array(t.column(i), type=pa.string())
                  for i, n in enumerate(t.columns)}
        buf = io.BytesIO()
        pq.write_table(pa.table(arrays), buf,
                       compression="zstd", compression_level=22)
        return buf.getvalue()

    blob, s = timed(parquet)
    rows.append(("parquet+zstd22", len(blob), s, True))

    blob, s = timed(lambda: fast.encode(t))
    rows.append(("POLYPRESS", len(blob), s, True))
    return rows


def run(path, max_mb):
    t, cut = load(path, max_mb)
    canon = dtz.canonical_csv(t)
    rows = competitors(t, canon)

    for name, opts in VARIANTS:
        try:
            blob, s = timed(lambda o=opts: codec.encode(t, **o))
            ok = same(t, codec.decode(blob))
        except Exception as exc:                       # noqa: BLE001
            print(f"   {name:<24} ERROR {type(exc).__name__}: {exc}")
            continue
        rows.append((name, len(blob), s, ok))

    return t.shape, len(canon), cut, rows


def main():
    max_mb = float(os.environ.get("PROBE_MAX_MB", "8"))
    jsonl = os.environ.get("STRIDEXZ_JSONL")
    totals = {}
    for path in sys.argv[1:]:
        shape, raw, cut, rows = run(path, max_mb)
        if jsonl:
            import json
            with open(jsonl, "a") as fh:
                fh.write(json.dumps({
                    "file": os.path.basename(path), "rows": shape[0],
                    "cols": shape[1], "csv": raw, "truncated": cut,
                    "results": {n: {"bytes": s, "secs": round(t, 2), "ok": ok}
                                for n, s, t, ok in rows}}) + "\n")
        note = " (truncated)" if cut else ""
        print(f"\n{os.path.basename(path)}  {shape[0]:,} rows x {shape[1]} "
              f"cols, {raw:,} bytes CSV{note}")
        print(f"   {'method':<24}{'bytes':>12}{'ratio':>9}{'secs':>8}  ok")
        pp = next(r[1] for r in rows if r[0] == "POLYPRESS")
        for name, size, secs, ok in sorted(rows, key=lambda r: r[1]):
            flag = "" if ok else "   <-- BROKEN, size not counted"
            vs = f"{pp / size:5.2f}x vs PP" if name != "POLYPRESS" else " " * 11
            print(f"   {name:<24}{size:>12,}{raw / size:>8.2f}x{secs:>8.1f}"
                  f"  {'y' if ok else 'N'}  {vs}{flag}")
            if ok:
                totals.setdefault(name, [0, 0.0])
                totals[name][0] += size
                totals[name][1] += secs
        sys.stdout.flush()

    if len(sys.argv) > 2 and totals:
        print("\n=== totals across all files ===")
        pp = totals["POLYPRESS"][0]
        for name, (size, secs) in sorted(totals.items(), key=lambda kv: kv[1][0]):
            print(f"   {name:<24}{size:>12,}{pp / size:>8.3f}x vs PP"
                  f"{secs:>9.1f}s")


if __name__ == "__main__":
    main()
