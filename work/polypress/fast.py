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


# At most this fraction of a column may be exception cells. It is a screen,
# not the decision -- the decision is the whole-file guard in encode(). The
# point of the screen is to refuse the pathological shapes cheaply: a column
# of ragged-decimal floats has exceptions everywhere and is exactly the case
# that was measured 9-23% worse and reverted.
EX_MAX_FRACTION = 0.05
# The C port's POW10 table stops here, so both implementations refuse beyond it.
EX_MAX_DEC = 18

_NUM_RE = codec._NUMERIC


def _exact_int(cell: str, dec: int):
    """The cell as a scaled integer, or None if it cannot be reproduced.

    Exactly codec.as_numeric_column's per-cell test: parse, print back, and
    require the original string. That is what rejects "007", "1.50" at three
    decimals, and -- the case that started this -- "-0.0", which parses to 0
    and prints as "0.0".
    """
    if not _NUM_RE.match(cell):
        return None
    v = codec.cell_to_int(cell, dec)
    # The C parser refuses anything past the 2^62 acceptance limit while it is
    # still accumulating digits, so a value beyond it is an exception there.
    # Python's ints are arbitrary precision and would happily take it, which
    # would put the two implementations on different plans for the same file.
    if v >= INT_LIMIT or v <= -INT_LIMIT:
        return None
    return v if codec.int_to_cell(v, dec) == cell else None


def _numeric_lenient(cells):
    """A column that is numeric apart from a few cells that are not.

    The numeric test is all or nothing, and that turns out to be expensive.
    The Treasury yield curve -- the matrix table the planar predictor exists
    for -- contains four blank cells in 72,048, and those four drop all eight
    rate columns to the dictionary path. With no numeric columns there is no
    group, and the archive is 59,309 B instead of 34,856 B. Four cells, 41% of
    the file. Separately, one "-0.0" in 26,304 cells disqualifies a whole
    temperature column and splits a 21-column matrix into 18 and 3.

    The exceptions are recorded by position and stored as text, and their
    slots in the integer array are FORWARD-FILLED from the previous good
    value. Filling rather than removing is what keeps every column the same
    length, which is what keeps the planar predictor able to stack them --
    worth another 18% on the yield curve on top of the 28% for being numeric
    at all. The fill values are never seen: the decoder overwrites those
    positions with the stored strings.

    The decimal count is the most common one rather than the maximum, with
    ties going to the smaller count so the choice cannot depend on dict order.
    Anything that does not reproduce exactly at that count becomes an
    exception, which folds blanks, "-0.0", leading zeros and stray decimal
    counts into one mechanism.
    """
    n = len(cells)
    if n == 0:
        return None
    limit = int(n * EX_MAX_FRACTION)

    counts: Dict[int, int] = {}
    bad = 0
    for c in cells:
        if not _NUM_RE.match(c):
            bad += 1
            if bad > limit:
                return None
            continue
        i = c.find(".")
        d = 0 if i < 0 else len(c) - i - 1
        if d > EX_MAX_DEC:
            # counted as an exception candidate rather than as a decimal
            # count, so the histogram stays a fixed 0..18 array in the C port
            bad += 1
            if bad > limit:
                return None
            continue
        counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    dec = min(counts, key=lambda d: (-counts[d], d))

    vals: List[Optional[int]] = [None] * n
    expos: List[int] = []
    exvals: List[str] = []
    for i, c in enumerate(cells):
        v = _exact_int(c, dec)
        if v is None:
            expos.append(i)
            exvals.append(c)
            if len(expos) > limit:
                return None
        else:
            vals[i] = v
    if not expos:
        return None                      # plain _numeric already handles this

    # forward fill, then patch any leading run with the first good value
    fill = None
    first_good = None
    for i in range(n):
        if vals[i] is None:
            vals[i] = fill
        else:
            fill = vals[i]
            if first_good is None:
                first_good = vals[i]
    if first_good is None:
        return None
    for i in range(n):
        if vals[i] is None:
            vals[i] = first_good
        else:
            break

    a = np.array(vals, dtype=np.int64)
    if a.size and int(np.abs(a).max()) >= INT_LIMIT:
        return None
    return a, dec, np.array(expos, dtype=np.int64), exvals


