"""A single table codec -- not a bake-off.

Four ideas, each applied where the data says it belongs:

  1. dictionary columns (few distinct responses: "yes"/"no"/"unknown", or a
     few thousand city names)
     mapped to 0..n-1 and range-coded. Fractional bits per symbol, so they
     reach the entropy floor that Rice coding cannot.

  2. CROSS-COLUMN CONDITIONING
     the columns of a table are not independent -- City is nearly determined
     by Postal Code. We measure mutual information between every pair of
     dictionary columns, build a maximum spanning tree over it (Chow-Liu),
     and code each column conditioned on its strongest correlate. This is the
     soft version of a functional dependency: it pays off whenever the parent
     is *informative*, not only when it is *determining*.

  3. numeric columns
     the local function builder: fit a low-degree polynomial to the last few
     values, extrapolate, store only the error. The fit is re-centred at every
     cell, so coefficients never have to be stored.

  4. groups of commensurable numeric columns (same decimals, same scale)
     the local function builder in TWO dimensions: predict a cell from its
     left, upper and upper-left neighbours. Only where the columns are truly
     comparable -- differencing "Model Year" against "Make" is meaningless.

  anything left over -> the raw text falls back to xz.

Round-trip is verified before any output is trusted.
"""

from __future__ import annotations

import json
import lzma
import math
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

import codec
import dtz
import rc

DICT_MAX = 4096            # most distinct values we will dictionary-encode
MI_SAMPLE = 40000          # rows sampled when measuring column correlation
MIN_ROWS_PER_CTX = 64      # data needed per context before a model can learn
LEARN_BITS = 3.0           # bits it costs to train one fresh context
MIN_2D_GROUP = 3
XZ = dict(preset=9 | lzma.PRESET_EXTREME)
FIXED = {0: [], 1: [1], 2: [2, -1], 3: [3, -3, 1], 4: [4, -6, 4, -1]}


# ----------------------------------------------------------------- analysis

def classify(table) -> List[dict]:
    nrows = len(table.rows)
    plan = []
    for j in range(len(table.columns)):
        cells = table.column(j)
        num = codec.as_numeric_column(cells) if cells else None
        if num is not None:
            ints, dec = num
            plan.append({"kind": "num", "ints": ints, "dec": dec, "j": j})
            continue
        uniq = sorted(set(cells))
        if len(uniq) <= DICT_MAX and len(uniq) * 2 <= max(nrows, 2):
            idx = {v: i for i, v in enumerate(uniq)}
            ids = [idx[v] for v in cells]
            # Does a dictionary actually pay here? Storing the alphabet costs
            # real bytes, and xz on the raw column is a strong competitor when
            # the values are long and highly repetitive.
            alpha_cost = len(lzma.compress("\n".join(uniq).encode(), **XZ))
            ids_cost = nrows * _entropy_prev(ids, len(uniq)) / 8.0
            text_cost = len(lzma.compress("\n".join(cells).encode(), **XZ))
            if alpha_cost + ids_cost < text_cost:
                plan.append({"kind": "dict", "alpha": uniq, "ids": ids,
                             "j": j})
                continue
        plan.append({"kind": "text", "cells": cells, "j": j})
    return plan


def _span(ints):
    return min(ints), max(ints)


def find_2d_groups(plan) -> List[List[int]]:
    groups, run = [], []
    for pos, col in enumerate(plan):
        if col["kind"] != "num" or not col["ints"]:
            if len(run) >= MIN_2D_GROUP:
                groups.append(run)
            run = []
            continue
        if not run:
            run = [pos]
            continue
        prev = plan[run[-1]]
        _, hi_a = _span(prev["ints"])
        _, hi_b = _span(col["ints"])
        scale_ok = max(abs(hi_a), 1) <= 8 * max(abs(hi_b), 1) and \
            max(abs(hi_b), 1) <= 8 * max(abs(hi_a), 1)
        if prev["dec"] == col["dec"] and scale_ok:
            run.append(pos)
        else:
            if len(run) >= MIN_2D_GROUP:
                groups.append(run)
            run = [pos]
    if len(run) >= MIN_2D_GROUP:
        groups.append(run)
    return groups


def best_order(ints) -> int:
    best, best_cost = 0, None
    for o in range(min(4, max(0, len(ints) - 1)) + 1):
        co = FIXED[o]
        cost = 0
        for i in range(o, len(ints)):
            p = 0
            for k, cf in enumerate(co):
                p += cf * ints[i - 1 - k]
            cost += rc.zigzag(ints[i] - p).bit_length() + 1
        if best_cost is None or cost < best_cost:
            best, best_cost = o, cost
    return best


# ------------------------------------------------- cross-column conditioning

def _cond_entropy(xs, ys) -> float:
    """H(Y | X) in bits per symbol -- how many bits Y still costs once X is
    known. Lower is a better parent."""
    n = len(xs)
    joint = Counter(zip(xs, ys))
    marg = Counter(xs)
    h = 0.0
    for (x, y), c in joint.items():
        h -= (c / n) * math.log2(c / marg[x])
    return h


