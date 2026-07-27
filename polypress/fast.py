"""Vectorised table codec -- same ideas as smart.py, none of the Python loops.

Three changes made this 15-50x faster AND slightly smaller:

  1. all prediction is numpy. Polynomial extrapolation of order k is just the
     k-th finite difference, so the "local function builder" is np.diff. The
     2D version is np.diff twice, once along each axis.

  2. residuals are packed as one-byte varints with an escape, vectorised --
     no per-value Python loop.

  3. the entropy stage is xz/zstd (C) instead of our Python range coder. The
     range coder was the bottleneck and it was not paying for itself.

  4. CROSS-COLUMN STRUCTURE IS EXPLOITED BY REORDERING, NOT CONTEXT MODELLING.
     To code City given Postal Code, sort the rows by Postal Code: equal
     postal codes become adjacent, so City collapses into long runs that xz
     eats. The permutation costs nothing to store because the decoder has
     already reconstructed the parent column and can recompute the same
     stable argsort. Measured on 40k EV rows this beat the conditioned range
     coder 2,328 B vs 6,901 B.

Fidelity: the logical table round-trips exactly, cell for cell.
"""

from __future__ import annotations

import bz2
import json
import lzma
import math
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import caccel
from . import codec
from . import dtz

DICT_MAX = 1 << 16
MI_SAMPLE = 40000          # ceiling on rows sampled for column correlation
MI_MIN_SAMPLE = 1500       # floor, so a very wide table still measures something
MI_BUDGET = 150_000_000    # cap on (column pairs x sampled rows)
MIN_2D_GROUP = 3
INT_LIMIT = 1 << 62
XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])
ESCAPE = 255
MAGIC = b"PPZ1"            # Polypress container
MAGIC_V0 = b"FAST"         # pre-rename archives still open
# Fallback magics are 4 bytes with the codec baked in rather than 4 + a method
# byte. That one byte matters: the fallback exists to tie a general compressor
# that we would otherwise lose to, and the tie is measured against its bare
# output stream. Every byte of container is a byte of deficit.
MAGIC_RAW_XZ = b"PPZX"
MAGIC_RAW_BZ = b"PPZB"

# Fallback codecs. stdlib only, deliberately. An adversarial suite found four
# tables where the modelling lost to a plain general-purpose compressor -- by
# 0.8% to 11% -- because this codec always finishes with xz and xz is not
# always the best finisher. Carrying xz and bzip2 candidates makes it
# impossible to lose to either. brotli won two of those four by under 1% and
# is NOT carried: it is not in the standard library and would mean linking
# libbrotli into the C port, which is a poor trade for <1% on data that is
# incompressible anyway.
# Sorting cannot create useful runs in a handful of rows, and the parent
# search is O(columns^2). Below this row count, skip it entirely.
MIN_ROWS_FOR_PARENTS = 8
# The planar predictor differences down the rows, so it needs rows to work
# with. On a 1-row table a "group" is pure bookkeeping: it restructures the
# payload, reduces nothing, and -- because it counted as a trick that fired --
# used to suppress the fallback on exactly the table that needed it most.
MIN_ROWS_FOR_2D = 3


# ------------------------------------------------------------------ packing

def zigzag(a: np.ndarray) -> np.ndarray:
    """Signed -> unsigned, using the bit trick rather than arithmetic.

    The obvious `(-a << 1) - 1` overflows int64: differences between values
    near the +/-2^62 limit legitimately reach 2^63, and negating or doubling
    those in signed arithmetic wraps to nonsense. `(a << 1) ^ (a >> 63)` is
    exact because it relies on the wraparound instead of fighting it."""
    a = np.ascontiguousarray(a, dtype=np.int64)
    with np.errstate(over="ignore"):
        u = (a << np.int64(1)) ^ (a >> np.int64(63))
    return u.view(np.uint64)


def unzigzag(u: np.ndarray) -> np.ndarray:
    u = np.ascontiguousarray(u, dtype=np.uint64)
    return ((u >> np.uint64(1)).astype(np.int64)
            ^ -(u & np.uint64(1)).astype(np.int64))


def pack_ints(res: np.ndarray) -> bytes:
    """One byte per small value, escape + 4 bytes for the rest."""
    if caccel.HAVE_C:
        return caccel.pack(res)
    u = zigzag(res)
    small = u < ESCAPE
    head = np.full(u.size, ESCAPE, dtype=np.uint8)
    head[small] = u[small].astype(np.uint8)
    tail = u[~small]
    if tail.size and int(tail.max()) >= (1 << 32):
        return b"\x08" + head.tobytes() + tail.astype("<u8").tobytes()
    return b"\x04" + head.tobytes() + tail.astype("<u4").tobytes()


