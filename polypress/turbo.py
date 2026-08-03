"""Turbo: the speed fork. Same ideas as fast.py, none of the double encodes.

Master spends 83-92% of encode time compressing bytes it then throws away
(measured 2026-08-03, cProfile, 10 MB slices):

    cdc_nndss        2.38s total, 1.97s of it the plain-fallback guard (83%)
    chicago_permits 13.02s total, only ~1.1s producing bytes anyone keeps (9%)

The guards exist for one structural reason: master concatenates every column
into ONE lzma stream, so no column has a size of its own. Shrinking column A
can swell column B, so nothing can be judged locally and every decision has to
be made by encoding the whole table twice and comparing.

Measured cost of making the streams independent (2026-08-03):

    payload         cdc_nndss  chicago_permits  usgs_quakes  seattle_fire911
    binary columns      +4.2%            +5.2%        +0.2%            +0.3%
    string groups      +13.2%           +13.1%        -1.0%           -12.4%

Cheap for the binary payloads, expensive for strings on tables whose text
columns resemble each other. So turbo splits the binary payloads (and gets
parallelism and local decisions from it) and keeps the string groups in one
pile by default -- `text_mode` exposes the trade because it goes both ways.

What that buys, and it is the whole point: **a column's compressed size is now
its own**, so every decision can be measured on that column alone instead of by
re-encoding the table. No never-worse guard, no lenient/strict second pass, no
2D on/off second pass, no front-coding trial over the whole pile.

Threading is free on top: lzma and bz2 release the GIL (measured 2.53x on 4
threads for xz-9e, 3.85x for bzip2), so the per-column compressions run
concurrently with no pickling and no copied table.

NOT byte-compatible with fast.py, and deliberately so -- this is a different
container (`PPZT`). Invariant 1 binds fast.py and the C encoder to each other;
it does not bind this.
"""

from __future__ import annotations

import bz2
import json
import lzma
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import dtz
from . import fast

MAGIC = b"PPZT"

# Kept identical to fast.py so the two produce comparable payloads and any size
# difference is attributable to the container and the decisions, not the codec.
XZ = fast.XZ
PROBE = fast._PROBE

# How many rows a decision may look at. Every choice here is a comparison
# between two orderings of the same column, and a comparison does not need the
# whole column -- master probes full columns and spends 1.06s of a 13s encode
# doing it. Generous enough that a rare value still appears.
DECIDE_ROWS = 20000

# The sample-scale model check that replaces the plain fallback. Validated on
# 81 corpus100 datasets 2026-08-03: at margin 0.95 it costs 1,575 bytes of
# 30,335,521 (+0.005%) against always encoding both ways, and runs in 0.11s
# where the thing it replaces is 31x dearer.
#
# The margin is one-sided on purpose. A sample under-rates the columnar model
# in two ways that both push the same direction -- fixed costs (alphabets,
# metadata) do not shrink with the sample while the payload does, and the
# reorder win grows with row count. So the sample never over-rates the model,
# and requiring the row-wise candidate to win by a margin cannot discard a win.
SAMPLE_ROWS = 4000
SAMPLE_MARGIN_NUM, SAMPLE_MARGIN_DEN = 95, 100

_CPUS = max(1, (os.cpu_count() or 2) - 1)
# Least binary payload worth giving its own compressor. Below this the thread
# saves no wall-clock and the lost cross-column sharing is pure cost.
BIN_GROUP_BYTES = 4 << 20
# How many string groups get a whole-pile front-coding trial.
#
# Turbo's trials are cheap probes rather than master's full-strength
# compressions, so more of them LOOK affordable. Measured on the bench set,
# more of them are worse: 1 -> -10.90%, 2 -> -10.25%, 3 -> -9.81%, 8 -> -9.19%,
# monotonically. The reason is the same one that broke the row-wise probe --
# a decision taken at preset 1 does not transfer to preset 9e, so every extra
# trial is another chance to be confidently wrong. Cheap trials do not mean
# more trials.
FRONT_CANDIDATES = 1
# Each concurrent lzma encoder wants its own dictionary. Measured 2026-08-03:
# 4 threads on a 10 MB table cost +486 MB. Cap the pool by input size so a big
# table cannot walk through the 1-2 GB ceiling.
_XZ_ENCODER_MB = 130