def _lenient_promising(cells, a, expos, nrows) -> bool:
    """Could storing this column as numbers possibly beat leaving it alone?

    The never-worse guard costs a whole second encode, and profiling says that
    second encode IS the cost -- the lenient scan itself is 0.04-0.09s where
    the extra encode is 0.9-1.4s. On four large datasets the guard runs, finds
    nothing, and doubles encode time for zero bytes.

    This is a one-sided screen, and the direction matters. The alternative cost
    is deliberately OVER-estimated: it includes the dictionary's alphabet, and
    it ignores the reorder parent that would make the column cheaper still. So
    the real alternative is never more expensive than this estimate, and
    `numeric >= estimate` therefore implies `numeric >= real`. Skipping on that
    basis cannot discard a win on this column.

    It is only a nominator. Whenever any column looks promising the full
    end-to-end guard still runs and still decides.
    """
    k = diff_order(a)
    num = _probe_bytes(pack_ints(np.diff(a, n=k) if k else a))
    num += _probe_bytes(pack_ints(np.diff(expos, prepend=np.int64(0))))
    uniq = sorted(set(cells))
    if len(uniq) <= DICT_MAX and len(uniq) * 2 <= max(nrows, 2):
        idx = {v: i for i, v in enumerate(uniq)}
        ids = np.array([idx[v] for v in cells], dtype=np.int64)
        alt = _probe_bytes(ids.astype(_width(len(uniq))).tobytes())
        alt += _probe_len(uniq)
    else:
        alt = _probe_len(cells)
    return num < alt


def classify(table, lenient: bool = True) -> List[dict]:
    nrows = len(table.rows)
    plan = []
    for j in range(len(table.columns)):
        cells = table.column(j)
        num = _numeric(cells)
        if num is not None:
            plan.append({"kind": "num", "ints": num[0], "dec": num[1],
                         "j": j, "ex": None})
            continue
        lax = _numeric_lenient(cells) if lenient else None
        if lax is not None:
            plan.append({"kind": "num", "ints": lax[0], "dec": lax[1],
                         "j": j, "ex": (lax[2], lax[3]),
                         "exp": _lenient_promising(cells, lax[0], lax[2],
                                                   nrows)})
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


# Entropy scores are COMPARED as integers, never as floats.
#
# The parent search ranks columns by entropy differences, and for a long time
# both implementations tried to agree on those differences bit for bit. That is
# not achievable and the attempt was hiding a real defect. `np.sum` is pairwise
# for small arrays -- which `csrc/ppz_encode.c:pairwise_sum` reproduces exactly,
# and for a 23-bin marginal the two agree to the last bit -- but on a 13,147-bin
# joint histogram numpy takes a SIMD reduction whose grouping depends on the
# CPU's vector width. Measured on this machine: `np.sum` differs from numpy's
# own documented scalar algorithm at that size. So no portable C can match it,
# and two numpy builds on different hardware need not match each other either.
#
# The last bit was never meaningful. A gain of 0.4912703838362144 and one of
# 0.4912703838362162 say the same thing about a column, and letting the
# difference between them choose a parent -- and therefore every byte of the
# archive after it -- is the bug. Scores are quantised to a grid about 1e-6
# wide and compared as int64. Anything closer than that is a tie, and ties fall
# to the lower column index in both implementations, which is a rule they can
# actually both keep.
#
# `floor(x * SCALE + 0.5)` rather than round(): Python's round() is
# banker's rounding and C's llround() is half-away-from-zero, and every score
# here is non-negative.
_SCORE_SCALE = 1 << 20


def _score(x: float) -> int:
    return int(math.floor(x * _SCORE_SCALE + 0.5))