def _entropy(ys) -> float:
    n = len(ys)
    return sum(-(c / n) * math.log2(c / n) for c in Counter(ys).values())


def use_prev_ctx(nalpha: int, nrows: int) -> bool:
    return nalpha * MIN_ROWS_PER_CTX <= nrows


def _entropy_prev(ids, nalpha: int) -> float:
    """Bits per symbol once the previous value in the same column is known --
    what the codec will actually pay for an unparented column.

    If the alphabet is large relative to the row count, conditioning on the
    previous value measures beautifully and delivers nothing: every context
    is visited a handful of times and the adaptive model never converges. In
    that regime the honest estimate is the plain order-0 entropy, which is
    also what the coder will fall back to."""
    if len(ids) < 2 or not use_prev_ctx(nalpha, len(ids)):
        return _entropy(ids)
    return _cond_entropy(ids[:-1], ids[1:])


def chow_liu(plan, nrows: int) -> Tuple[Dict[int, Optional[int]], List[int]]:
    """Choose a parent for each dictionary column, then return a
    parent-before-child ordering. Cycles are impossible because we grow a
    tree outward from an already-placed set."""
    dict_pos = [p for p, c in enumerate(plan) if c["kind"] == "dict"]
    if len(dict_pos) < 2:
        return {p: None for p in dict_pos}, list(dict_pos)

    step = max(1, nrows // MI_SAMPLE)
    sample = {p: plan[p]["ids"][::step] for p in dict_pos}
    base = {p: _entropy(sample[p]) for p in dict_pos}

    # gain[a][b] = bits per symbol saved on b by conditioning on a.
    #
    # Two guards, both learned the hard way. A parent with many distinct
    # values splits the child into contexts that each see almost no data:
    # the measured conditional entropy looks wonderful on a sample and the
    # adaptive model never gets to exploit it. So (1) demand enough rows per
    # context to learn anything, and (2) charge the gain for the cost of
    # training every context it creates.
    gain = defaultdict(dict)
    for a in dict_pos:
        pa = len(plan[a]["alpha"])
        if pa * MIN_ROWS_PER_CTX > nrows:
            continue                                   # guard 1: too sparse
        for b in dict_pos:
            if a == b:
                continue
            nb = max(1, (len(plan[b]["alpha"]) - 1).bit_length())
            g = base[b] - _cond_entropy(sample[a], sample[b])
            saved = g * nrows
            training = LEARN_BITS * pa * nb            # guard 2: model cost
            if saved > training and g > 0.01:
                gain[b][a] = g

    # grow a tree: repeatedly attach the unplaced column with the best parent
    root = min(dict_pos, key=lambda p: base[p])
    placed, order = {root}, [root]
    parent: Dict[int, Optional[int]] = {root: None}
    remaining = set(dict_pos) - placed
    while remaining:
        best = None
        for b in remaining:
            for a, g in gain[b].items():
                if a in placed and (best is None or g > best[2]):
                    best = (b, a, g)
        if best is None:                       # nothing correlated is left
            b = min(remaining, key=lambda p: base[p])
            parent[b] = None
        else:
            b, a, _ = best
            parent[b] = a
        placed.add(b)
        order.append(b)
        remaining.discard(b)
    return parent, order


# ------------------------------------------------------------------- codec

def _dict_streams(plan, parent, order, nrows):
    """Yield (pos, spec, ids, ctx_ids, nb) in coding order."""
    for pos in order:
        col = plan[pos]
        a = len(col["alpha"])
        nb = max(1, (a - 1).bit_length())
        par = parent.get(pos)
        ctx = plan[par]["ids"] if par is not None else None
        yield pos, col, nb, par, ctx


def encode(table) -> bytes:
    plan = classify(table)
    nrows = len(table.rows)
    groups = find_2d_groups(plan)
    in_group = {pos: gi for gi, g in enumerate(groups) for pos in g}
    parent, order = chow_liu(plan, nrows)

    enc = rc.Encoder()
    specs: List[dict] = [None] * len(plan)
    text_blobs: List[str] = []

    # --- dictionary columns, parent before child
    for pos, col, nb, par, ctx in _dict_streams(plan, parent, order, nrows):
        model = rc.Model()
        shift = nb + 1
        ids = col["ids"]
        a_n = len(col["alpha"])
        if ctx is None:
            if use_prev_ctx(a_n, nrows):
                prev = a_n                    # dedicated start context
                for v in ids:
                    enc.tree_ctx(model, nb, v, prev << shift)
                    prev = v
            else:
                for v in ids:                 # too sparse to condition
                    enc.tree(model, nb, v)
        else:
            for v, c in zip(ids, ctx):
                enc.tree_ctx(model, nb, v, c << shift)
        specs[pos] = {"kind": "dict", "alpha": col["alpha"], "nb": nb,
                      "parent": par, "prev": use_prev_ctx(a_n, nrows)}

    # --- numeric columns
    for pos, col in enumerate(plan):
        if col["kind"] == "text":
            text_blobs.append("\n".join(col["cells"]))
            specs[pos] = {"kind": "text"}
        elif col["kind"] == "num" and pos in in_group:
            specs[pos] = {"kind": "grp", "dec": col["dec"], "g": in_group[pos]}
        elif col["kind"] == "num":
            o = best_order(col["ints"])
            co, m = FIXED[o], rc.IntModel()
            ints = col["ints"]
            for v in ints[:o]:
                m.encode(enc, v)
            for i in range(o, len(ints)):
                p = 0
                for k, cf in enumerate(co):
                    p += cf * ints[i - 1 - k]
                m.encode(enc, ints[i] - p)
            specs[pos] = {"kind": "num", "dec": col["dec"], "order": o}

    # --- 2D groups
    for gi, g in enumerate(groups):
        M = [plan[pos]["ints"] for pos in g]
        m, w = rc.IntModel(), len(g)
        for r in range(nrows):
            for c in range(w):
                if r == 0 and c == 0:
                    p = 0
                elif r == 0:
                    p = M[c - 1][0]
                elif c == 0:
                    p = M[0][r - 1]
                else:
                    p = M[c][r - 1] + M[c - 1][r] - M[c - 1][r - 1]
                m.encode(enc, M[c][r] - p)

    meta = {"columns": table.columns, "nrows": nrows, "cols": specs,
            "groups": groups, "order": order}
    body = enc.finish()
    meta_b = lzma.compress(json.dumps(meta, separators=(",", ":")).encode(),
                           **XZ)
    text_b = lzma.compress("\x00".join(text_blobs).encode(), **XZ) \
        if text_blobs else b""
    head = b"SMRT" + len(meta_b).to_bytes(4, "big") + \
        len(text_b).to_bytes(4, "big") + len(body).to_bytes(4, "big")
    return head + meta_b + text_b + body


def decode(blob: bytes):
    assert blob[:4] == b"SMRT"
    ml = int.from_bytes(blob[4:8], "big")
    tl = int.from_bytes(blob[8:12], "big")
    o = 16
    meta = json.loads(lzma.decompress(blob[o:o + ml]))
    o += ml
    texts = lzma.decompress(blob[o:o + tl]).decode().split("\x00") if tl else []
    o += tl
    dec = rc.Decoder(blob[o:])

    nrows, specs = meta["nrows"], meta["cols"]
    cols: List[Optional[List[str]]] = [None] * len(specs)
    ids_by_pos: Dict[int, List[int]] = {}

    for pos in meta["order"]:
        sp = specs[pos]
        nb, par = sp["nb"], sp["parent"]
        model = rc.Model()
        shift = nb + 1
        out = []
        if par is None:
            if sp["prev"]:
                prev = len(sp["alpha"])
                for _ in range(nrows):
                    v = dec.tree_ctx(model, nb, prev << shift)
                    out.append(v)
                    prev = v
            else:
                for _ in range(nrows):
                    out.append(dec.tree(model, nb))
        else:
            for c in ids_by_pos[par]:
                out.append(dec.tree_ctx(model, nb, c << shift))
        ids_by_pos[pos] = out
        cols[pos] = [sp["alpha"][v] for v in out]

    ti = 0
    for pos, sp in enumerate(specs):
        if sp["kind"] == "text":
            cols[pos] = texts[ti].split("\n") if nrows else []
            ti += 1
        elif sp["kind"] == "num":
            m, co = rc.IntModel(), FIXED[sp["order"]]
            ints = [m.decode(dec) for _ in range(sp["order"])]
            for i in range(sp["order"], nrows):
                p = 0
                for k, cf in enumerate(co):
                    p += cf * ints[i - 1 - k]
                ints.append(m.decode(dec) + p)
            cols[pos] = [codec.int_to_cell(v, sp["dec"]) for v in ints]

    for gi, g in enumerate(meta["groups"]):
        m, w = rc.IntModel(), len(g)
        M = [[0] * nrows for _ in range(w)]
        for r in range(nrows):
            for c in range(w):
                if r == 0 and c == 0:
                    p = 0
                elif r == 0:
                    p = M[c - 1][0]
                elif c == 0:
                    p = M[0][r - 1]
                else:
                    p = M[c][r - 1] + M[c - 1][r] - M[c - 1][r - 1]
                M[c][r] = m.decode(dec) + p
        for c, pos in enumerate(g):
            cols[pos] = [codec.int_to_cell(v, specs[pos]["dec"])
                         for v in M[c]]

    rows = [[cols[j][i] for j in range(len(specs))] for i in range(nrows)]
    return dtz.Table(list(meta["columns"]), rows)