def _pool_size(nbytes: int) -> int:
    """Threads we can afford for compression on an input of this size."""
    budget_mb = 900
    per = min(_XZ_ENCODER_MB, 40 + nbytes / 1e6)
    return max(1, min(_CPUS, int(budget_mb // max(per, 1))))


# ------------------------------------------------------------------ helpers

def _xz(b: bytes) -> bytes:
    return lzma.compress(b, **XZ)


def _unxz(b: bytes) -> bytes:
    return lzma.decompress(b, **XZ)


def _probe(b: bytes) -> int:
    return len(lzma.compress(b, **PROBE))


# The string pile is decided with a STRONGER probe than everything else, and
# it has to be. Measured on `opendata_howard_county` 2026-08-03, varying only
# the preset used for the front-coding trials:
#
#     preset 1   354,059  (+6.29% vs master)   0.66s
#     preset 4   354,059  (+6.29%)             1.05s
#     preset 6   333,124  (+0.01%)             1.37s
#     preset 9e  333,124  (+0.01%)             1.45s
#
# The entire deficit on that table was the probe being too weak to rank the
# candidates the way the real entropy stage would -- the same mismatch that
# makes the row-wise sample probe unreliable. Preset 6 is where the ranking
# converges, and it costs 0.7s where 9e costs 0.8s.
#
# It matters here and not elsewhere because this is the one decision taken over
# the whole shared pile rather than over a single independent column.
FRONT_PROBE = dict(format=lzma.FORMAT_RAW,
                   filters=[{"id": lzma.FILTER_LZMA2, "preset": 6}])


# The pile trials only have to RANK candidates, and a prefix ranks them the
# same way the whole pile does. Measured on three tables, truncating to 0.5 MB
# changed no decision at all while cutting the trial cost by a third:
#
#     howard_county   full 333,124 / 1.39s   0.5 MB 333,124 / 0.83s
#     text_heavy      full 907,989 / 3.54s   0.5 MB 907,989 / 2.19s
#     mixed_wide      full 445,069 / 1.55s   0.5 MB 445,069 / 1.01s
#
# 1 MB is used rather than 0.5 for margin -- the saving between them is small
# and a prefix that misses a whole group would not.
FRONT_PROBE_BYTES = 1 << 20


def _probe_front(b: bytes) -> int:
    return len(lzma.compress(b[:FRONT_PROBE_BYTES], **FRONT_PROBE))


def _stable_perm(ids: np.ndarray) -> np.ndarray:
    return np.argsort(ids, kind="stable")


# --------------------------------------------------------------- decisions

def _as_dict_or_text(cells, j, nrows) -> dict:
    uniq = sorted(set(cells))
    if len(uniq) <= fast.DICT_MAX and len(uniq) * 2 <= max(nrows, 2):
        idx = {v: i for i, v in enumerate(uniq)}
        return {"kind": "dict", "alpha": uniq,
                "ids": np.array([idx[v] for v in cells], dtype=np.int64),
                "j": j}
    return {"kind": "text", "cells": cells, "j": j}


# Rows sampled when searching for a numeric column's predictor. The search is
# O(numeric columns^2) array subtractions, so it runs on a sample and the
# winner is confirmed on the full column.
PRED_SAMPLE = 2500
# A predictor must beat storing the column on its own by this much before it is
# worth the metadata and the decode dependency.
PRED_MARGIN_NUM, PRED_MARGIN_DEN = 15, 16


def _packed_cost(a: np.ndarray) -> int:
    k = fast.diff_order(a)
    return fast.packed_len(np.diff(a, n=k) if k else a)


def pick_num_parents(plan, nrows) -> Dict[int, Optional[int]]:
    """Predict a numeric column from ANOTHER numeric column.

    Polypress differences down a column (time) and never across (columns), so
    a table of nested aggregates is invisible to it. `covid_19_vaccinations` is
    80 columns of the same measurement sliced by age --
    series_complete_5plus >= series_complete_12plus >= series_complete_18plus,
    each a subset count of the last -- so a neighbouring column predicts this
    one far better than its own past does. Measured 2026-08-03: 26.2% off the
    packed numeric payload, with `booster_doses` going 84,413 -> 20,126 from
    `booster_doses_5plus`.

    That table is exactly where master loses 21.9% to plain compression, and
    the reason is this: its redundancy runs ACROSS the row, and the only thing
    in the codec that could see it was the row-wise candidate.

    No compression is needed to decide. `pack_ints` already prices a residual,
    so the whole search is integer subtraction and `packed_len`.

    Cycles are impossible by construction: a column may only be predicted from
    one already placed, exactly as `pick_parents` does for dictionaries, and
    the resulting order is stored so the decoder can walk it.
    """
    nums = [i for i, c in enumerate(plan)
            if c["kind"] == "num" and c["ints"].size == nrows]
    out: Dict[int, Optional[int]] = {i: None for i in nums}
    if len(nums) < 2 or nrows < fast.MIN_ROWS_FOR_PARENTS:
        return out

    step = max(1, nrows // PRED_SAMPLE)
    samp = {i: np.ascontiguousarray(plan[i]["ints"][::step]) for i in nums}
    own = {i: _packed_cost(samp[i]) for i in nums}

    # Only commensurable columns: same decimal count, and magnitudes within a
    # factor. Subtracting a percentage from a count is arithmetically fine and
    # never smaller.
    hi = {i: (int(np.abs(samp[i]).max()) or 1) for i in nums}
    cand: Dict[int, List[Tuple[int, int]]] = {}
    for i in nums:
        row = []
        for j in nums:
            if i == j or plan[i]["dec"] != plan[j]["dec"]:
                continue
            if hi[i] > 64 * hi[j] or hi[j] > 64 * hi[i]:
                continue
            c = _packed_cost(samp[i] - samp[j])
            if c * PRED_MARGIN_DEN < own[i] * PRED_MARGIN_NUM:
                row.append((c, j))
        row.sort(key=lambda r: (r[0], r[1]))
        cand[i] = row

    # Greedy, in the same shape as pick_parents: start from the column that is
    # cheapest on its own, then repeatedly attach whichever remaining column
    # gains most from something already placed.
    placed = set()
    root = min(nums, key=lambda i: (own[i], i))
    placed.add(root)
    remaining = [i for i in nums if i != root]
    while remaining:
        best = None
        for i in remaining:
            for c, j in cand[i]:
                if j in placed:
                    gain = own[i] - c
                    if best is None or gain > best[0]:
                        best = (gain, i, j)
                    break
        if best is None:
            nxt = min(remaining, key=lambda i: (own[i], i))
            out[nxt] = None
            placed.add(nxt)
            remaining.remove(nxt)
            continue
        _g, i, j = best
        out[i] = j
        placed.add(i)
        remaining.remove(i)

    # Confirm each choice on the FULL column -- the search ran on a sample and
    # a sample of a differenced series exaggerates the residuals.
    for i in nums:
        j = out[i]
        if j is None:
            continue
        full_own = _packed_cost(plan[i]["ints"])
        full_pred = _packed_cost(plan[i]["ints"] - plan[j]["ints"])
        if full_pred * PRED_MARGIN_DEN >= full_own * PRED_MARGIN_NUM:
            out[i] = None
    return out


# --------------------------------------------------- derived string columns
#
# Real tables republish the same value in several columns at different
# precisions and formats. `traffic_incidents` stores one point three times:
#
#   longitude = "-114.09470548026574"
#   latitude  = "51.034986038356934"
#   point     = "POINT (-114.09470548026574 51.034986038356934)"
#   id        = "2026-07-31T00:38:59" + latitude + longitude
#
# No general compressor can see through that, because the copies are far apart
# in the file and formatted differently -- and a COLUMN codec cannot either,
# because it never compares two columns. It is the reason `traffic` is one of
# the tables where master falls back to compressing the raw rows.
#
# Two patterns do nearly all of the available gain, measured on the unbiased
# 100 (`results/cross-column-summary.txt`): geometry republished as text, and
# keys built by pasting other columns together. Both are the same shape -- a
# template of literals and slices of other columns -- so both are built here
# and nothing more general is attempted.
#
# The trap already paid for: a SUBSTRING detector finds almost nothing, because
# the duplication is usually at a different precision. Matching whole cell
# values and their prefixes is what makes it fire.

DERIVE_PROBE_ROWS = 200
DERIVE_MIN_PART = 5
# A SCREEN, not the decision. The decision is the measured never-worse check at
# the bottom of find_derived, which compares the template plus its exceptions
# against simply storing the column.
#
# This started at 5% and that was the all-or-nothing mistake CLAUDE.md warns
# about. `traffic`'s `id` is start_dt + latitude + longitude for 62% of its
# rows and something else for the rest, so a 5% cap threw away a formula that
# covers nearly two thirds of a column holding 50,000 distinct 55-character
# strings. A partly-right model is still worth far more than no model.
DERIVE_MAX_EX_NUM, DERIVE_MAX_EX_DEN = 1, 2      # screen out below 50%


def _build_template(target: str, srcs: List[Tuple[int, str]]):
    """Cover `target` with literals and slices of other columns, left to right.

    Greedy and longest-first. A part is either ("l", text) or
    ("c", column, start, length) meaning that column's value sliced -- the
    slice is what catches `id`, whose first field is a 19-character prefix of
    `start_dt` rather than the whole of it.
    """
    parts, i, lit = [], 0, []
    n = len(target)
    while i < n:
        best = None
        for j, v in srcs:
            if not v:
                continue
            # longest prefix of v that matches at i
            m = 0
            lim = min(len(v), n - i)
            while m < lim and v[m] == target[i + m]:
                m += 1
            if m < DERIVE_MIN_PART:
                continue
            whole = (m == len(v))
            # Prefer a whole-value match to a prefix of equal length, since it
            # survives a source whose length varies between rows.
            key = (m, 1 if whole else 0)
            if best is None or key > (best[0], 1 if best[2] else 0):
                best = (m, j, whole)
        if best is None:
            lit.append(target[i])
            i += 1
            continue
        if lit:
            parts.append(("l", "".join(lit)))
            lit = []
        m, j, whole = best
        # A match that consumed the source's ENTIRE value is recorded as "the
        # whole value" rather than "the first m characters". That distinction
        # is the difference between working and not: `traffic`'s latitude runs
        # 16 to 18 characters row to row, so a fixed-length slice built from
        # one row reproduced only 124 rows in 200, while the same template with
        # a whole-value part reproduces all of them.
        parts.append(("c", j, 0, -1 if whole else m))
        i += m
    if lit:
        parts.append(("l", "".join(lit)))
    return parts


def _apply_template(parts, rowvals: List[str]) -> str:
    out = []
    for p in parts:
        if p[0] == "l":
            out.append(p[1])
        else:
            v = rowvals[p[1]]
            out.append(v[p[2]:p[2] + p[3]] if p[3] >= 0 else v[p[2]:])
    return "".join(out)


def _render(parts, rv: Dict[int, List[str]], k: int) -> str:
    """Row k of a derived column, from only the columns it references."""
    out = []
    for p in parts:
        if p[0] == "l":
            out.append(p[1])
        else:
            v = rv[p[1]][k]
            out.append(v[p[2]:p[2] + p[3]] if p[3] >= 0 else v[p[2]:])
    return "".join(out)


def _stored_cost_bytes(col, vals: List[str]) -> bytes:
    """The bytes this column would occupy if left alone, in its real form.

    A dictionary column is ids plus a sorted alphabet, not its values joined.
    Getting this wrong is what made `find_derived` fire on densely-coded
    tables where it had nothing to win.
    """
    if col["kind"] == "dict":
        w = fast._width(len(col["alpha"]))
        return (col["ids"].astype(w).tobytes()
                + "\n".join(col["alpha"]).encode("utf-8"))
    return "\n".join(vals).encode("utf-8")


def _col_values(col) -> List[str]:
    if col["kind"] == "text":
        return col["cells"]
    if col["kind"] == "dict":
        alpha = col["alpha"]
        return [alpha[i] for i in col["ids"].tolist()]
    return None


def find_derived(plan, nrows) -> Dict[int, dict]:
    """Which string columns are a formula over other columns?

    Sources are restricted to columns that are NOT themselves derived, so the
    dependency graph is one level deep and no cycle is possible. Every
    candidate is confirmed by rebuilding the whole column and counting the
    rows it fails on; the failures are stored as exceptions, and a column with
    too many is rejected outright rather than carried.
    """
    strcols = [i for i, c in enumerate(plan) if c["kind"] in ("text", "dict")]
    if len(strcols) < 2 or nrows < 32:
        return {}
    vals = {i: _col_values(plan[i]) for i in strcols}
    # A column can only be a source if it is smaller than the target, else the
    # two would derive each other and the search would pick arbitrarily.
    width = {i: sum(len(v) for v in vals[i][:DERIVE_PROBE_ROWS])
             for i in strcols}

    out: Dict[int, dict] = {}
    for tgt in strcols:
        srcpos = [j for j in strcols if j != tgt and width[j] < width[tgt]]
        if not srcpos:
            continue
        # Build a template from a few rows and keep the one that verifies best
        best = None
        for r in range(0, min(nrows, 5)):
            srcs = [(j, vals[j][r]) for j in srcpos]
            parts = _build_template(vals[tgt][r], srcs)
            if not any(p[0] == "c" for p in parts):
                continue
            # Only the columns the template actually references are gathered.
            # Building a full row of every column here made this quadratic in
            # table width for no reason.
            refs = sorted({p[1] for p in parts if p[0] == "c"})
            rv = {j: vals[j] for j in refs}
            hits = 0
            stride = max(1, nrows // min(nrows, DERIVE_PROBE_ROWS))
            checked = 0
            tv = vals[tgt]
            for k in range(0, nrows, stride):
                checked += 1
                if _render(parts, rv, k) == tv[k]:
                    hits += 1
            if checked and (best is None or hits > best[0]):
                best = (hits, checked, parts)
        if best is None:
            continue
        hits, checked, parts = best
        if hits * DERIVE_MAX_EX_DEN < checked * (DERIVE_MAX_EX_DEN
                                                 - DERIVE_MAX_EX_NUM):
            continue
        # confirm over every row, collecting the exceptions
        refs = sorted({p[1] for p in parts if p[0] == "c"})
        rv = {j: vals[j] for j in refs}
        tv = vals[tgt]
        limit = nrows * DERIVE_MAX_EX_NUM // DERIVE_MAX_EX_DEN
        expos, exvals, bailed = [], [], False
        for k in range(nrows):
            if _render(parts, rv, k) != tv[k]:
                expos.append(k)
                exvals.append(tv[k])
                if len(expos) > limit:
                    bailed = True
                    break
        if bailed:
            continue
        # Never worse: the template plus its exceptions has to beat storing the
        # column. Measured on the column alone, which is sound here because the
        # column really is its own stream.
        #
        # The alternative must be how the column would ACTUALLY be stored, not
        # how it looks as raw text. A dictionary column costs ids plus an
        # alphabet, which is far less than its values joined -- comparing
        # against the joined text made every dictionary column look expensive
        # and fired formulas that lost badly. On
        # `covid_19_case_surveillance` that shipped 92,603 against master's
        # 79,587: the formula's exceptions tripled the text pile, +16.35%, on
        # exactly the densely-coded shape this codec is best at.
        keep = _probe(_stored_cost_bytes(plan[tgt], vals[tgt]))
        got = _probe("\n".join(exvals).encode("utf-8")) + 40 * len(parts)
        got += _probe(fast.pack_ints(
            np.diff(np.array(expos, dtype=np.int64),
                    prepend=np.int64(0)))) if expos else 0
        if got >= keep:
            continue
        out[tgt] = {"parts": parts, "expos": expos, "exvals": exvals}
    return out


def _num_order(pred: Dict[int, Optional[int]]) -> List[int]:
    """Positions in an order where every predictor precedes its dependant."""
    order, seen = [], set()

    def visit(i):
        if i in seen:
            return
        seen.add(i)
        p = pred.get(i)
        if p is not None:
            visit(p)
        order.append(i)

    for i in sorted(pred):
        visit(i)
    return order


def classify(table) -> List[dict]:
    """fast.classify, with the lenient-numeric choice made PER COLUMN.

    Recovering a column that is numeric apart from a few cells is worth a great
    deal where it applies -- 41% of the Treasury yield curve -- and a disaster
    where it does not, because it also moves the column out of the dictionary
    path. That is the trade CLAUDE.md records as making the reverted
    ragged-decimal experiment 9-23% worse.

    Master cannot decide it per column: its columns share one stream, so it
    encodes the WHOLE TABLE both ways and keeps the smaller. Measured on the
    bench set, that second encode is worth a lot -- ev_pop 420,588 lenient
    against 274,425 strict, sideways 1,385,863 against 948,662 -- which is why
    turbo v1, which always took the lenient plan, shipped 52% and 70% larger.

    `fast._lenient_promising` already computes exactly the right comparison and
    master uses it only as a nominator for the global double encode. Here it is
    the decision, which is sound for the same reason everything else in this
    module is: the column really is its own stream.
    """
    plan = fast.classify(table, lenient=True)
    nrows = len(table.rows)
    lax = [i for i, c in enumerate(plan) if c["kind"] == "num" and c.get("ex")]
    if not lax:
        return plan

    # The demotion has to be judged WITH the cross-column predictor, not
    # before it. `sideways` is the case: 43 of its 46 numeric columns are
    # lenient, each one is worse than a dictionary on its own, and the version
    # of this function that judged them alone demoted all 43 -- which deleted
    # exactly the structure the predictor exists to exploit, taking the
    # measured 26.2% saving with it. Judging a model before the model that
    # depends on it exists is the same ordering trap as the numeric gate.
    npred = pick_num_parents(plan, nrows)
    out = []
    for i, c in enumerate(plan):
        if c["kind"] == "num" and c.get("ex"):
            j = npred.get(i)
            pints = plan[j]["ints"] if j is not None else None
            if not _lenient_keeps(table.column(c["j"]), c, pints, nrows):
                out.append(_as_dict_or_text(table.column(c["j"]), c["j"],
                                            nrows))
                continue
        out.append(c)
    return out


def _lenient_keeps(cells, col, pints, nrows) -> bool:
    """Is this column cheaper as numbers-with-exceptions than as a dictionary?

    `fast._lenient_promising` asks the same question but cannot see a
    predictor, so it prices the column against its own past only.
    """
    a = col["ints"]
    resid = a if pints is None else a - pints
    k = fast.diff_order(resid)
    num = fast._probe_bytes(
        fast.pack_ints(np.diff(resid, n=k) if k else resid))
    expos = col["ex"][0]
    num += fast._probe_bytes(
        fast.pack_ints(np.diff(expos, prepend=np.int64(0))))
    uniq = sorted(set(cells))
    if len(uniq) <= fast.DICT_MAX and len(uniq) * 2 <= max(nrows, 2):
        idx = {v: i for i, v in enumerate(uniq)}
        ids = np.array([idx[v] for v in cells], dtype=np.int64)
        alt = fast._probe_bytes(
            ids.astype(fast._width(len(uniq))).tobytes()) + fast._probe_len(uniq)
    else:
        alt = fast._probe_len(cells)
    return num < alt


def pick_parents(plan, nrows) -> Tuple[Dict[int, Optional[int]], List[int]]:
    """Same nomination as fast.pick_parents; the confirmation is now local.

    Master confirms a parent with `_probe_bytes` over the whole column, which
    is correct there only because it happens to be comparing two orderings of
    the same bytes. Here the confirmation is also *sufficient*, because the
    column really is its own stream -- master's version could be right about
    the column and wrong about the file.
    """
    dict_pos = [p for p, c in enumerate(plan) if c["kind"] == "dict"]
    if nrows < fast.MIN_ROWS_FOR_PARENTS or len(dict_pos) < 2:
        return {p: None for p in dict_pos}, list(dict_pos)

    npairs = max(1, len(dict_pos) * (len(dict_pos) - 1))
    rows = min(fast.MI_SAMPLE,
               max(fast.MI_MIN_SAMPLE, fast.MI_BUDGET // npairs))
    step = max(1, nrows // rows)
    sample = {p: np.ascontiguousarray(plan[p]["ids"][::step]) for p in dict_pos}
    sizes = {p: len(plan[p]["alpha"]) for p in dict_pos}
    base, distinct = {}, {}
    for p in dict_pos:
        base[p], distinct[p] = fast._entropy_and_distinct(sample[p])

    from collections import defaultdict
    gain = defaultdict(dict)
    for a in dict_pos:
        ha, ma = base[a], distinct[a]
        for b in dict_pos:
            if a == b:
                continue
            g = fast._score(base[b] - fast._cond_entropy_corrected(
                sample[a], sample[b], sizes[b], ha, ma))
            if g > fast._MIN_GAIN_SCORE:
                gain[b][a] = g

    root = min(dict_pos, key=lambda p: fast._score(base[p]))
    placed, order = {root}, [root]
    parent: Dict[int, Optional[int]] = {root: None}
    remaining = [p for p in dict_pos if p != root]
    while remaining:
        best = None
        for b in remaining:
            for a, g in gain[b].items():
                if a in placed and (best is None or g > best[2]):
                    best = (b, a, g)
        if best is None:
            b = min(remaining, key=lambda p: fast._score(base[p]))
            parent[b] = None
        else:
            b, a, _ = best
            parent[b] = a
        placed.add(b)
        order.append(b)
        remaining.remove(b)

    # Confirm on a sample rather than the whole column -- this is a comparison
    # between two orderings, and a comparison converges long before the column
    # is exhausted.
    dstep = max(1, nrows // DECIDE_ROWS)
    for b in order:
        a = parent.get(b)
        if a is None:
            continue
        ids = plan[b]["ids"]
        perm = _stable_perm(plan[a]["ids"])
        w = fast._width(len(plan[b]["alpha"]))
        got = ids[perm][::dstep].astype(w).tobytes()
        raw = ids[::dstep].astype(w).tobytes()
        if _probe(got) >= _probe(raw):
            parent[b] = None
    return parent, order


def pick_text_parents(plan, nrows, order) -> Dict[int, Optional[int]]:
    """As fast.pick_text_parents, but every trial runs on a row sample.

    Master probes `TEXT_PARENT_CANDIDATES` full columns per text column; on
    chicago_permits that is 1.06s of a 13s encode, for a decision that is a
    comparison between orderings.
    """
    text_pos = [p for p, c in enumerate(plan) if c["kind"] == "text"]
    out: Dict[int, Optional[int]] = {p: None for p in text_pos}
    dict_pos = [p for p, c in enumerate(plan) if c["kind"] == "dict"]
    if nrows < fast.MIN_ROWS_FOR_PARENTS or not dict_pos or not text_pos:
        return out

    npairs = max(1, len(text_pos) * len(dict_pos))
    rows = min(fast.MI_SAMPLE,
               max(fast.MI_MIN_SAMPLE, fast.MI_BUDGET // npairs))
    step = max(1, nrows // rows)
    dsample = {p: np.ascontiguousarray(plan[p]["ids"][::step])
               for p in dict_pos}
    dbase = {p: fast._entropy_and_distinct(dsample[p]) for p in dict_pos}
    dstep = max(1, nrows // DECIDE_ROWS)

    def choose(tp):
        cells = plan[tp]["cells"][::step]
        uniq = sorted(set(cells))
        idx = {v: i for i, v in enumerate(uniq)}
        ids = np.array([idx[v] for v in cells], dtype=np.int64)
        base_t = fast._entropy(ids)
        ranked = []
        for dp in dict_pos:
            ha, ma = dbase[dp]
            g = fast._score(base_t - fast._cond_entropy_corrected(
                dsample[dp], ids, len(uniq), ha, ma))
            if g > fast._MIN_GAIN_SCORE:
                ranked.append((g, dp))
        if not ranked:
            return tp, None
        ranked.sort(key=lambda r: (-r[0], r[1]))
        full = plan[tp]["cells"]
        keep = _probe("\n".join(full[::dstep]).encode("utf-8"))
        best, best_cost = None, keep
        for _g, dp in ranked[:fast.TEXT_PARENT_CANDIDATES]:
            perm = _stable_perm(plan[dp]["ids"])
            trial = [full[i] for i in perm[::dstep]]
            cost = _probe("\n".join(trial).encode("utf-8"))
            if cost < best_cost:
                best, best_cost = dp, cost
        return tp, best

    with ThreadPoolExecutor(_CPUS) as ex:
        for tp, best in ex.map(choose, text_pos):
            out[tp] = best
    return out


def _choose_front(groups: List[List[str]], ndict: int) -> frozenset:
    """Which string groups to front-code, decided on the WHOLE pile.

    An earlier version of this decided per group, measuring each group against
    itself, on the reasoning that turbo's streams are independent so local is
    global. **That reasoning does not apply here.** The binary payloads are
    independent; the string groups are deliberately NOT -- they stay in one
    pile because splitting them costs up to 13.2%. So a group that is smaller
    on its own can still make the pile bigger, which is exactly the trap
    CLAUDE.md records from three separate per-group designs that all lost.

    Deciding on the whole pile costs up to three trials. Master pays for those
    at full strength; here they run at the probe preset, so the pile is
    compressed cheaply three times rather than dearly three times. The
    candidates are ranked by bytes AT STAKE, not by ratio -- a numeric
    column's exception group is a run of identical values sharing 98% of its
    bytes and would otherwise outrank everything while having nothing to win.
    """
    def pile(front):
        return fast._pack_strings(groups, front)[0]

    best = frozenset()
    best_cost = _probe_front(pile(best))

    if ndict:
        alpha = frozenset(range(ndict))
        c = _probe_front(pile(alpha))
        if c < best_cost:
            best, best_cost = alpha, c

    cands = []
    for i in range(ndict, len(groups)):
        g = groups[i]
        if not g or len(g) < 8 or any("\n" in s for s in g):
            continue
        sh, tot = fast._prefix_stats(g)
        if sh <= 0 or tot <= 0 or sh * fast.FC_MIN_DEN <= tot * fast.FC_MIN_NUM:
            continue
        stake = sh * len(g) // min(len(g), 4000)
        if stake < fast.FC_MIN_BYTES:
            continue
        cands.append((stake, i))
    # Greedy over several candidates, each confirmed on the whole pile and
    # kept only if it helps. Master stops at one because each of its trials
    # costs a full-strength compression of the pile; here a trial is a cheap
    # probe, so more of them are affordable -- and they are needed. Stopping at
    # one left datasets whose date and code columns EACH want front-coding
    # stuck at master's size when per-group had them 2.5% smaller.
    cands.sort(key=lambda c: (-c[0], c[1]))
    for _stake, i in cands[:FRONT_CANDIDATES]:
        trial = frozenset(best | {i})
        c = _probe_front(pile(trial))
        if c < best_cost:
            best, best_cost = trial, c
    return best


# ------------------------------------------------------------ the row-wise
# candidate. Not a fallback -- a member of the model family, chosen by the
# same sample the other decisions use. It is what covers tables whose
# redundancy runs across the row rather than down the column: on
# covid_19_vaccinations, splitting into columns makes the data 100.2% larger,
# and master loses 21.9% there because this candidate is only reachable
# through a guard.

def _canonical(table) -> bytes:
    return fast._canonical_bytes(table)


class _Cap:
    """A size the row-wise candidate must beat, which can TIGHTEN mid-flight.

    The two candidates race on separate cores, so when the columnar one lands
    first it can publish its size and let the row-wise one give up immediately
    instead of finishing a compression that has already lost. Without this,
    `text_dissimilar` -- nominated by the sample, beaten by a mile in reality
    -- cost 7.04 MB/s down to 2.24 MB/s for a candidate that never had a
    chance. An int assignment is atomic under the GIL, so no lock is needed.
    """

    __slots__ = ("v",)

    def __init__(self, v: int) -> None:
        self.v = v

    def tighten(self, v: int) -> None:
        if v < self.v:
            self.v = v


_CAP_CHUNK = 1 << 20


def _compress_capped(data: bytes, cap: _Cap, kind: str) -> Optional[bytes]:
    """`data` compressed, or None as soon as it cannot fit under `cap`.

    Compressed output only grows, so abandoning cannot change WHICH candidate
    wins, only the time spent losing. Feeding LZMA2 and bzip2 in chunks is
    byte-identical to a single call -- verified on master, which is what makes
    the cap safe rather than merely fast.
    """
    comp = (lzma.LZMACompressor(**XZ) if kind == "xz" else bz2.BZ2Compressor(9))
    out, total = [], 0
    for i in range(0, len(data), _CAP_CHUNK):
        piece = comp.compress(data[i:i + _CAP_CHUNK])
        total += len(piece)
        if total >= cap.v:
            return None
        out.append(piece)
    piece = comp.flush()
    total += len(piece)
    if total >= cap.v:
        return None
    out.append(piece)
    return b"".join(out)


def _rowwise(table, cap: Optional[_Cap] = None) -> Optional[bytes]:
    canon = _canonical(table)
    try:
        back = fast._from_canonical(canon)
        if back.rows != table.rows or back.columns != table.columns:
            return None
    except Exception:
        return None
    del back
    if cap is None:
        cap = _Cap(len(canon))
    # bzip2 FIRST. It is roughly 20x faster than xz at these settings, so
    # running it first buys a real cap for almost nothing, and xz then abandons
    # a hopeless candidate far sooner. Running xz first meant its cap was
    # whatever the columnar encoder had published, which on a close table is no
    # cap at all.
    best = None
    for magic, kind in ((b"B", "bz2"), (b"X", "xz")):
        blob = _compress_capped(canon, cap, kind)
        if blob is not None and (best is None or len(blob) + 1 < len(best)):
            best = magic + blob
            cap.tighten(len(blob))
    return best


def _sample_ratio(table) -> float:
    """Row-wise size over columnar size, on a sample. A NOMINATOR only.

    Measured 2026-08-03 and it is not safe as a decider: on `sideways` the
    probe says 1.099 where the truth is 0.860, and it still says 1.736 when
    given every row -- so the error is not sampling, it is the compressor. The
    two candidates respond differently to compression strength, because the
    row-major CSV holds long-range redundancy that a strong coder finds and a
    weak one does not, while the columnar payload is already de-correlated.

    Raising the probe's dictionary to 64 MB was tried on the hypothesis that
    reach was the problem: 1.088 against 1.099, i.e. no effect. Recorded so it
    is not tried again.

    So this only nominates, and the real capped candidate decides. Sweeping the
    threshold over 81 corpus100 datasets: at 1.05 all four genuine row-wise
    wins are nominated and 14 of 81 pay; 1.15 keeps the margin that `sideways`
    needs at 27 of 81.
    """
    n = len(table.rows)
    if n < 200:
        return 1e9
    step = max(1, n // SAMPLE_ROWS)
    sub = dtz.Table(list(table.columns), table.rows[::step])
    # BOTH sides must use the SAME compressor or the comparison is meaningless.
    # Patching `fast.XZ` does not do it: turbo's `_xz` reads turbo's own module
    # global, bound to the same dict at import. Getting that wrong measured the
    # columnar candidate at 9e and the row-wise one at preset 1, so row-wise
    # could never win and `sideways` shipped 70.6% larger than master.
    global XZ
    real = XZ
    try:
        XZ = PROBE
        col = len(_encode_columnar(sub, threads=1, probe=True))
        canon = fast._canonical_bytes(sub)
        row = min(len(lzma.compress(canon, **PROBE)),
                  len(bz2.compress(canon, 1))) + 1
    except Exception:
        return 1e9
    finally:
        XZ = real
    return row / float(col) if col else 1e9


# Nominate the row-wise candidate below this ratio. Generous on purpose: a
# nomination costs TIME (the real candidate is built) and never bytes, because
# the real candidate has to actually be smaller to win.
# 0 disables the row-wise candidate entirely. That is the DEFAULT, and it is
# the user's call, made after seeing the trade measured.
#
# Measured on 63 unselected corpus100 datasets, 255.2 MB, all round-trip exact
# either way:
#
#     row-wise candidate on   13,638,428 B   50.2s    5.08 MB/s
#     row-wise candidate off  13,640,906 B   24.1s   10.58 MB/s
#
# **2.08x the speed for 2,478 bytes -- +0.018%.** The guarantee given up is
# "never larger than plain xz/bzip2 of the same table". It is given up
# knowingly: a codec that needs a second opinion about its own model on 4 of
# 63 tables should fix the model, not carry the second opinion.
#
# Set to ~1.10 to restore it as a nominator; the real candidate then races the
# columnar one under a shared cap and decides by measurement.
NOMINATE_ROWWISE = 0.0


# ------------------------------------------------------------------- codec

def encode(table, text_mode: str = "pile") -> bytes:
    """Columnar, with the row-wise candidate raced against it when nominated.

    The two candidates are independent, so when the sample nominates the
    row-wise one they are built concurrently and the smaller wins. That makes
    the guarantee a measurement rather than a prediction, while the tables the
    sample confidently rejects -- 67 of 81 in the corpus sweep -- never build
    the second candidate at all.
    """
    # NOMINATE_ROWWISE <= 0 disables the row-wise candidate entirely, and skips
    # the nominator with it. That is the "no fallbacks at all" position: the
    # codec must be good enough not to need a second opinion about its own
    # model. It is the largest single speed lever left -- on a 40 MB
    # text-heavy table the nominator says 1.117, just under the 1.15 threshold,
    # and the candidate it then builds LOSES (3,961,432 against 3,527,412)
    # after costing 5.78s of a 14.95s encode.
    if NOMINATE_ROWWISE <= 0:
        return _encode_columnar(table, text_mode=text_mode)
    if _sample_ratio(table) >= NOMINATE_ROWWISE:
        return _encode_columnar(table, text_mode=text_mode)

    cap = _Cap(1 << 62)

    def columnar():
        blob = _encode_columnar(table, text_mode, max(1, _CPUS - 2))
        cap.tighten(len(blob) - 5)      # the row-wise container costs 5 bytes
        return blob

    with ThreadPoolExecutor(2) as ex:
        f_col = ex.submit(columnar)
        f_row = ex.submit(_rowwise, table, cap)
        col = f_col.result()
        try:
            row = f_row.result()
        except Exception:
            row = None
    if row is not None and len(row) + 5 < len(col):
        return MAGIC + b"R" + row
    return col


def _encode_columnar(table, text_mode: str = "pile", threads: int = 0,
                     probe: bool = False) -> bytes:
    plan = classify(table)
    nrows = len(table.rows)
    # Derived columns are resolved BEFORE the parent searches so those never
    # spend effort on a column that is about to become a formula.
    #
    # This runs in probe mode TOO, and it has to. The probe exists to decide
    # whether the row-wise candidate is worth building, so it must model the
    # columnar candidate as it will actually be built. Skipping the formulas
    # here made the probe badly under-rate exactly the tables the formulas
    # rescue: `traffic` ends up 32.7% SMALLER than master once its `point` and
    # `id` columns become formulas, and it was still paying 2.5s to build a
    # row-wise candidate that had already lost.
    for pos, d in find_derived(plan, nrows).items():
        plan[pos] = {"kind": "drv", "j": plan[pos]["j"],
                     "parts": d["parts"], "expos": d["expos"],
                     "exvals": d["exvals"]}
    # In probe mode skip BOTH parent searches. They are the dearest part of the
    # analysis -- on text_heavy the probe was 31% of total encode time, almost
    # all of it the O(columns^2) entropy search re-run on the sample -- and
    # omitting them only makes the columnar candidate look WORSE, which lowers
    # the ratio and nominates the row-wise candidate more readily. Nominations
    # cost time and never bytes, so the error is on the safe side by
    # construction, and a false nomination is now cheap because the row-wise
    # candidate races under a cap that the columnar one tightens.
    if probe:
        dict_pos = [p for p, c in enumerate(plan) if c["kind"] == "dict"]
        parent = {p: None for p in dict_pos}
        order = list(dict_pos)
        tparent = {p: None for p, c in enumerate(plan) if c["kind"] == "text"}
    else:
        parent, order = pick_parents(plan, nrows)
        tparent = pick_text_parents(plan, nrows, order)
    npred = ({i: None for i, c in enumerate(plan) if c["kind"] == "num"}
             if probe else pick_num_parents(plan, nrows))

    bins: List[bytes] = []
    sgroups: List[List[str]] = []
    specs: List[Optional[dict]] = [None] * len(plan)

    for pos in order:
        col = plan[pos]
        ids = col["ids"]
        par = parent.get(pos)
        if par is not None:
            ids = ids[_stable_perm(plan[par]["ids"])]
        w = fast._width(len(col["alpha"]))
        bins.append(ids.astype(w).tobytes())
        sgroups.append(col["alpha"])
        specs[pos] = {"kind": "dict", "n": len(col["alpha"]), "w": w,
                      "parent": par}

    for pos, col in enumerate(plan):
        if col["kind"] == "text":
            tp = tparent.get(pos)
            cells = col["cells"]
            if tp is not None:
                cells = [cells[i] for i in _stable_perm(plan[tp]["ids"])]
            sgroups.append(cells)
            specs[pos] = {"kind": "text"} if tp is None else \
                         {"kind": "text", "parent": tp}
        elif col["kind"] == "drv":
            specs[pos] = {"kind": "drv", "parts": col["parts"],
                          "nex": len(col["expos"])}
            if col["expos"]:
                bins.append(fast.pack_ints(
                    np.diff(np.array(col["expos"], dtype=np.int64),
                            prepend=np.int64(0))))
                sgroups.append(col["exvals"])
        elif col["kind"] == "num":
            a = col["ints"]
            j = npred.get(pos)
            resid = a if j is None else a - plan[j]["ints"]
            k = fast.diff_order(resid)
            bins.append(fast.pack_ints(np.diff(resid, n=k) if k else resid))
            specs[pos] = {"kind": "num", "dec": col["dec"], "k": k,
                          "warm": resid[:k].tolist(), "pred": j}
            fast._emit_exceptions(col, specs[pos], bins, sgroups)

    front = _choose_front(sgroups, len(order))
    txt_data, smeta, length_arrays = fast._pack_strings(sgroups, front)
    # "front is exactly the dictionary alphabets" is the common case, and one
    # archive-level flag spells it in 7 bytes rather than 7 per group. On a
    # small archive that is real money -- metadata was 2.7% of an 18 KB file.
    compact = bool(order) and front == frozenset(range(len(order)))
    if compact:
        for m in smeta:
            m.pop("fc", None)
    n_before = len(bins)
    bins.extend(fast.pack_ints(a) for a in length_arrays)

    # --- the parallel part.
    #
    # One stream per column is what makes every decision above local, but it is
    # not free: measured 2026-08-03, independence costs +0.2% to +5.2% on the
    # binary payloads because neighbouring columns do help compress each other.
    # Paying that in full is unnecessary -- the parallelism only needs as many
    # independent pieces as there are threads, not as many as there are
    # columns. So consecutive payloads are glued into a few groups, which keeps
    # most of the sharing and still saturates the pool. Consecutive rather than
    # scattered because adjacent columns are the ones that resemble each other.
    # The string pile is ONE stream for size, which makes it one thread and,
    # on a wide text-heavy table, the entire critical path -- measured on a
    # 40 MB, 116-column table the pile was 15.3 MB taking 3.73s while the 6.0
    # MB of binary payload took 0.96s beside it.
    #
    # Cutting it into contiguous chunks costs far less than splitting it by
    # column, because a chunk keeps its neighbours: per-column splitting was
    # measured at +13.1% on this table, contiguous chunking at +2.0% for four
    # pieces. Same 4 MB rule as the binary side, so a small pile stays whole
    # and pays nothing.
    #
    #     chunks   1: 3,510,950 B  3.73s
    #              2: 3,558,501 B (+1.35%)  1.90s
    #              4: 3,581,213 B (+2.00%)  1.08s   <- 3.47x
    #              8: 3,717,415 B (+5.88%)  0.76s
    strparts = (_split_pile(txt_data, smeta) if text_mode == "split"
                else _chunk(txt_data, BIN_GROUP_BYTES, _CPUS))
    total = sum(len(b) for b in bins) + len(txt_data)
    nthreads = threads or _pool_size(total)
    # Group by BYTES, not by column count. Splitting a 1.4 MB payload sixteen
    # ways buys no wall-clock -- it compresses in 0.16s either way -- and pays
    # the full independence penalty, which on coded_admin is 4.2% of the binary
    # payload and most of that table's whole deficit against master.
    binbytes = sum(len(b) for b in bins)
    ngroups = (max(1, min(len(bins), max(1, nthreads),
                          binbytes // BIN_GROUP_BYTES)) if bins else 0)
    chunk = max(1, -(-len(bins) // ngroups)) if ngroups else 1
    bingroups = [b"".join(bins[i:i + chunk])
                 for i in range(0, len(bins), chunk)]
    parts = bingroups + strparts
    if nthreads > 1 and len(parts) > 1:
        with ThreadPoolExecutor(nthreads) as ex:
            blobs = list(ex.map(_xz, parts))
    else:
        blobs = [_xz(p) for p in parts]

    meta = {"columns": table.columns, "nrows": nrows, "cols": specs,
            "order": order, "smeta": smeta, "nlenbins": len(bins) - n_before,
            "ngroups": len(bingroups), "nstr": len(strparts),
            "binlens": [len(b) for b in bins],
            "numorder": _num_order(npred),
            "sizes": [len(b) for b in blobs]}
    if compact:
        meta["fc"] = 1
    meta_b = _xz(json.dumps(meta, separators=(",", ":")).encode())
    return (MAGIC + b"C" + len(meta_b).to_bytes(4, "big") + meta_b
            + b"".join(blobs))


def _chunk(data: bytes, target: int, cap: int) -> List[bytes]:
    """`data` in contiguous pieces of about `target` bytes, at most `cap`.

    Contiguous on purpose: a chunk keeps its neighbours, so the compressor
    still sees the local redundancy. Splitting the same pile by column instead
    was measured at +13.1% against +2.0% for four contiguous chunks.
    """
    if not data:
        return [data]
    k = max(1, min(cap, len(data) // max(target, 1)))
    if k <= 1:
        return [data]
    n = -(-len(data) // k)
    return [data[i:i + n] for i in range(0, len(data), n)]


def _split_pile(txt_data: bytes, smeta) -> List[bytes]:
    out, at = [], 0
    for m in smeta:
        out.append(txt_data[at:at + m["b"]])
        at += m["b"]
    return out


def decode(blob: bytes):
    if blob[:4] != MAGIC:
        raise ValueError("not a turbo archive")
    mode = blob[4:5]
    if mode == b"R":
        body = blob[5:]
        if body[:1] == b"X":
            return fast._from_canonical(_unxz(body[1:]))
        if body[:1] == b"B":
            return fast._from_canonical(bz2.decompress(body[1:]))
        raise ValueError("bad row-wise marker")
    if mode != b"C":
        raise ValueError("bad turbo mode")

    ml = int.from_bytes(blob[5:9], "big")
    meta = json.loads(_unxz(blob[9:9 + ml]))
    at = 9 + ml
    parts = []
    for size in meta["sizes"]:
        parts.append(_unxz(blob[at:at + size]))
        at += size

    ng = meta["ngroups"]
    binblob = b"".join(parts[:ng])
    cuts, at = [], 0
    for L in meta["binlens"]:
        cuts.append(binblob[at:at + L])
        at += L
    txt_data = b"".join(parts[ng:])

    nrows, specs = meta["nrows"], meta["cols"]
    nlen = meta["nlenbins"]
    length_bins = cuts[len(cuts) - nlen:] if nlen else []
    legacy = len(meta["order"]) if meta.get("fc") else 0
    texts = fast._unpack_strings(txt_data, meta["smeta"], length_bins, legacy)

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
            perm = _stable_perm(ids_by_pos[par])
            out = np.empty_like(ids)
            out[perm] = ids
            ids = out
        ids_by_pos[pos] = ids
        cols[pos] = (np.array(alpha, dtype=object)[ids].tolist()
                     if alpha else [])

    # A numeric column may be stored as its difference from another numeric
    # column, and the predictor is not necessarily earlier in the file. So the
    # payloads are unpacked in file order -- which needs no predictor -- and
    # the additions are resolved afterwards in dependency order.
    resid_by_pos: Dict[int, np.ndarray] = {}
    ex_by_pos: Dict[int, Tuple[np.ndarray, List[str]]] = {}
    drv: List[int] = []
    drv_ex: Dict[int, Tuple[np.ndarray, List[str]]] = {}

    for pos, sp in enumerate(specs):
        if sp["kind"] == "drv":
            drv.append(pos)
            nex = sp.get("nex", 0)
            if nex:
                drv_ex[pos] = (
                    np.cumsum(fast.unpack_ints(cuts[bi], nex)), texts[ti])
                bi += 1
                ti += 1
        elif sp["kind"] == "text":
            cells = list(texts[ti])
            ti += 1
            tp = sp.get("parent")
            if tp is not None:
                perm = _stable_perm(ids_by_pos[tp])
                restored = [None] * len(cells)
                for k, src in enumerate(perm):
                    restored[src] = cells[k]
                cells = restored
            cols[pos] = cells
        elif sp["kind"] == "num":
            k = sp["k"]
            d = fast.unpack_ints(cuts[bi], nrows - k)
            bi += 1
            resid_by_pos[pos] = (
                fast._undiff(d, np.array(sp["warm"], dtype=np.int64), k)
                if k else d)
            nex = sp.get("nex", 0)
            if nex:
                ex_by_pos[pos] = (
                    np.cumsum(fast.unpack_ints(cuts[bi], nex)), texts[ti])
                bi += 1
                ti += 1

    ints_by_pos: Dict[int, np.ndarray] = {}
    for pos in meta.get("numorder", sorted(resid_by_pos)):
        if pos not in resid_by_pos:
            continue
        j = specs[pos].get("pred")
        ints_by_pos[pos] = (resid_by_pos[pos] if j is None
                            else resid_by_pos[pos] + ints_by_pos[j])

    for pos, a in ints_by_pos.items():
        cells = fast.ints_to_cells(a, specs[pos]["dec"])
        ex = ex_by_pos.get(pos)
        if ex is not None:
            expos, exvals = ex
            for i, p in enumerate(expos.tolist()):
                cells[p] = exvals[i]
        cols[pos] = cells

    # Derived columns last -- their sources are never themselves derived, so a
    # single pass suffices and no ordering metadata is needed.
    for pos in drv:
        parts = [tuple(p) for p in specs[pos]["parts"]]
        rv = {p[1]: cols[p[1]] for p in parts if p[0] == "c"}
        cells = [_render(parts, rv, k) for k in range(nrows)]
        ex = drv_ex.get(pos)
        if ex is not None:
            expos, exvals = ex
            for i, p in enumerate(expos.tolist()):
                cells[p] = exvals[i]
        cols[pos] = cells

    rows = [list(r) for r in zip(*cols)]
    return dtz.Table(list(meta["columns"]), rows)