# The nomination floor, 0.05 bits, on the same integer grid.
_MIN_GAIN_SCORE = _score(0.05)


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
            g = _score(base[b] - _cond_entropy_corrected(
                sample[a], sample[b], sizes[b], ha, ma))
            if g > _MIN_GAIN_SCORE:
                gain[b][a] = g

    root = min(dict_pos, key=lambda p: _score(base[p]))
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
            b = min(remaining, key=lambda p: _score(base[p]))
            parent[b] = None
        else:
            b, a, _ = best
            parent[b] = a
        placed.add(b)
        order.append(b)
        remaining.remove(b)

    # Never-worse, the same guard text columns already had.
    #
    # This asymmetry was doing real damage. Text parents are chosen by
    # measurement and can decline; dictionary parents were chosen by
    # conditional entropy and taken on trust. So every experiment that moved a
    # column out of `text` -- ragged-decimal numerics, raising the dictionary
    # threshold -- traded a measured decision for an unmeasured one and lost,
    # and the loss landed on a different column than the one being changed,
    # which is why per-column probes never saw it coming.
    #
    # Entropy stays as the nominator; it is good at that and cheap. What is
    # added is the check that the nomination actually pays. A parent is kept
    # only if permuting by it beats leaving the column alone.
    for b in order:
        a = parent.get(b)
        if a is None:
            continue
        ids = plan[b]["ids"]
        perm = np.argsort(plan[a]["ids"], kind="stable")
        w = _width(len(plan[b]["alpha"]))
        if _probe_bytes(ids[perm].astype(w).tobytes()) >= \
                _probe_bytes(ids.astype(w).tobytes()):
            parent[b] = None
    return parent, order


# How many entropy-nominated parents actually get compressed and compared.
# Each candidate costs one cheap pass over the column, and the score is a good
# enough nominator that the winner is usually in the first few -- but "usually"
# was doing more work than it should. Measured end to end over 18 tables:
#
#     candidates   1        3        5       10      all
#     total     8,839,893 8,833,390 8,800,253 8,796,082 8,790,196
#     vs 3        +0.07%    0.00%   -0.38%   -0.42%   -0.49%
#     encode s      9.6     10.4     11.2     12.9     17.4
#
# 5 takes the bulk of the available gain for 8% more encode time; going to
# "all" costs 67% more time for a further 0.11%. The win is concentrated where
# it is most wanted -- chicago_permits, the worst dataset in the corpus, drops
# 3.5% at this setting.
#
# One caveat worth keeping: widening this is NOT monotonic per file. Each
# column's choice is probed alone, but every text column ends up in one shared
# blob that is compressed together, so a locally better ordering can be
# globally worse -- noaa_gsoy_ord is 6,559 B at 3 and 6,588 B at 10. That is
# the same local-versus-global trap as the 2D groups, and it is the reason
# this constant is set by measurement rather than raised to "all".
TEXT_PARENT_CANDIDATES = 5

# A fast stand-in for the real entropy stage, used only to choose between
# orderings. Preset 1 ranks the candidates the same way preset 9 does at a
# fraction of the cost, and nothing it produces is ever stored.
_PROBE = dict(format=lzma.FORMAT_RAW,
              filters=[{"id": lzma.FILTER_LZMA2, "preset": 1}])


def _probe_len(cells: List[str]) -> int:
    return len(lzma.compress("\n".join(cells).encode("utf-8"), **_PROBE))


def _probe_bytes(b: bytes) -> int:
    return len(lzma.compress(b, **_PROBE))


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
            g = _score(base_t - _cond_entropy_corrected(
                dsample[dp], ids, len(uniq), ha, ma))
            if g > _MIN_GAIN_SCORE:
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


def _emit_exceptions(col, spec, bins, sgroups) -> None:
    """Store the cells a numeric column could not represent.

    Positions are delta-coded before packing -- exceptions are sorted and
    usually sparse, so the gaps are far smaller than the indices. The strings
    join the text blob, where a run of empty cells or of "-0.0" costs
    essentially nothing.

    Emitted immediately after the column's own payload, in both the binary and
    the string stream, so the decoder recovers them at the same point in its
    own walk without needing an index.
    """
    ex = col.get("ex")
    if not ex:
        return
    expos, exvals = ex
    spec["nex"] = int(expos.size)
    bins.append(pack_ints(np.diff(expos, prepend=np.int64(0))))
    sgroups.append(exvals)


