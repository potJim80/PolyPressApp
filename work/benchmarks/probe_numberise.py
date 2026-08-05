#!/usr/bin/env python3
"""Probe: forget parenting. Number-encode every column, hand the lot to xz.

The question, in the user's words: "have we tried just forgetting parenting
columns? just number encode everything ... every new thing gets a new number."

Baselines:
  csv+xz        canonical CSV through xz -9e (the codec's own plain fallback)
  polypress     the real codec, every trick on

Variants, all of them dictionary + integer matrix + xz -9e:
  num-text      ids as decimal text, column-major
  num-b-col     ids as fixed-width LE bytes, column-major
  num-b-row     ids as fixed-width LE bytes, row-major (constant stride/record)
  num-b-plane   fixed-width bytes, column-major, byte planes split
  num-freq      like num-b-col but ids assigned by frequency (0 = commonest)
  num-global    one dictionary shared by every column (literal reading of
                "every new thing gets a new number")

Every variant stores the same information, so sizes are comparable. None of
them round-trip here -- this measures a ceiling, not a codec.
"""
import io
import lzma
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, "/Users/mahdiakbarin/Desktop/polypress/work")

from polypress import dtz, fast  # noqa: E402

XZ = dict(format=lzma.FORMAT_XZ,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])


def xz(data: bytes) -> int:
    return len(lzma.compress(data, **XZ))


def width_for(n: int) -> int:
    if n <= 256:
        return 1
    if n <= 65536:
        return 2
    return 4


def numberise(cols, by_freq=False):
    """-> (list of id-lists, list of dictionaries in id order)."""
    ids_all, dicts_all = [], []
    for col in cols:
        if by_freq:
            order = [v for v, _ in Counter(col).most_common()]
        else:
            order, seen = [], set()
            for v in col:
                if v not in seen:
                    seen.add(v)
                    order.append(v)
        index = {v: i for i, v in enumerate(order)}
        ids_all.append([index[v] for v in col])
        dicts_all.append(order)
    return ids_all, dicts_all


def dict_blob(dicts_all) -> bytes:
    out = io.BytesIO()
    for d in dicts_all:
        out.write(("\n".join(d) + "\n\x00").encode("utf-8", "surrogatepass"))
    return out.getvalue()


def pack_col_major(ids_all, widths) -> bytes:
    out = io.BytesIO()
    for ids, w in zip(ids_all, widths):
        out.write(b"".join(i.to_bytes(w, "little") for i in ids))
    return out.getvalue()


def pack_row_major(ids_all, widths) -> bytes:
    out = io.BytesIO()
    nrows = len(ids_all[0]) if ids_all else 0
    for r in range(nrows):
        for ids, w in zip(ids_all, widths):
            out.write(ids[r].to_bytes(w, "little"))
    return out.getvalue()


def pack_planes(ids_all, widths) -> bytes:
    out = io.BytesIO()
    for ids, w in zip(ids_all, widths):
        raw = b"".join(i.to_bytes(w, "little") for i in ids)
        if w == 1:
            out.write(raw)
        else:
            for p in range(w):
                out.write(raw[p::w])
    return out.getvalue()


def text_col_major(ids_all) -> bytes:
    out = io.BytesIO()
    for ids in ids_all:
        out.write(("\n".join(str(i) for i in ids) + "\n").encode())
    return out.getvalue()


def globalise(cols):
    index, order = {}, []
    ids_all = []
    for col in cols:
        ids = []
        for v in col:
            j = index.get(v)
            if j is None:
                j = index[v] = len(order)
                order.append(v)
            ids.append(j)
        ids_all.append(ids)
    return ids_all, order


def load(path, max_mb):
    """Read the table, truncated at a row boundary if oversize."""
    cap = int(max_mb * 1024 * 1024)
    size = os.path.getsize(path)
    if size <= cap:
        return dtz.read_any(path).normalise()
    with open(path, "rb") as fh:
        head = fh.read(cap)
    head = head[:head.rfind(b"\n") + 1]
    tmp = path + ".__probe_cut"
    with open(tmp, "wb") as fh:
        fh.write(head)
    try:
        return dtz.read_any(tmp).normalise()
    finally:
        os.unlink(tmp)


def run(path, max_mb=float(os.environ.get("PROBE_MAX_MB", "16"))):
    t = load(path, max_mb)
    nrows, ncols = t.shape
    cols = [t.column(i) for i in range(ncols)]
    header = (",".join(t.columns) + "\n").encode()

    res = {}
    res["csv+xz"] = xz(dtz.canonical_csv(t))
    t0 = time.time()
    res["polypress"] = len(fast.encode(t))
    pp_secs = time.time() - t0

    # -- per-column dictionaries, first-appearance ids
    ids_all, dicts_all = numberise(cols)
    widths = [width_for(len(d)) for d in dicts_all]
    dblob = dict_blob(dicts_all)

    res["num-text"] = xz(header + dblob + text_col_major(ids_all))
    res["num-b-col"] = xz(header + dblob + pack_col_major(ids_all, widths))
    res["num-b-row"] = xz(header + dblob + pack_row_major(ids_all, widths))
    res["num-b-plane"] = xz(header + dblob + pack_planes(ids_all, widths))

    # -- frequency-ordered ids
    fids, fdicts = numberise(cols, by_freq=True)
    fwidths = [width_for(len(d)) for d in fdicts]
    res["num-freq"] = xz(header + dict_blob(fdicts)
                         + pack_col_major(fids, fwidths))
    del fids, fdicts

    # -- one dictionary for the whole table
    gids, gorder = globalise(cols)
    gw = width_for(len(gorder))
    gblob = ("\n".join(gorder) + "\n").encode("utf-8", "surrogatepass")
    res["num-global"] = xz(header + gblob
                           + pack_col_major(gids, [gw] * ncols))
    del gids, gorder

    card = sum(len(d) for d in dicts_all)
    return dict(name=os.path.basename(path), rows=nrows, cols=ncols,
                cells=nrows * ncols, distinct=card, pp_secs=pp_secs, **res)


NAMES = ["csv+xz", "polypress", "num-text", "num-b-col", "num-b-row",
         "num-b-plane", "num-freq", "num-global"]


def main():
    paths = sys.argv[1:]
    rows = []
    for p in paths:
        try:
            r = run(p)
        except Exception as exc:                        # noqa: BLE001
            print(f"{os.path.basename(p)}: FAILED {type(exc).__name__}: {exc}",
                  flush=True)
            continue
        rows.append(r)
        base = r["csv+xz"]
        print(f"\n{r['name']}  {r['rows']:,} rows x {r['cols']} cols, "
              f"{r['distinct']:,} distinct values", flush=True)
        for n in NAMES:
            print(f"   {n:<12} {r[n]:>12,}   {base / r[n]:6.3f}x vs csv+xz   "
                  f"{r['polypress'] / r[n]:6.3f}x vs polypress", flush=True)

    if len(rows) > 1:
        print("\n=== totals ===")
        for n in NAMES:
            tot = sum(r[n] for r in rows)
            b = sum(r["csv+xz"] for r in rows)
            p = sum(r["polypress"] for r in rows)
            print(f"   {n:<12} {tot:>12,}   {b / tot:6.3f}x vs csv+xz   "
                  f"{p / tot:6.3f}x vs polypress")
        print("\n=== per-file winner among the number-encoded variants ===")
        for r in rows:
            v = min(NAMES[2:], key=lambda n: r[n])
            print(f"   {r['name']:<28} {v:<12} "
                  f"{r['polypress'] / r[v]:6.3f}x vs polypress")


if __name__ == "__main__":
    main()
