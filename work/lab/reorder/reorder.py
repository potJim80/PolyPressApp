"""Three reorderings of a table, each fed to the same xz back end.

Question (Mahdi, 2026-09-28): if Polypress is "reorder, then xz", is its
reordering the right one? Three completely different ways to order a table's
cells are tested here against PolyPress's own and against no reordering.

Everything except the ORDER is held fixed, so the numbers isolate reordering:
every method writes cells as text (backslash-escaped, newline-separated),
column after column, into one xz -9e stream -- no dictionary ids, no numeric
differences. A method that must store its order pays for it in the total.

    none      the control: columns contiguous, rows in file order
    parent    PolyPress's own reordering on this back end: each column sorted
              by the parent PolyPress picks for it (fast.pick_parents /
              pick_text_parents). Free -- the decoder recomputes the sort.
    global    ONE shared row order: sort every row by all columns, lowest
              cardinality first. The permutation is stored.
    chain     ONE shared row order: a greedy nearest-neighbour path through
              the rows, each step to the unvisited row differing in the fewest
              (size-weighted) cells. The permutation is stored.
    context   A DIFFERENT order per column, free: column k (in ascending
              cardinality order) is sorted by every column stored before it --
              the table version of the BWT's context sort. The decoder has
              those columns already, so it recomputes each sort.

Every method has a decoder and every result is round-trip checked; a size
without a decode is not a result (memory/LAWS.md).

    python3 lab/reorder/reorder.py ../IN/suite/m_nndss_2mb.csv ... [--json out]
"""

from __future__ import annotations

import json
import lzma
import os
import re
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
from polypress import dtz, fast  # noqa: E402

XZ = fast.XZ
CHAIN_ORDERS = 3        # candidate orderings for the nearest-neighbour search
CHAIN_WINDOW = 3        # neighbours taken on each side in each ordering
CHAIN_SEED = 20260928


# ------------------------------------------------------------ back end

_UNESC = re.compile(r"\\(.)", re.S)


def _esc(c: str) -> str:
    return c.replace("\\", "\\\\").replace("\n", "\\n")


def _unesc(c: str) -> str:
    return _UNESC.sub(lambda m: "\n" if m.group(1) == "n" else m.group(1), c)


def pack_columns(cols) -> bytes:
    """Cells as escaped text, one per line, column after column."""
    return "\n".join(_esc(c) for col in cols for c in col).encode("utf-8")


def unpack_columns(data: bytes, ncols: int, n: int):
    if ncols == 0 or n == 0:
        return [[] for _ in range(ncols)]
    cells = [_unesc(c) for c in data.decode("utf-8").split("\n")]
    if len(cells) != ncols * n:
        raise ValueError("cell count")
    return [cells[k * n:(k + 1) * n] for k in range(ncols)]


def factorize(cells) -> np.ndarray:
    """Ids in sorted-value order -- the same ids fast.classify gives."""
    uniq = sorted(set(cells))
    idx = {v: i for i, v in enumerate(uniq)}
    return np.fromiter((idx[v] for v in cells), dtype=np.int64,
                       count=len(cells))


def pack_perm(perm: np.ndarray) -> bytes:
    """Row order, as raw indices or as deltas, whichever xz takes smaller."""
    n = len(perm)
    w = np.uint16 if n < (1 << 16) else np.uint32
    raw = lzma.compress(b"R" + perm.astype(w).tobytes(), **XZ)
    d = np.diff(perm, prepend=0).astype(np.int32)
    dlt = lzma.compress(b"D" + d.tobytes(), **XZ)
    return raw if len(raw) <= len(dlt) else dlt


def unpack_perm(blob: bytes, n: int) -> np.ndarray:
    b = lzma.decompress(blob, **XZ)
    if b[:1] == b"R":
        w = np.uint16 if n < (1 << 16) else np.uint32
        return np.frombuffer(b[1:], dtype=w).astype(np.int64)
    return np.cumsum(np.frombuffer(b[1:], dtype=np.int32).astype(np.int64))


def container(meta: dict, payload: bytes, perm_blob: bytes = b"") -> bytes:
    mb = lzma.compress(json.dumps(meta, separators=(",", ":")).encode(), **XZ)
    pb = lzma.compress(payload, **XZ)
    return (len(mb).to_bytes(4, "big") + len(pb).to_bytes(4, "big")
            + len(perm_blob).to_bytes(4, "big") + mb + pb + perm_blob)


def open_container(blob: bytes):
    ml, pl, ql = (int.from_bytes(blob[i:i + 4], "big") for i in (0, 4, 8))
    o = 12
    meta = json.loads(lzma.decompress(blob[o:o + ml], **XZ))
    o += ml
    payload = lzma.decompress(blob[o:o + pl], **XZ)
    o += pl
    return meta, payload, blob[o:o + ql]