def _apply_exceptions(cells, sp, cuts, texts, bi, ti):
    """Inverse of _emit_exceptions. Returns (cells, bi, ti)."""
    nex = sp.get("nex", 0)
    if not nex:
        return cells, bi, ti
    expos = np.cumsum(unpack_ints(cuts[bi], nex))
    bi += 1
    exvals = texts[ti]
    ti += 1
    for i, p in enumerate(expos.tolist()):
        cells[p] = exvals[i]
    return cells, bi, ti


def _width(n: int) -> str:
    return "<u1" if n <= 256 else ("<u2" if n <= 65536 else "<u4")


# Strings are stored as one concatenated UTF-8 blob plus an array of byte
# lengths. Joining on a separator -- "\n", "\x00", anything -- is wrong,
# because a cell is allowed to contain that byte. This costs nothing: the
# lengths compress to almost nothing and the blob compresses exactly as well
# as the joined form did.

def _front_code(words: List[str]) -> bytes:
    """Sorted words as (shared prefix length, remainder).

    A dictionary column's alphabet is stored sorted, so neighbours share long
    prefixes -- "CHICAGO AVE", "CHICAGO BLVD". Front-coding replaces each word
    with how much it shares with the previous one plus the rest.

    Worth being sceptical of, because RLE over the same blob was tried here and
    bought +0.3%: xz already finds adjacent repeats. This is a different shape
    of redundancy, and measured end to end it is worth -1.19% overall and
    -7.39% on seattle_fire911 -- but it LOSES on four of thirteen datasets, so
    it is chosen by measurement rather than applied on principle.

    The prefix length is one byte, capped at 255. Only ever applied to groups
    with no embedded newline, so no remainder can contain one either and the
    newline join stays unambiguous.

    The shared prefix is counted in BYTES, not characters. That is not a
    detail: the C port compares bytes, and if this counted characters the two
    would produce different archives for any word with a non-ASCII prefix.
    Cutting a multi-byte character in half is harmless here because the halves
    are only ever concatenated back together before decoding.
    """
    lens = bytearray()
    rest = []
    prev = b""
    for w in words:
        b = w.encode("utf-8")
        n = 0
        m = min(len(prev), len(b), 255)
        while n < m and prev[n] == b[n]:
            n += 1
        lens.append(n)
        rest.append(b[n:])
        prev = b
    return bytes(lens) + b"\n".join(rest)


def _un_front_code(chunk: bytes, n: int) -> List[str]:
    lens = chunk[:n]
    rest = chunk[n:].split(b"\n")
    out: List[str] = []
    prev = b""
    for i in range(n):
        w = prev[:lens[i]] + rest[i]
        out.append(w.decode("utf-8"))
        prev = w
    return out


# How many groups get a real end-to-end trial. Each one costs a compression of
# the whole text pile, so this is what bounds the price -- and the price is the
# whole argument for the value being 1. Measured across 21 tables:
#
#     K   encode    total bytes   vs K=0
#     0    28.5s      9,646,428        +0     (1.00x)
#     1    32.9s      9,633,980   -12,448     (1.16x)
#     2    40.0s      9,633,419   -13,009     (1.41x)
#     3    43.1s      9,633,419   -13,009     (1.52x)
#
# One trial takes 96% of everything available for a sixth of the time penalty.
# The stake-ranked first candidate is the right one nearly every time, which is
# what a good nominator is for.
TEXT_FC_CANDIDATES = 1


# Below this estimated saving a candidate is not worth a trial, and below this
# share of its own bytes it is not worth one either. Both are floors on the
# NOMINATION only -- every survivor is still decided by measuring the whole
# pile. Their job is to stop the trials being spent on nothing, which is what
# takes encode time from 21s to 57s across the test set.
FC_MIN_BYTES = 1024
FC_MIN_NUM, FC_MIN_DEN = 1, 4        # shared/total must exceed 1/4