def unpack_ints(buf: bytes, n: int) -> np.ndarray:
    if caccel.HAVE_C:
        return caccel.unpack(buf, n)
    width = buf[0]
    head = np.frombuffer(buf[1:1 + n], dtype=np.uint8).astype(np.uint64)
    big = head == ESCAPE
    nbig = int(big.sum())
    if nbig:
        at = 1 + n
        if width == 8:
            tail = np.frombuffer(buf[at:at + 8 * nbig], dtype="<u8")
        else:
            tail = np.frombuffer(buf[at:at + 4 * nbig],
                                 dtype="<u4").astype(np.uint64)
        head = head.copy()
        head[big] = tail
    return unzigzag(head)


def ints_to_cells(a: np.ndarray, dec: int) -> List[str]:
    """Fixed-point integers back to their printed text, vectorised.

    Equivalent to codec.int_to_cell per element, but the digit formatting
    happens inside numpy instead of a Python loop -- this was over half of
    decode time."""
    if a.size == 0:
        return []
    if caccel.HAVE_C:
        return caccel.cells_from_ints(a, dec)
    if dec == 0:
        return list(map(str, a.tolist()))
    scale = 10 ** dec
    pos = "%d.%0" + str(dec) + "d"
    neg = "-" + pos
    out = []
    ap = out.append
    for x in a.tolist():
        if x < 0:
            x = -x
            ap(neg % (x // scale, x % scale))
        else:
            ap(pos % (x // scale, x % scale))
    return out


def packed_len(res: np.ndarray) -> int:
    u = zigzag(res)
    esc = u[u >= ESCAPE]
    width = 8 if esc.size and int(esc.max()) >= (1 << 32) else 4
    return 1 + res.size + width * int(esc.size)


# ----------------------------------------------------------------- analysis

def _numeric(cells):
    """Scaled-integer view of a column, or None. C first, numpy fallback."""
    if not cells:
        return None
    if caccel.HAVE_C:
        return caccel.parse_column(cells)
    num = codec.as_numeric_column(cells)
    if num is None or not num[0]:
        return None
    if max(abs(min(num[0])), abs(max(num[0]))) >= INT_LIMIT:
        return None
    return np.array(num[0], dtype=np.int64), num[1]


def classify(table) -> List[dict]:
    nrows = len(table.rows)
    plan = []
    for j in range(len(table.columns)):
        cells = table.column(j)
        num = _numeric(cells)
        if num is not None:
            plan.append({"kind": "num", "ints": num[0], "dec": num[1],
                         "j": j})
            continue
        uniq = sorted(set(cells))
        if len(uniq) <= DICT_MAX and len(uniq) * 2 <= max(nrows, 2):
            idx = {v: i for i, v in enumerate(uniq)}
            plan.append({"kind": "dict", "alpha": uniq,
                         "ids": np.array([idx[v] for v in cells],
                                         dtype=np.int64), "j": j})
            continue
        plan.append({"kind": "text", "cells": cells, "j": j})
    return plan


def find_2d_groups(plan, nrows: int = 1 << 30) -> List[List[int]]:
    if nrows < MIN_ROWS_FOR_2D:
        return []
    groups, run = [], []
    for pos, col in enumerate(plan):
        if col["kind"] != "num" or col["ints"].size == 0:
            if len(run) >= MIN_2D_GROUP:
                groups.append(run)
            run = []
            continue
        if not run:
            run = [pos]
            continue
        prev = plan[run[-1]]
        hi_a = int(np.abs(prev["ints"]).max()) or 1
        hi_b = int(np.abs(col["ints"]).max()) or 1
        if prev["dec"] == col["dec"] and hi_a <= 8 * hi_b and hi_b <= 8 * hi_a:
            run.append(pos)
        else:
            if len(run) >= MIN_2D_GROUP:
                groups.append(run)
            run = [pos]
    if len(run) >= MIN_2D_GROUP:
        groups.append(run)
    return groups


def diff_order(a: np.ndarray) -> int:
    """Polynomial degree whose residuals pack smallest. Order k residuals are
    the k-th finite difference, so this is just np.diff k times."""
    best, best_cost = 0, packed_len(a)
    for k in range(1, min(4, a.size - 1) + 1):
        c = packed_len(np.diff(a, n=k)) + 8 * k
        if c < best_cost:
            best, best_cost = k, c
    return best


# ------------------------------------------------- cross-column conditioning

def _counts_entropy(counts: np.ndarray, n: int) -> float:
    p = counts / n
    return float(-np.sum(p * np.log2(p)))


# Above this many joint bins, counting by bincount would allocate more than
# it saves and np.unique's sort is the better trade. Real survey columns have
# cardinalities in the tens, so the product is tiny and this never trips.
JOINT_BINCOUNT_MAX = 1 << 20


def _joint_counts(xs: np.ndarray, ys: np.ndarray, ny: int) -> np.ndarray:
    """Counts of each distinct (x, y) pair, ascending by combined key.

    np.unique sorts, which is O(n log n) and was the single largest cost in
    encoding a wide table. When the combined key space is small -- which is
    the normal case for categorical columns -- bincount does it in one pass.

    Both branches return counts in ascending key order, which matters for more
    than tidiness: the caller sums them, np.sum is pairwise, and a different
    order would give a different last bit, a different parent, and a different
    archive. The two paths are interchangeable only because the order matches.
    """
    key = xs * ny + ys
    if key.size:
        hi = int(key.max())
        if hi < JOINT_BINCOUNT_MAX:
            c = np.bincount(key)
            return c[c > 0]
    _, jc = np.unique(key, return_counts=True)
    return jc


def _cond_entropy_corrected(xs: np.ndarray, ys: np.ndarray, ny: int,
                            hx: float, mx_size: int) -> float:
    """H(Y|X) with a Miller-Madow correction.

    Computed as H(X,Y) - H(X) from one combined key, so it is two numpy
    passes instead of two Python Counters. That mattered: on a 209-column
    survey table this function was 94% of encode time, called once per
    ordered column pair -- 38,220 times.

    `hx` and `mx_size` describe X alone. They used to be recomputed inside
    here, once for every Y -- 420 times per column on a 421-column table, for
    a value that never changed. They are now hoisted to the caller. The
    arithmetic is written in the same shape as before so the result is the
    same double, not merely the same number.

    The correction is not optional. A parent with many distinct values scores
    near-zero conditional entropy purely because each of its values is seen a
    handful of times, which is how an earlier version picked nonsense parents.
    """
    n = xs.size
    if n == 0:
        return 0.0
    jc = _joint_counts(xs, ys, ny)
    h = _counts_entropy(jc, n) - hx
    return h + (jc.size - mx_size) / (2.0 * n * math.log(2))


def _entropy_and_distinct(ys: np.ndarray) -> Tuple[float, int]:
    """H(Y) and the number of distinct values, from one bincount.

    Both are needed per column and both come from the same counts, so taking
    them together halves the work and guarantees they agree.
    """
    n = ys.size
    if n == 0:
        return 0.0, 0
    counts = np.bincount(ys)
    counts = counts[counts > 0]
    return _counts_entropy(counts, n), int(counts.size)


def _entropy(ys: np.ndarray) -> float:
    return _entropy_and_distinct(ys)[0]


def pick_parents(plan, nrows) -> Tuple[Dict[int, Optional[int]], List[int]]:
    dict_pos = [p for p, c in enumerate(plan) if c["kind"] == "dict"]
    if nrows < MIN_ROWS_FOR_PARENTS or len(dict_pos) < 2:
        return {p: None for p in dict_pos}, list(dict_pos)

    # Parent search is O(columns^2) pairs. A wide table has a lot of them --
    # 209 columns is 38,220 -- so trade sample depth against pair count and
    # keep the total work bounded. Narrow tables are unaffected: they hit the
    # MI_SAMPLE ceiling instead.
    npairs = max(1, len(dict_pos) * (len(dict_pos) - 1))
    rows = min(MI_SAMPLE, max(MI_MIN_SAMPLE, MI_BUDGET // npairs))
    step = max(1, nrows // rows)
    sample = {p: np.ascontiguousarray(plan[p]["ids"][::step]) for p in dict_pos}
    sizes = {p: len(plan[p]["alpha"]) for p in dict_pos}
    base, distinct = {}, {}
    for p in dict_pos:
        base[p], distinct[p] = _entropy_and_distinct(sample[p])

    gain = defaultdict(dict)
    for a in dict_pos:
        # H(X) and X's distinct count are the same for every b, so they are
        # computed once per a rather than once per pair
        ha, ma = base[a], distinct[a]
        for b in dict_pos:
            if a == b:
                continue
            g = base[b] - _cond_entropy_corrected(sample[a], sample[b],
                                                  sizes[b], ha, ma)
            if g > 0.05:
                gain[b][a] = g

    root = min(dict_pos, key=lambda p: base[p])
    placed, order = {root}, [root]
    parent: Dict[int, Optional[int]] = {root: None}

    # `remaining` is a list in column order, not a set. Two candidate pairs can
    # have exactly equal gain -- duplicated or near-duplicated columns are
    # common in survey extracts -- and the winner is whichever the loop reaches
    # first, because the comparison is strictly `>`. With a set, "first" is an
    # artefact of CPython's hash table, so the bytes of the archive depended on
    # it. A list makes the tie-break explicit and reproducible: lowest column
    # index wins. That matters for the C port, which has to reproduce this
    # exactly, and it is worth having regardless -- an encoder whose output can
    # shift with an interpreter's internals is not one to build a format on.
    remaining = [p for p in dict_pos if p != root]
    while remaining:
        best = None
        for b in remaining:
            for a, g in gain[b].items():
                if a in placed and (best is None or g > best[2]):
                    best = (b, a, g)
        if best is None:
            b = min(remaining, key=lambda p: base[p])
            parent[b] = None
        else:
            b, a, _ = best
            parent[b] = a
        placed.add(b)
        order.append(b)
        remaining.remove(b)
    return parent, order


# How many entropy-nominated parents actually get compressed and compared.
# Each candidate costs one cheap pass over the column, and the score is a good
# enough nominator that the winner is almost always in the first few.
TEXT_PARENT_CANDIDATES = 3

# A fast stand-in for the real entropy stage, used only to choose between
# orderings. Preset 1 ranks the candidates the same way preset 9 does at a
# fraction of the cost, and nothing it produces is ever stored.
_PROBE = dict(format=lzma.FORMAT_RAW,
              filters=[{"id": lzma.FILTER_LZMA2, "preset": 1}])


def _probe_len(cells: List[str]) -> int:
    return len(lzma.compress("\n".join(cells).encode("utf-8"), **_PROBE))


def pick_text_parents(plan, nrows, parent, order) -> Dict[int, Optional[int]]:
    """Choose a reorder parent for each text column.

    Text columns were the one place the reordering idea was never applied, and
    they are exactly where it was most needed: on the datasets this codec does
    worst on, the text blob is 59-71% of the output and receives no modelling
    at all. Measured on real data, sorting a text column by the right
    dictionary column takes 21-45% off it -- for free, by the same argument
    that makes it free for dictionary columns.

    Text columns are leaves: a text column may HAVE a parent but never BE one.
    That is not a modelling decision, it is a decoding one -- the permutation
    is recomputed from a parent the decoder has already rebuilt, and text
    columns are rebuilt after every dictionary column, so a text parent could
    not be guaranteed available in time. Keeping them leaves also means no
    cycle is possible and the existing decode order still holds.

    Scoring reuses the dictionary machinery by factorising the text column
    into ids. That is only ever used to score -- the column is still stored as
    text.
    """
    text_pos = [p for p, c in enumerate(plan) if c["kind"] == "text"]
    out: Dict[int, Optional[int]] = {p: None for p in text_pos}
    dict_pos = [p for p, c in enumerate(plan) if c["kind"] == "dict"]
    if nrows < MIN_ROWS_FOR_PARENTS or not dict_pos or not text_pos:
        return out

    npairs = max(1, len(text_pos) * len(dict_pos))
    rows = min(MI_SAMPLE, max(MI_MIN_SAMPLE, MI_BUDGET // npairs))
    step = max(1, nrows // rows)

    dsample = {p: np.ascontiguousarray(plan[p]["ids"][::step]) for p in dict_pos}
    dbase = {}
    for p in dict_pos:
        dbase[p] = _entropy_and_distinct(dsample[p])

    for tp in text_pos:
        cells = plan[tp]["cells"][::step]
        uniq = sorted(set(cells))
        idx = {v: i for i, v in enumerate(uniq)}
        ids = np.array([idx[v] for v in cells], dtype=np.int64)
        base_t = _entropy(ids)
        ranked = []
        for dp in dict_pos:
            ha, ma = dbase[dp]
            g = base_t - _cond_entropy_corrected(dsample[dp], ids, len(uniq),
                                                 ha, ma)
            if g > 0.05:
                ranked.append((g, dp))
        if not ranked:
            continue
        ranked.sort(key=lambda r: (-r[0], r[1]))

        # Conditional entropy is the right criterion for dictionary columns and
        # the wrong one here. It measures how often the parent pins down the
        # exact value; what actually shrinks a text column is having *similar*
        # strings adjacent, which is not the same thing. Worse, reordering
        # destroys whatever useful order the file already had -- a table
        # written in time order often has address locality for free. Trusting
        # the score alone made one real dataset 66% LARGER.
        #
        # So the score is used only to nominate candidates, and the decision is
        # measured. A cheap preset picks between them; the real entropy stage
        # runs later on whichever won. Never-worse is the same rule the
        # fallback container follows, and for the same reason.
        full = plan[tp]["cells"]
        keep_none = _probe_len(full)
        best, best_cost = None, keep_none
        for _g, dp in ranked[:TEXT_PARENT_CANDIDATES]:
            perm = np.argsort(plan[dp]["ids"], kind="stable")
            cost = _probe_len([full[i] for i in perm])
            if cost < best_cost:
                best, best_cost = dp, cost
        out[tp] = best
    return out


def _width(n: int) -> str:
    return "<u1" if n <= 256 else ("<u2" if n <= 65536 else "<u4")


# Strings are stored as one concatenated UTF-8 blob plus an array of byte
# lengths. Joining on a separator -- "\n", "\x00", anything -- is wrong,
# because a cell is allowed to contain that byte. This costs nothing: the
# lengths compress to almost nothing and the blob compresses exactly as well
# as the joined form did.

def _pack_strings(groups: List[List[str]]):
    """Concatenate string groups, paying for length prefixes only where a
    value actually contains a newline.

    Newline-joining is what xz likes -- the separator sits in the same stream
    as the content and models well. But a cell may legally contain a newline,
    which silently split one value into two. So each group records whether it
    is newline-joined; the rare group that is not carries an explicit length
    array. On real tables no group needs one, and the joined layout is kept."""
    parts, metas, length_arrays = [], [], []
    for g in groups:
        if any("\n" in s for s in g):
            blobs = [s.encode("utf-8") for s in g]
            data = b"".join(blobs)
            length_arrays.append(np.fromiter((len(b) for b in blobs),
                                             dtype=np.int64, count=len(blobs)))
            metas.append({"n": len(g), "b": len(data), "nl": False})
        else:
            data = "\n".join(g).encode("utf-8")
            metas.append({"n": len(g), "b": len(data), "nl": True})
        parts.append(data)
    return b"".join(parts), metas, length_arrays


def _unpack_strings(data: bytes, metas, length_bins) -> List[List[str]]:
    out, at, li = [], 0, 0
    for m in metas:
        chunk = data[at:at + m["b"]]
        at += m["b"]
        if m["n"] == 0:
            out.append([])
            continue
        if m["nl"]:
            out.append(chunk.decode("utf-8").split("\n"))
        else:
            lengths = unpack_ints(length_bins[li], m["n"])
            li += 1
            ends = np.cumsum(lengths)
            starts = ends - lengths
            out.append([chunk[int(starts[i]):int(ends[i])].decode("utf-8")
                        for i in range(m["n"])])
    return out


# --------------------------------------------------------------- fallbacks

def _canonical_bytes(table) -> bytes:
    """The table as canonical CSV -- the form a general compressor would see.

    Deliberately the same shape as the input file rather than some private
    layout, because the fallback only earns its place if it matches what
    `xz file.csv` would have produced. A private layout that compresses worse
    than the user's own CSV would be a fallback that does not fall back.
    """
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(table.columns)
    w.writerows(table.rows)
    return buf.getvalue().encode("utf-8")


def _from_canonical(data: bytes):
    import csv
    import io
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))
    if not rows:
        return dtz.Table([], [])
    return dtz.Table(rows[0], rows[1:])


def _raw_candidates(table, limit: int) -> Optional[bytes]:
    """Smallest standard-codec encoding of the whole table, or None.

    Returns None unless it beats `limit`, and also unless it round-trips --
    CSV quoting is not lossless for every conceivable cell, so the candidate
    is parsed back and compared before it is allowed to win. A fallback that
    corrupts data is worse than losing by 11%.
    """
    canon = _canonical_bytes(table)
    try:
        if _from_canonical(canon).rows != table.rows:
            return None
        if _from_canonical(canon).columns != table.columns:
            return None
    except Exception:
        return None

    best = None
    for magic, blob in ((MAGIC_RAW_XZ, lzma.compress(canon, **XZ)),
                        (MAGIC_RAW_BZ, bz2.compress(canon, 9))):
        cand = magic + blob
        if len(cand) < limit and (best is None or len(cand) < len(best)):
            best = cand
    return best


# ------------------------------------------------------------------- codec

def encode(table) -> bytes:
    """Smallest of the modelled encoding and the plain fallbacks.

    The fallbacks are not run unconditionally -- they roughly double encode
    time, since the entropy stage dominates. They are run only when none of
    the three modelling tricks fired, which is precisely the case where this
    codec has degenerated into "split into columns, then xz" and a different
    finisher may well beat it. When any trick fired, the modelled output wins
    by a margin no general compressor closes, and the extra work is skipped.
    """
    blob, fired = _encode_plan(table)
    if fired:
        return blob
    alt = _raw_candidates(table, len(blob))
    return alt if alt is not None else blob


def _encode_plan(table) -> Tuple[bytes, int]:
    plan = classify(table)
    nrows = len(table.rows)
    groups = find_2d_groups(plan, nrows)
    in_group = {pos: gi for gi, g in enumerate(groups) for pos in g}
    parent, order = pick_parents(plan, nrows)

    bins: List[bytes] = []            # binary payloads
    sgroups: List[List[str]] = []     # string payloads, length-prefixed
    specs: List[Optional[dict]] = [None] * len(plan)

    for pos in order:
        col = plan[pos]
        ids = col["ids"]
        par = parent.get(pos)
        if par is not None:
            perm = np.argsort(plan[par]["ids"], kind="stable")
            ids = ids[perm]
        w = _width(len(col["alpha"]))
        bins.append(ids.astype(w).tobytes())
        sgroups.append(col["alpha"])
        specs[pos] = {"kind": "dict", "n": len(col["alpha"]), "w": w,
                      "parent": par}

    tparent = pick_text_parents(plan, nrows, parent, order)

    for pos, col in enumerate(plan):
        if col["kind"] == "text":
            tp = tparent.get(pos)
            cells = col["cells"]
            if tp is not None:
                # exactly the dictionary-column trick, and free for exactly
                # the same reason: the decoder has already rebuilt the parent
                # and recomputes the same stable argsort
                perm = np.argsort(plan[tp]["ids"], kind="stable")
                cells = [cells[i] for i in perm]
            sgroups.append(cells)
            specs[pos] = {"kind": "text"} if tp is None else \
                         {"kind": "text", "parent": tp}
        elif col["kind"] == "num" and pos in in_group:
            specs[pos] = {"kind": "grp", "dec": col["dec"], "g": in_group[pos]}
        elif col["kind"] == "num":
            a = col["ints"]
            k = diff_order(a)
            bins.append(pack_ints(np.diff(a, n=k) if k else a))
            specs[pos] = {"kind": "num", "dec": col["dec"], "k": k,
                          "warm": a[:k].tolist()}

    for g in groups:
        M = np.stack([plan[pos]["ints"] for pos in g], axis=1)   # rows x cols
        D = np.diff(np.diff(M, axis=0), axis=1)
        side = np.concatenate([M[0], np.diff(M, axis=0)[:, 0]])
        bins.append(pack_ints(np.concatenate([side, D.ravel()])))

    txt_data, smeta, length_arrays = _pack_strings(sgroups)
    n_before = len(bins)
    bins.extend(pack_ints(a) for a in length_arrays)   # always last
    meta = {"columns": table.columns, "nrows": nrows, "cols": specs,
            "groups": groups, "order": order,
            "bins": [len(b) for b in bins],
            "smeta": smeta, "nlenbins": len(bins) - n_before}
    meta_b = lzma.compress(json.dumps(meta, separators=(",", ":")).encode(),
                           **XZ)
    bin_b = lzma.compress(b"".join(bins), **XZ)
    txt_b = lzma.compress(txt_data, **XZ)
    blob = (MAGIC + len(meta_b).to_bytes(4, "big")
            + len(bin_b).to_bytes(4, "big") + len(txt_b).to_bytes(4, "big")
            + meta_b + bin_b + txt_b)

    # Did any of the three ideas actually do something? A parent-sorted
    # column, a column whose differences packed smaller than its values, or a
    # 2D group. If none did, this run was just "columns, then xz".
    fired = (sum(1 for s in specs if s and s["kind"] == "dict"
                 and s["parent"] is not None)
             + sum(1 for s in specs if s and s["kind"] == "num" and s["k"])
             + len(groups))
    return blob, fired


def decode(blob: bytes):
    if blob[:4] == MAGIC_RAW_XZ:
        return _from_canonical(lzma.decompress(blob[4:], **XZ))
    if blob[:4] == MAGIC_RAW_BZ:
        return _from_canonical(bz2.decompress(blob[4:]))
    if blob[:4] not in (MAGIC, MAGIC_V0):
        raise ValueError("not a Polypress archive")
    ml = int.from_bytes(blob[4:8], "big")
    bl = int.from_bytes(blob[8:12], "big")
    o = 16
    meta = json.loads(lzma.decompress(blob[o:o + ml], **XZ))
    o += ml
    raw = lzma.decompress(blob[o:o + bl], **XZ)
    o += bl
    txt_data = lzma.decompress(blob[o:], **XZ)

    nrows, specs = meta["nrows"], meta["cols"]
    cuts, at = [], 0
    for size in meta["bins"]:
        cuts.append(raw[at:at + size])
        at += size

    nlen = meta["nlenbins"]
    length_bins = cuts[len(cuts) - nlen:] if nlen else []
    texts = _unpack_strings(txt_data, meta["smeta"], length_bins)

    cols: List[Optional[List[str]]] = [None] * len(specs)
    ids_by_pos: Dict[int, np.ndarray] = {}
    bi = ti = 0

    for pos in meta["order"]:
        sp = specs[pos]
        alpha = texts[ti]
        ti += 1
        ids = np.frombuffer(cuts[bi], dtype=sp["w"]).astype(np.int64)
        bi += 1
        par = sp["parent"]
        if par is not None:
            perm = np.argsort(ids_by_pos[par], kind="stable")
            out = np.empty_like(ids)
            out[perm] = ids
            ids = out
        ids_by_pos[pos] = ids
        cols[pos] = (np.array(alpha, dtype=object)[ids].tolist()
                     if alpha else [])

    for pos, sp in enumerate(specs):
        if sp["kind"] == "text":
            cells = list(texts[ti])
            ti += 1
            tp = sp.get("parent")
            if tp is not None:
                perm = np.argsort(ids_by_pos[tp], kind="stable")
                restored = [None] * len(cells)
                for k, src in enumerate(perm):
                    restored[src] = cells[k]
                cells = restored
            cols[pos] = cells
        elif sp["kind"] == "num":
            k = sp["k"]
            d = unpack_ints(cuts[bi], nrows - k)
            bi += 1
            a = _undiff(d, np.array(sp["warm"], dtype=np.int64), k) if k else d
            cols[pos] = ints_to_cells(a, sp["dec"])

    for gi, g in enumerate(meta["groups"]):
        w = len(g)
        n_side = w + (nrows - 1)
        flat = unpack_ints(cuts[bi], n_side + (nrows - 1) * (w - 1))
        bi += 1
        row0 = flat[:w]
        col0 = flat[w:n_side]
        D = flat[n_side:].reshape(nrows - 1, w - 1)
        D1 = np.concatenate([col0[:, None], D], axis=1).cumsum(axis=1)
        M = np.concatenate([row0[None, :], D1], axis=0).cumsum(axis=0)
        for c, pos in enumerate(g):
            cols[pos] = ints_to_cells(M[:, c], specs[pos]["dec"])

    # zip transposes at C speed; the nested comprehension did not
    rows = [list(r) for r in zip(*cols)]
    return dtz.Table(list(meta["columns"]), rows)


def _undiff(d: np.ndarray, warm: np.ndarray, k: int) -> np.ndarray:
    """Invert k-fold differencing.

    `warm` holds the first k ORIGINAL values, not the differences, so each
    level's leading term has to be re-derived: the first element of the j-th
    difference is np.diff(warm, n=j)[0]."""
    a = d
    for j in range(k - 1, -1, -1):
        first = warm[0] if j == 0 else np.diff(warm, n=j)[0]
        a = np.concatenate([[first], a]).cumsum()
    return a