def columns_of(table):
    return [table.column(j) for j in range(len(table.columns))]


def rows_of(cols, n):
    return [[col[i] for col in cols] for i in range(n)]


def by_cardinality(cols):
    """Column indices, fewest distinct values first; ties by position."""
    return sorted(range(len(cols)), key=lambda j: (len(set(cols[j])), j))


# ------------------------------------------------------------ none

def enc_none(table):
    return container({"m": "none", "c": table.columns, "n": len(table.rows)},
                     pack_columns(columns_of(table)))


def dec_none(blob):
    meta, payload, _ = open_container(blob)
    cols = unpack_columns(payload, len(meta["c"]), meta["n"])
    return dtz.Table(meta["c"], rows_of(cols, meta["n"]))


# ------------------------------------------------------------ parent
#
# PolyPress's own choice of parents, applied to text on this back end. Only
# columns PolyPress would sort get sorted; parents are always dictionary
# columns, and `order` lists every dictionary column parents-first.

def enc_parent(table):
    n = len(table.rows)
    plan = fast.classify(table)
    parent, order = fast.pick_parents(plan, n)
    tparent = fast.pick_text_parents(plan, n, parent, order)
    par = {j: p for j, p in list(parent.items()) + list(tparent.items())
           if p is not None}
    stored = list(order) + [j for j in range(len(plan)) if j not in order]
    cols = columns_of(table)
    out = []
    for j in stored:
        col = cols[j]
        if j in par:
            perm = np.argsort(factorize(cols[par[j]]), kind="stable")
            col = [col[i] for i in perm]
        out.append(col)
    meta = {"m": "parent", "c": table.columns, "n": n, "s": stored,
            "p": {str(j): p for j, p in par.items()}}
    return container(meta, pack_columns(out))


def dec_parent(blob):
    meta, payload, _ = open_container(blob)
    n, stored = meta["n"], meta["s"]
    par = {int(j): p for j, p in meta["p"].items()}
    got = unpack_columns(payload, len(stored), n)
    cols = [None] * len(stored)
    for j, col in zip(stored, got):
        if j in par:
            perm = np.argsort(factorize(cols[par[j]]), kind="stable")
            orig = [None] * n
            for k, i in enumerate(perm):
                orig[i] = col[k]
            col = orig
        cols[j] = col
    return dtz.Table(meta["c"], rows_of(cols, n))


# ------------------------------------------------------------ global

def enc_global(table):
    n = len(table.rows)
    cols = columns_of(table)
    keys = by_cardinality(cols)
    if n and keys:
        ids = [factorize(cols[j]) for j in keys]
        perm = np.lexsort(tuple(reversed(ids)))       # first key is primary
    else:
        perm = np.arange(n)
    out = [[col[i] for i in perm] for col in cols]
    return container({"m": "global", "c": table.columns, "n": n},
                     pack_columns(out), pack_perm(perm))


def _unpermute(cols, perm, n):
    res = []
    for col in cols:
        orig = [None] * n
        for k, i in enumerate(perm):
            orig[i] = col[k]
        res.append(orig)
    return res


def dec_global(blob):
    meta, payload, pb = open_container(blob)
    n = meta["n"]
    cols = unpack_columns(payload, len(meta["c"]), n)
    perm = unpack_perm(pb, n) if n else np.arange(0)
    return dtz.Table(meta["c"], rows_of(_unpermute(cols, perm, n), n))


# ------------------------------------------------------------ chain