def _prefix_stats(words: List[str], cap: int = 4000) -> Tuple[int, int]:
    """(bytes shared with the previous word, total bytes), both integers.

    This measures the one property that makes front-coding pay -- how much
    consecutive values have in common. It is deliberately NOT a compression
    probe: a probe run on the group alone was measured +2.50% worse end to end,
    because it cannot see what happens once the group is concatenated with
    everything else.

    Returned as two integers rather than a ratio so the C port can rank
    candidates by cross-multiplying and never has to agree about a float.
    """
    n = min(len(words), cap)
    if n < 2:
        return 0, 0
    shared = 0
    prev = words[0].encode("utf-8")
    total = len(prev)
    for i in range(1, n):
        b = words[i].encode("utf-8")
        m = min(len(prev), len(b), 255)
        k = 0
        while k < m and prev[k] == b[k]:
            k += 1
        shared += k
        total += len(b)
        prev = b
    return shared, total


def _choose_front(groups: List[List[str]], ndict: int) -> frozenset:
    """Which string groups to front-code, decided on the WHOLE text pile.

    Three designs that decided per group were measured and all lost: a cheap
    per-group compression probe (+2.50%) and a prefix-ratio threshold at 0.3,
    0.5 and 0.7 (+3.29%, +2.45%, +0.57%). The reason is worth remembering --
    a long shared prefix IS a long LZ77 match, and xz already codes it
    cheaply, so front-coding can remove the very anchor xz was matching on.
    High prefix sharing is necessary and nowhere near sufficient.

    So the ratio only NOMINATES, capped at TEXT_FC_CANDIDATES, and every
    decision is a measurement of the entire pile. Measured that way: -0.39%
    overall and never worse on any table, with weather_hourly's time column
    going 9,438 -> 434 bytes and treasury_yields' date column -19.5%.
    """
    def build(front):
        packed = _pack_strings(groups, front)
        return packed, lzma.compress(packed[0], **XZ)

    best = frozenset()
    best_packed, best_zb = build(best)
    best_z = len(best_zb)

    # the alphabets as one block, which is what shipped before this
    if ndict:
        alpha = frozenset(range(ndict))
        packed, zb = build(alpha)
        if len(zb) < best_z:
            best, best_packed, best_zb, best_z = alpha, packed, zb, len(zb)

    # Then the most promising remaining groups, one at a time, each confirmed.
    #
    # Ranked by ESTIMATED BYTES AT STAKE, not by ratio. Ranking by ratio was
    # wrong in a way worth recording: a numeric column's exception group is a
    # run of identical values -- 47 copies of "-0.0" -- which shares 98% of its
    # bytes and so outranked everything, and with three trial slots those tiny
    # groups crowded out the real prize. weather_hourly's timestamp column has
    # a ratio of only 0.74 but about a megabyte at stake against that group's
    # 184 bytes, and it was being skipped entirely.
    cands = []
    for i in range(ndict, len(groups)):
        g = groups[i]
        if not g or any("\n" in s for s in g):
            continue
        sh, tot = _prefix_stats(g)
        if sh <= 0 or tot <= 0:
            continue
        if sh * FC_MIN_DEN <= tot * FC_MIN_NUM:
            continue
        sampled = min(len(g), 4000)
        stake = sh * len(g) // sampled
        if stake < FC_MIN_BYTES:
            continue
        cands.append((stake, i))
    # descending stake, ties by group index so both implementations agree
    cands.sort(key=lambda c: (-c[0], c[1]))
    for _stake, i in cands[:TEXT_FC_CANDIDATES]:
        trial = frozenset(best | {i})
        packed, zb = build(trial)
        if len(zb) < best_z:
            best, best_packed, best_zb, best_z = trial, packed, zb, len(zb)
    # the winner's pile and its compressed bytes are returned so the caller
    # never compresses the same pile a second time
    return best, best_packed, best_zb


