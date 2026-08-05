"""Polypress against Parquet, like for like, with exactness checked.

    python3 benchmarks/bench_parquet.py corpus/nyc_311.csv --max-mb 25

The comparison the repo actually cares about, and the one turbo was never
measured against. Three things are held honest here:

  * **Exactness.** Parquet's "lossy" reputation is a READER problem, not a
    format one -- read every column as a string and it is byte-exact. So the
    all-varchar rows are the fair comparison and the typed rows are reported
    separately, with their fidelity measured rather than assumed.
  * **Row order.** Sorting rows is most of Polypress's advantage and Parquet
    can do it too -- but sorting DISCARDS the original row order, which
    Polypress keeps for free. The sorted rows are marked so that is not
    quietly claimed as a like-for-like win.
  * **Memory.** Each configuration runs in its own subprocess so peak RSS is
    that configuration's, not the accumulated total.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import resource
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 << 20)


def _load(path, max_mb):
    from polypress import dtz
    if max_mb:
        data = open(path, "rb").read(int(max_mb * 1e6))
        cut = data.rfind(b"\n")
        tmp = path + f".__cut{int(max_mb)}"
        open(tmp, "wb").write(data[:cut + 1])
        try:
            return dtz.read_any(tmp), len(data[:cut + 1])
        finally:
            os.unlink(tmp)
    return dtz.read_any(path), os.path.getsize(path)


def _sort_key_columns(table, k=12):
    """The k lowest-cardinality columns -- the crude ORDER BY that already
    captured most of Polypress's advantage on the user's NEDS slice."""
    card = []
    for j in range(len(table.columns)):
        col = table.column(j)
        card.append((len(set(col[::max(1, len(col) // 20000)])), j))
    card.sort()
    return [j for _c, j in card[:k]]


def run_one(path, max_mb, which):
    table, nbytes = _load(path, max_mb)
    nrows, ncols = len(table.rows), len(table.columns)
    out = {"which": which, "mb": nbytes / 1e6, "rows": nrows, "cols": ncols}

    if which in ("fast", "turbo"):
        import importlib
        mod = importlib.import_module(f"polypress.{which}")
        t0 = time.time()
        blob = mod.encode(table)
        out["enc"] = time.time() - t0
        t0 = time.time()
        back = mod.decode(blob)
        out["dec"] = time.time() - t0
        out["bytes"] = len(blob)
        out["exact"] = (back.rows == table.rows
                        and list(back.columns) == list(table.columns))
        out["order_kept"] = True
    else:
        import pyarrow as pa
        import pyarrow.parquet as pq
        _, kind, codec, sort = which.split(":")
        rows = table.rows
        if sort == "sorted":
            keys = _sort_key_columns(table)
            rows = sorted(rows, key=lambda r: tuple(r[j] for j in keys))
        cols = list(zip(*rows)) if rows else [[] for _ in table.columns]
        if kind == "varchar":
            arr = [pa.array(list(c), type=pa.string()) for c in cols]
        else:
            arr = []
            for c in cols:
                c = list(c)
                try:
                    arr.append(pa.array(c).cast(pa.int64()))
                    continue
                except Exception:
                    pass
                try:
                    arr.append(pa.array([float(x) if x else None for x in c],
                                        type=pa.float64()))
                    continue
                except Exception:
                    pass
                arr.append(pa.array(c, type=pa.string()))
        tbl = pa.Table.from_arrays(arr, names=[f"c{i}" for i in
                                               range(len(arr))])
        buf = io.BytesIO()
        t0 = time.time()
        pq.write_table(tbl, buf, compression=codec.split("-")[0],
                       compression_level=(int(codec.split("-")[1])
                                          if "-" in codec else None),
                       row_group_size=max(1, nrows))
        out["enc"] = time.time() - t0
        blob = buf.getvalue()
        out["bytes"] = len(blob)
        t0 = time.time()
        back = pq.read_table(io.BytesIO(blob))
        out["dec"] = time.time() - t0
        got = [[("" if v is None else str(v)) for v in col.to_pylist()]
               for col in back.columns]
        got_rows = [list(r) for r in zip(*got)]
        out["exact"] = got_rows == [list(r) for r in rows]
        out["order_kept"] = (sort != "sorted")

    out["rss"] = rss_mb()
    print("@@" + json.dumps(out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench_parquet")
    ap.add_argument("path")
    ap.add_argument("--max-mb", type=float, default=0)
    ap.add_argument("--which", default=None, help="internal")
    ap.add_argument("--only", default=None)
    a = ap.parse_args(argv)

    if a.which:
        run_one(a.path, a.max_mb, a.which)
        return 0

    configs = ["fast", "turbo",
               "pq:varchar:zstd-9:asis", "pq:varchar:zstd-19:asis",
               "pq:varchar:zstd-19:sorted", "pq:typed:zstd-19:asis",
               "pq:typed:zstd-19:sorted", "pq:varchar:snappy:asis"]
    if a.only:
        configs = [c for c in configs if a.only in c]

    print("{:<28} {:>11} {:>8} {:>8} {:>8} {:>7} {:>7} {:>6}".format(
        "config", "bytes", "enc s", "MB/s", "dec s", "exact", "order", "RSS"))
    print("-" * 92)
    results = []
    for c in configs:
        cmd = [sys.executable, os.path.abspath(__file__), a.path,
               "--max-mb", str(a.max_mb), "--which", c]
        env = dict(os.environ, OMP_NUM_THREADS="1")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env=env,
                               timeout=3600)
        except subprocess.TimeoutExpired:
            print(f"{c:<28} TIMEOUT")
            continue
        line = [l for l in p.stdout.splitlines() if l.startswith("@@")]
        if not line:
            err = p.stderr.strip().splitlines()
            print("{:<28} FAILED  {}".format(c, err[-1][:48] if err else ""))
            continue
        r = json.loads(line[0][2:])
        results.append(r)
        print("{:<28} {:>11,} {:>7.2f}s {:>8.2f} {:>7.2f}s {:>7} {:>7} "
              "{:>5.0f}M".format(
                  c, r["bytes"], r["enc"], r["mb"] / r["enc"], r["dec"],
                  "yes" if r["exact"] else "**NO**",
                  "kept" if r["order_kept"] else "LOST", r["rss"]))
    if results:
        base = next((r for r in results if r["which"] == "turbo"), None)
        if base:
            print(f"\nturbo vs each, on {base['mb']:.1f} MB "
                  f"({base['rows']:,} rows x {base['cols']} cols):")
            for r in results:
                if r["which"] == "turbo":
                    continue
                print("   vs {:<26} size {:>+7.1f}%   speed {:>6.2f}x"
                      .format(r["which"],
                              100.0 * (base["bytes"] - r["bytes"]) / r["bytes"],
                              r["enc"] / base["enc"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