def chain_order(cols) -> np.ndarray:
    """Greedy nearest-neighbour path through the rows.

    Exact nearest neighbours are O(n^2), so candidates come from a few cheap
    orderings: rows adjacent in any of CHAIN_ORDERS lexicographic sorts (by
    cardinality, by cell size, and one seeded shuffle of the columns) are the
    only ones considered. Distance is the number of differing cells, each
    weighted by its column's mean cell length -- a differing address costs
    more than a differing flag. When every candidate is used up, the path
    jumps to the next unvisited row of the first ordering.
    """
    n, m = len(cols[0]), len(cols)
    ids = np.stack([factorize(c) for c in cols], axis=1)            # n x m
    weight = np.array([1.0 + sum(len(c) for c in col) / n for col in cols])

    rng = np.random.default_rng(CHAIN_SEED)
    key_orders = [by_cardinality(cols),
                  sorted(range(m), key=lambda j: (-weight[j], j)),
                  list(rng.permutation(m))][:CHAIN_ORDERS]
    orders, where = [], []
    for ko in key_orders:
        o = np.lexsort(tuple(ids[:, j] for j in reversed(ko)))
        pos = np.empty(n, dtype=np.int64)
        pos[o] = np.arange(n)
        orders.append(o)
        where.append(pos)

    visited = np.zeros(n, dtype=bool)
    path = np.empty(n, dtype=np.int64)
    first, ptr = orders[0], 0
    r = int(first[0])
    for step in range(n):                     # exactly n rows placed
        visited[r] = True
        path[step] = r
        if step == n - 1:
            break
        cand = np.unique(np.concatenate([
            o[max(0, p - CHAIN_WINDOW):p + CHAIN_WINDOW + 1]
            for o, p in ((o, int(w[r])) for o, w in zip(orders, where))]))
        cand = cand[~visited[cand]]
        if cand.size:
            # elementwise, not `@`: Accelerate's matmul raises spurious FP
            # warnings on this Mac, and a NaN cost would steer the path
            cost = ((ids[cand] != ids[r]) * weight).sum(axis=1)
            r = int(cand[int(np.argmin(cost))])   # ties: lowest row index
        else:
            while visited[first[ptr]]:            # an unvisited row exists
                ptr += 1
            r = int(first[ptr])
    return path


def enc_chain(table):
    n = len(table.rows)
    cols = columns_of(table)
    perm = chain_order(cols) if n and cols else np.arange(n)
    out = [[col[i] for i in perm] for col in cols]
    return container({"m": "chain", "c": table.columns, "n": n},
                     pack_columns(out), pack_perm(perm))


dec_chain = dec_global


# ------------------------------------------------------------ context

def enc_context(table):
    n = len(table.rows)
    cols = columns_of(table)
    stored = by_cardinality(cols)
    out, ctx = [], []                 # ctx: ids of columns already stored
    for j in stored:
        col = cols[j]
        if ctx:
            perm = np.lexsort(tuple(reversed(ctx)))   # earliest is primary
            col = [col[i] for i in perm]
        out.append(col)
        ctx.append(factorize(cols[j]))
    return container({"m": "context", "c": table.columns, "n": n,
                      "s": stored}, pack_columns(out))


def dec_context(blob):
    meta, payload, _ = open_container(blob)
    n, stored = meta["n"], meta["s"]
    got = unpack_columns(payload, len(stored), n)
    cols, ctx = [None] * len(stored), []
    for j, col in zip(stored, got):
        if ctx:
            perm = np.lexsort(tuple(reversed(ctx)))
            orig = [None] * n
            for k, i in enumerate(perm):
                orig[i] = col[k]
            col = orig
        cols[j] = col
        ctx.append(factorize(col))
    return dtz.Table(meta["c"], rows_of(cols, n))


METHODS = [("none", enc_none, dec_none),
           ("parent", enc_parent, dec_parent),
           ("global", enc_global, dec_global),
           ("chain", enc_chain, dec_chain),
           ("context", enc_context, dec_context)]


# ------------------------------------------------------------ driver

def measure(path: str) -> dict:
    table = dtz.read_any(path)
    raw = open(path, "rb").read()
    res = {"name": os.path.basename(path), "bytes": len(raw),
           "rows": len(table.rows), "cols": len(table.columns)}
    t = time.perf_counter()
    res["xz_file"] = len(lzma.compress(raw, **XZ))
    res["t_xz_file"] = time.perf_counter() - t
    t = time.perf_counter()
    pp = fast.encode(table)
    res["t_polypress"] = time.perf_counter() - t
    if fast.decode(pp).rows != table.rows:
        raise SystemExit("polypress failed its own round trip on " + path)
    res["polypress"] = len(pp)
    for name, enc, dec in METHODS:
        t = time.perf_counter()
        blob = enc(table)
        res["t_" + name] = time.perf_counter() - t
        back = dec(blob)
        res[name] = len(blob)
        res["ok_" + name] = (back.columns == table.columns
                             and back.rows == table.rows)
    return res


def main(argv):
    out = None
    if "--json" in argv:
        i = argv.index("--json")
        out = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    results = []
    for p in argv:
        r = measure(p)
        results.append(r)
        cells = "  ".join("{}={:,}{}".format(
            m, r[m], "" if r["ok_" + m] else "(FAILED)") for m, _, _ in METHODS)
        print("{:24s} xz_file={:,}  polypress={:,}  {}".format(
            r["name"], r["xz_file"], r["polypress"], cells), flush=True)
        if out:
            with open(out, "w") as fh:
                json.dump(results, fh, indent=1)
    return results


if __name__ == "__main__":
    main(sys.argv[1:])