def _pack_strings(groups: List[List[str]], front=frozenset()):
    """Concatenate string groups, paying for length prefixes only where a
    value actually contains a newline.

    Newline-joining is what xz likes -- the separator sits in the same stream
    as the content and models well. But a cell may legally contain a newline,
    which silently split one value into two. So each group records whether it
    is newline-joined; the rare group that is not carries an explicit length
    array. On real tables no group needs one, and the joined layout is kept."""
    parts, metas, length_arrays = [], [], []
    for i, g in enumerate(groups):
        if any("\n" in s for s in g):
            blobs = [s.encode("utf-8") for s in g]
            data = b"".join(blobs)
            length_arrays.append(np.fromiter((len(b) for b in blobs),
                                             dtype=np.int64, count=len(blobs)))
            metas.append({"n": len(g), "b": len(data), "nl": False})
        elif i in front:
            data = _front_code(g)
            metas.append({"n": len(g), "b": len(data), "nl": True, "fc": 1})
        else:
            data = "\n".join(g).encode("utf-8")
            metas.append({"n": len(g), "b": len(data), "nl": True})
        parts.append(data)
    return b"".join(parts), metas, length_arrays


def _unpack_strings(data: bytes, metas, length_bins,
                    legacy_nfront: int = 0) -> List[List[str]]:
    """`legacy_nfront` covers archives written with the archive-level "fc"
    flag, which meant "the first N groups are front-coded". Those are only
    hours old but they exist on disk, and reading one under the new per-group
    rule would return wrong strings rather than an error -- the worst outcome
    for a decoder. Both spellings are honoured."""
    out, at, li = [], 0, 0
    for i, m in enumerate(metas):
        chunk = data[at:at + m["b"]]
        at += m["b"]
        if m["n"] == 0:
            out.append([])
            continue
        if m["nl"]:
            if m.get("fc") or i < legacy_nfront:
                out.append(_un_front_code(chunk, m["n"]))
            else:
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


# Input block size for the capped compressors below. Only affects how often
# the output is checked against the cap, never the bytes produced: LZMA2 and
# bzip2 are streams, and feeding one in pieces gives the identical output to a
# single call. Verified for both.
_CAP_CHUNK = 1 << 20


def _compress_capped(data: bytes, cap: int, kind: str) -> Optional[bytes]:
    """`data` compressed, or None as soon as the result cannot fit in `cap`.

    The point is to make "always check the fallback" affordable. Compressed
    output only grows, so once it has passed the cap the candidate has lost and
    the rest of the input is wasted work. On a table the modelling wins
    handsomely -- which is most of them -- that abort happens early, because
    the modelled archive is a small fraction of what the plain CSV compresses
    to.

    Aborting cannot change the answer, only the time taken: the bytes returned
    when it does NOT abort are exactly `lzma.compress`/`bz2.compress` would
    have produced.
    """
    comp = (lzma.LZMACompressor(**XZ) if kind == "xz"
            else bz2.BZ2Compressor(9))
    out, total = [], 0
    for i in range(0, len(data), _CAP_CHUNK):
        piece = comp.compress(data[i:i + _CAP_CHUNK])
        total += len(piece)
        if total >= cap:
            return None
        out.append(piece)
    piece = comp.flush()
    total += len(piece)
    if total >= cap:
        return None
    out.append(piece)
    return b"".join(out)


def _raw_candidates(table, limit: int) -> Optional[bytes]:
    """Smallest standard-codec encoding of the whole table, or None.

    Returns None unless it beats `limit`, and also unless it round-trips --
    CSV quoting is not lossless for every conceivable cell, so the candidate
    is parsed back and compared before it is allowed to win. A fallback that
    corrupts data is worse than losing by 11%.

    Called on every table now, not only when no trick fired. The compressors
    are capped at `limit` so a hopeless candidate is abandoned part-way rather
    than finished and then thrown away.
    """
    canon = _canonical_bytes(table)
    try:
        # Parsed ONCE. This used to call _from_canonical twice, once for the
        # rows and once for the columns, which was affordable while the
        # fallback ran only on tables where no trick fired and is not now that
        # it runs on every table.
        back = _from_canonical(canon)
        if back.rows != table.rows or back.columns != table.columns:
            return None
    except Exception:
        return None

    best = None
    for magic, kind in ((MAGIC_RAW_XZ, "xz"), (MAGIC_RAW_BZ, "bz2")):
        # 4 bytes of magic ride in front, so the compressor's own budget is
        # that much smaller than the archive it has to beat.
        cap = (len(best) if best is not None else limit) - len(magic)
        if cap <= 0:
            continue
        blob = _compress_capped(canon, cap, kind)
        if blob is not None:
            best = magic + blob
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
    # Numeric-with-exceptions, measured. Recovering a column that is numeric
    # apart from a few cells is worth a great deal where it applies -- 41% of
    # the Treasury yield curve -- but it also moves a column out of the
    # dictionary path, and that is precisely the trade that made the reverted
    # ragged-decimal experiment 9-23% worse. So the old behaviour is encoded
    # too and kept if it is smaller: never-worse per file, not on average.
    #
    # The guard costs a whole second encode, so it is nominated first. The
    # plan is built once and reused, and the second encode only happens when
    # some column's numbers actually look cheaper than leaving it alone. That
    # screen is one-sided -- it over-estimates the alternative -- so it can
    # only decline cases that could not have won. Without it, four large
    # datasets encoded twice and kept the first result every time.
    plan = classify(table)
    any_lax = any(c.get("ex") for c in plan)
    promising = any(c.get("exp") for c in plan)

    lenient = True
    if not any_lax:
        # the two plans are identical, so there is nothing to choose between
        blob, fired, ngroups, _ = _encode_plan(table, plan=plan)
    elif not promising:
        del plan
        lenient = False
        blob, fired, ngroups, _ = _encode_plan(table, use_lenient=False)
    else:
        blob, fired, ngroups, _ = _encode_plan(table, plan=plan)
        del plan
        alt, alt_fired, alt_groups, _ = _encode_plan(table, use_lenient=False)
        if len(alt) < len(blob):
            blob, fired, ngroups, lenient = alt, alt_fired, alt_groups, False

    # The planar predictor was the last decision in this codec taken on trust,
    # and measuring it showed the trust was misplaced: of the three tables in
    # the corpus where a group forms at all, two came out LARGER for it --
    # nyc_collisions by 2,377 B and wide_random by 15,733 B -- against one real
    # win of 5.98% on random_floats. Grouped columns lose their own measured
    # differencing order and are forced into one fixed scheme, so a group
    # trades several measured decisions for a single unmeasured one, which is
    # exactly the asymmetry commit 20dd96e removed from the parent search.
    #
    # The guard has to be END TO END, not per group. Two cheaper checks were
    # tried and both give the wrong answer: raw packed length shows -0.0% on
    # nyc_collisions where the real effect is +28.1%, and a per-group probe
    # says wide_random gains 1.4% where the file actually loses 0.69% -- the
    # group's bytes are concatenated with every other payload and compressed
    # together, so nothing short of the whole container can see the result.
    #
    # Encoding twice is affordable because groups are rare: they formed on 2 of
    # 18 tables here and 3 of 24 in the full sweep. Tables without a group pay
    # nothing at all.
    if ngroups:
        alt, alt_fired, _, _ = _encode_plan(table, use_2d=False,
                                            use_lenient=lenient)
        if len(alt) < len(blob):
            blob, fired = alt, alt_fired

    # The fallbacks are ALWAYS considered. They used to be skipped whenever any
    # trick fired, on the theory that a table where one fired cannot lose to
    # plain xz. That theory was measured against 18 tables and held; measured
    # against 100 unselected ones it fails on 3, once by 28.1% -- the shipped
    # archive was 2,830,752 bytes where the xz fallback nobody ran was
    # 2,210,086. Invariant 2 says "never worse, MEASURED, not assumed", and a
    # gate that decides which encodings are even built is exactly an assumption.
    #
    # It is affordable because `_raw_candidates` stops compressing the moment
    # the candidate cannot win; see there.
    alt = _raw_candidates(table, len(blob))
    return alt if alt is not None else blob


def _encode_plan(table, use_2d: bool = True, use_lenient: bool = True,
                 plan=None) -> Tuple[bytes, int, int, int]:
    # `plan` lets the caller hand in a classification it has already paid for.
    # Nothing here writes to it, so it is safe to share between encodes that
    # agree about `use_lenient`.
    if plan is None:
        plan = classify(table, lenient=use_lenient)
    nrows = len(table.rows)
    groups = find_2d_groups(plan, nrows) if use_2d else []
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
            _emit_exceptions(col, specs[pos], bins, sgroups)
        elif col["kind"] == "num":
            a = col["ints"]
            k = diff_order(a)
            bins.append(pack_ints(np.diff(a, n=k) if k else a))
            specs[pos] = {"kind": "num", "dec": col["dec"], "k": k,
                          "warm": a[:k].tolist()}
            _emit_exceptions(col, specs[pos], bins, sgroups)

    for g in groups:
        M = np.stack([plan[pos]["ints"] for pos in g], axis=1)   # rows x cols
        D = np.diff(np.diff(M, axis=0), axis=1)
        side = np.concatenate([M[0], np.diff(M, axis=0)[:, 0]])
        bins.append(pack_ints(np.concatenate([side, D.ravel()])))

    # Front-coding the dictionary alphabets, measured. The alphabets are the
    # first len(order) string groups by construction, and they are sorted, so
    # neighbours share long prefixes. Worth -1.19% overall and -7.39% on
    # seattle_fire911 -- but it LOSES on four of thirteen datasets, so the
    # blob is built both ways and the smaller kept.
    #
    # This guard is cheap in a way the others are not: it re-compresses only
    # the text pile, not the whole container. Compare the exceptions guard,
    # which costs a second full encode.
    front, (txt_data, smeta, length_arrays), txt_b = \
        _choose_front(sgroups, len(order))
    # "front == exactly the alphabets" is the common case, and the older
    # archive-level flag spells it in 7 bytes instead of 7 per group. Same
    # information, smaller metadata, same payload; the decoder reads both.
    compact = bool(order) and front == frozenset(range(len(order)))
    if compact:
        for m in smeta:
            m.pop("fc", None)

    n_before = len(bins)
    bins.extend(pack_ints(a) for a in length_arrays)   # always last
    meta = {"columns": table.columns, "nrows": nrows, "cols": specs,
            "groups": groups, "order": order,
            "bins": [len(b) for b in bins],
            "smeta": smeta, "nlenbins": len(bins) - n_before}
    if compact:
        meta["fc"] = 1
    meta_b = lzma.compress(json.dumps(meta, separators=(",", ":")).encode(),
                           **XZ)
    bin_b = lzma.compress(b"".join(bins), **XZ)
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
    nlax = sum(1 for c in plan if c.get("ex"))
    return blob, fired, len(groups), nlax


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
    # Which groups are front-coded is recorded per group in smeta. The
    # archive-level "fc" is the older spelling, kept readable.
    legacy = len(meta["order"]) if meta.get("fc") else 0
    texts = _unpack_strings(txt_data, meta["smeta"], length_bins, legacy)

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

    # Exceptions belonging to grouped columns are read here, in the same walk
    # the encoder wrote them, but cannot be applied until the group has been
    # reconstructed further down.
    grp_ex: Dict[int, Tuple[np.ndarray, List[str]]] = {}

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
        elif sp["kind"] == "grp":
            nex = sp.get("nex", 0)
            if nex:
                grp_ex[pos] = (np.cumsum(unpack_ints(cuts[bi], nex)),
                               texts[ti])
                bi += 1
                ti += 1
        elif sp["kind"] == "num":
            k = sp["k"]
            d = unpack_ints(cuts[bi], nrows - k)
            bi += 1
            a = _undiff(d, np.array(sp["warm"], dtype=np.int64), k) if k else d
            cells = ints_to_cells(a, sp["dec"])
            cells, bi, ti = _apply_exceptions(cells, sp, cuts, texts, bi, ti)
            cols[pos] = cells

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
            cells = ints_to_cells(M[:, c], specs[pos]["dec"])
            ex = grp_ex.get(pos)
            if ex is not None:
                expos, exvals = ex
                for i, p in enumerate(expos.tolist()):
                    cells[p] = exvals[i]
            cols[pos] = cells

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
