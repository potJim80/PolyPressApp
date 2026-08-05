#!/usr/bin/env python3
"""Probe: number-encode, sort each column ON ITS OWN, regress, keep deviations.

The proposal, in the user's words:

    "firstly, number encoded everything. Every unique entry gets a number,
     except numbers themselves. then, you assign every cell a row number.
     then, you order every column separately in increasing order. then, you
     try to create some sort of regression for every column, and just store
     the deviations. no xz at the end."

So this file implements exactly that, as a real codec: every variant below is
DECODED and compared cell by cell before its size is reported, and no variant
uses xz (or any general-purpose compressor) unless its name says so.

The entropy backend is an LZMA-style adaptive binary range coder -- ours, ~60
lines, no library.  Every stream is coded with it, so all the variants share a
backend and the comparison isolates the idea rather than the finisher.

WHAT THE VARIANTS ISOLATE

  ctrl no-sort      ids straight down the column, no sort, no permutation.
                    This is the control: it is the proposal minus the sort.
  ctrl no-sort d    the same, delta-coded down the column.
  SORTREG raw       the proposal, literally.  Row numbers stored as written.
  SORTREG gap       the proposal, with the permutation coded the best way it
                    can be coded: within a run of equal values the stable sort
                    leaves the row numbers ascending, so store gaps.
  SORTREG cum       gap, plus ids assigned as cumulative counts so that the
                    sorted column is already near-linear.
  SORTREG delta     gap, with the sorted column delta-coded instead of
                    regressed -- the strongest predictor for a sorted series.

Every one carries a per-stream byte breakdown, because the interesting number
is not the total, it is which stream ate it.

  python3 benchmarks/probe_sortreg.py ../IN/corpus/noaa_gsoy_ord.csv ...

  PROBE_MAX_MB    cap the input at a row boundary (default 2)
"""
import io
import lzma
import os
import re
import subprocess
import sys
import time
from array import array
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "old"))

from polypress import dtz, fast          # noqa: E402

_XZ = dict(format=lzma.FORMAT_XZ,
           filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])


# ===================================================== the entropy backend
#
# LZMA's binary range coder.  Probabilities are 11-bit, adaptation shift 5.
# This is the whole "no xz" claim: nothing below calls a compressor.

_TOP = 1 << 24
_MASK = 0xFFFFFFFF


class RangeEncoder:
    __slots__ = ("low", "rng", "cache", "csize", "out")

    def __init__(self):
        self.low = 0
        self.rng = _MASK
        self.cache = 0
        self.csize = 1
        self.out = bytearray()

    def _shift_low(self):
        low = self.low
        if low < 0xFF000000 or low > _MASK:
            carry = low >> 32
            temp = self.cache
            out = self.out
            while True:
                out.append((temp + carry) & 0xFF)
                temp = 0xFF
                self.csize -= 1
                if self.csize == 0:
                    break
            self.cache = (low >> 24) & 0xFF
        self.csize += 1
        self.low = (low << 8) & _MASK

    def bit(self, p, i, b):
        bound = (self.rng >> 11) * p[i]
        if b:
            self.low += bound
            self.rng -= bound
            p[i] -= p[i] >> 5
        else:
            self.rng = bound
            p[i] += (2048 - p[i]) >> 5
        while self.rng < _TOP:
            self.rng = (self.rng << 8) & _MASK
            self._shift_low()

    def finish(self) -> bytes:
        for _ in range(5):
            self._shift_low()
        return bytes(self.out)


class RangeDecoder:
    __slots__ = ("data", "pos", "rng", "code")

    def __init__(self, data: bytes):
        self.data = data
        self.rng = _MASK
        code = 0
        for k in range(5):
            code = ((code << 8) | (data[k] if k < len(data) else 0)) & _MASK
        self.code = code
        self.pos = 5

    def bit(self, p, i):
        bound = (self.rng >> 11) * p[i]
        if self.code < bound:
            self.rng = bound
            p[i] += (2048 - p[i]) >> 5
            b = 0
        else:
            self.code -= bound
            self.rng -= bound
            p[i] -= p[i] >> 5
            b = 1
        while self.rng < _TOP:
            self.rng = (self.rng << 8) & _MASK
            d = self.data
            self.code = ((self.code << 8) |
                         (d[self.pos] if self.pos < len(d) else 0)) & _MASK
            self.pos += 1
        return b


def _fresh():
    return array("H", [1024]) * 256


class OutStream:
    """One named byte stream.  Bytes go through an 8-bit adaptive bittree; the
    model is selected by position within the varint, which is most of the
    difference between this and a flat order-0 coder."""

    __slots__ = ("rc", "models", "nvals", "wmodel")

    def __init__(self):
        self.rc = RangeEncoder()
        self.models = [_fresh() for _ in range(4)]
        self.wmodel = array("H", [1024]) * 128
        self.nvals = 0

    def byte(self, v, slot=0):
        p = self.models[slot]
        rc = self.rc
        ctx = 1
        for k in (7, 6, 5, 4, 3, 2, 1, 0):
            b = (v >> k) & 1
            rc.bit(p, ctx, b)
            ctx = (ctx << 1) | b

    def bitctx(self, depth, ctx, b):
        """One bitmap bit, modelled by (tree depth, the last 3 bits at this
        node).  The context is the whole point: without it a wavelet tree
        lands exactly on the enumerative floor, and clustered rows make long
        constant stretches that a context model gets almost for free."""
        self.rc.bit(self.wmodel, ((depth if depth < 15 else 15) << 3) | ctx, b)

    def uint(self, v):
        self.nvals += 1
        i = 0
        while True:
            b = v & 0x7F
            v >>= 7
            if v:
                b |= 0x80
            self.byte(b, i if i < 3 else 3)
            i += 1
            if not (b & 0x80):
                return

    def sint(self, v):
        self.uint((v << 1) if v >= 0 else ((-v << 1) - 1))

    def text(self, s):
        raw = s.encode("utf-8")
        self.uint(len(raw))
        for b in raw:
            self.byte(b, 3)

    def blob(self):
        return self.rc.finish()


class RawOutStream:
    """Same API, no range coder.  Only used for the streams that a variant
    hands to xz instead -- feeding xz the coder's output would be compressing
    already-compressed bytes, which measures nothing."""

    __slots__ = ("buf",)

    def __init__(self):
        self.buf = bytearray()

    def byte(self, v, slot=0):
        self.buf.append(v)

    def uint(self, v):
        _uvar(self.buf, v)

    def sint(self, v):
        self.uint((v << 1) if v >= 0 else ((-v << 1) - 1))

    def text(self, s):
        raw = s.encode("utf-8")
        _uvar(self.buf, len(raw))
        self.buf += raw

    def blob(self):
        return bytes(self.buf)


class RawInStream:
    __slots__ = ("data", "pos")

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def byte(self, slot=0):
        b = self.data[self.pos]
        self.pos += 1
        return b

    def uint(self):
        p = [self.pos]
        v = _uread(self.data, p)
        self.pos = p[0]
        return v

    def sint(self):
        v = self.uint()
        return (v >> 1) if not (v & 1) else -((v + 1) >> 1)

    def text(self):
        n = self.uint()
        s = self.data[self.pos:self.pos + n].decode("utf-8")
        self.pos += n
        return s


class InStream:
    __slots__ = ("rd", "models", "wmodel")

    def __init__(self, data):
        self.rd = RangeDecoder(data)
        self.models = [_fresh() for _ in range(4)]
        self.wmodel = array("H", [1024]) * 128

    def bitctx(self, depth, ctx):
        return self.rd.bit(self.wmodel,
                           ((depth if depth < 15 else 15) << 3) | ctx)

    def byte(self, slot=0):
        p = self.models[slot]
        rd = self.rd
        ctx = 1
        for _ in range(8):
            ctx = (ctx << 1) | rd.bit(p, ctx)
        return ctx & 0xFF

    def uint(self):
        v = 0
        sh = 0
        i = 0
        while True:
            b = self.byte(i if i < 3 else 3)
            v |= (b & 0x7F) << sh
            if not (b & 0x80):
                return v
            sh += 7
            i += 1

    def sint(self):
        v = self.uint()
        return (v >> 1) if not (v & 1) else -((v + 1) >> 1)

    def text(self):
        n = self.uint()
        return bytes(self.byte(3) for _ in range(n)).decode("utf-8")


# The container.  Streams are named so the size breakdown is a by-product of
# the format rather than a separate accounting pass that can drift from it.
MAGIC = b"SRG1"


def pack(streams, xz_names=()):
    parts = []
    for name, st in streams.items():
        blob = st.blob()
        if name in xz_names:
            blob = lzma.compress(blob, **_XZ)
        parts.append((name, blob))
    out = bytearray(MAGIC)
    _uvar(out, len(parts))
    for name, blob in parts:
        raw = name.encode()
        _uvar(out, len(raw))
        out += raw
        _uvar(out, len(blob))
    for _, blob in parts:
        out += blob
    return bytes(out), {n: len(b) for n, b in parts}


def unpack(data, xz_names=()):
    if data[:4] != MAGIC:
        raise ValueError("bad magic")
    pos = [4]
    n = _uread(data, pos)
    meta = []
    for _ in range(n):
        ln = _uread(data, pos)
        name = data[pos[0]:pos[0] + ln].decode()
        pos[0] += ln
        meta.append((name, _uread(data, pos)))
    out = {}
    at = pos[0]
    for name, ln in meta:
        blob = data[at:at + ln]
        at += ln
        if name in xz_names:
            out[name] = RawInStream(lzma.decompress(blob))
        else:
            out[name] = InStream(blob)
    return out


def _uvar(buf, v):
    while True:
        b = v & 0x7F
        v >>= 7
        buf.append(b | (0x80 if v else 0))
        if not v:
            return


def _uread(data, pos):
    v = 0
    sh = 0
    while True:
        b = data[pos[0]]
        pos[0] += 1
        v |= (b & 0x7F) << sh
        if not (b & 0x80):
            return v
        sh += 7


# ===================================================== "except numbers themselves"

_NUM = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_LIMIT = 1 << 62


def _render(v, dec):
    if dec == 0:
        return str(v)
    neg = v < 0
    a = -v if neg else v
    s = str(a).rjust(dec + 1, "0")
    out = s[:-dec] + "." + s[-dec:]
    return "-" + out if neg else out


def numeric_plan(cells):
    """Scaled int64s, or None.  All or nothing on purpose: this probe is about
    the sort, and a column that is 'mostly numeric' is a different experiment.
    The re-render check is what keeps it lossless -- '1.5' in a column that
    needs two decimals renders back as '1.50', so that column is refused."""
    dec = 0
    for c in cells:
        if not _NUM.match(c):
            return None
        i = c.find(".")
        if i >= 0:
            d = len(c) - i - 1
            if d > dec:
                dec = d
    vals = []
    for c in cells:
        i = c.find(".")
        if i < 0:
            v = int(c) * (10 ** dec)
        else:
            frac = c[i + 1:]
            v = int(c[:i] + frac + "0" * (dec - len(frac)))
        if not -_LIMIT < v < _LIMIT:
            return None
        if _render(v, dec) != c:
            return None
        vals.append(v)
    return vals, dec


def dict_plan(cells, mode):
    """Every unique entry gets a number.  Which number is the whole question:
    the sorted column is a step function, and how linear that step function is
    decides what the regression can do."""
    if mode == "first":
        alpha, seen = [], {}
        for c in cells:
            if c not in seen:
                seen[c] = len(alpha)
                alpha.append(c)
        return alpha, [seen[c] for c in cells], "id"
    if mode == "freq":
        cnt = Counter(cells)
        alpha = sorted(cnt, key=lambda s: (-cnt[s], s))
        idx = {s: i for i, s in enumerate(alpha)}
        return alpha, [idx[c] for c in cells], "id"
    if mode == "lex":
        alpha = sorted(set(cells))
        idx = {s: i for i, s in enumerate(alpha)}
        return alpha, [idx[c] for c in cells], "id"
    if mode == "cumfreq":
        # id(v) = how many cells hold something smaller.  The sorted column is
        # then within one run-length of the identity, which is the most linear
        # a step function can be made.
        cnt = Counter(cells)
        alpha = sorted(cnt)
        idx, run = {}, 0
        for s in alpha:
            idx[s] = run
            run += cnt[s]
        return alpha, [idx[c] for c in cells], "lex"
    raise ValueError(mode)


# ===================================================== the codec

SHIFT = 20


def _fit(sv):
    """Least squares, quantised to fixed point so the decoder reproduces the
    prediction with integer arithmetic and no float ever crosses the format."""
    n = len(sv)
    if n < 2:
        return (sv[0] << SHIFT if n else 0), 0
    sx = n * (n - 1) / 2.0
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    sy = float(sum(sv))
    sxy = float(sum(j * v for j, v in enumerate(sv)))
    den = n * sxx - sx * sx
    b = (n * sxy - sx * sy) / den if den else 0.0
    a = (sy - b * sx) / n
    return int(round(a * (1 << SHIFT))), int(round(b * (1 << SHIFT)))


def _numberise(table, ids):
    """Stage 1 for every column at once: values, and what it takes to undo."""
    plans = []
    for ci in range(len(table.columns)):
        cells = table.column(ci)
        num = numeric_plan(cells)
        if num is not None:
            vals, dec = num
            plans.append((1, dec, None, vals))
        else:
            alpha, vals, _ = dict_plan(cells, ids)
            plans.append((0, None, alpha, vals))
    return plans


def _emit_gap(pm, order, sv, n):
    """Row numbers ascend inside a run of equal values, so store gaps."""
    j = 0
    while j < n:
        k = j + 1
        while k < n and sv[k] == sv[j]:
            k += 1
        pm.uint(order[j])
        for t in range(j + 1, k):
            pm.uint(order[t] - order[t - 1] - 1)
        j = k


def _read_gap(pm, order, sv, n):
    j = 0
    while j < n:
        k = j + 1
        while k < n and sv[k] == sv[j]:
            k += 1
        order[j] = pm.uint()
        for t in range(j + 1, k):
            order[t] = order[t - 1] + 1 + pm.uint()
        j = k


def _emit_reg(pm, order, n, A, B):
    """Regress the ROW NUMBER on sorted position and keep its deviations --
    the same treatment the values get, applied to the permutation."""
    for j in range(n):
        pm.sint(order[j] - ((A + B * j) >> SHIFT))


def _read_reg(pm, order, n, A, B):
    for j in range(n):
        order[j] = pm.sint() + ((A + B * j) >> SHIFT)


def _wave_emit(pm, ranks, k):
    """A wavelet tree over the value alphabet -- the binary tree that makes
    the 0/1 codes, one bitmap per node.  At a node covering values [lo,hi) the
    bitmap says, for each row still in play, whether its value is below or
    above the midpoint; then recurse on the two halves.  n*log2(k) bits, and
    the decoder needs no counts: the number of 0s it decodes IS the size of
    the left child.

    Uncontexted this lands exactly on log2(n!/prod c!), the enumerative floor.
    The context is what takes it below: clustered rows make long constant
    stretches in every bitmap."""
    stack = [(ranks, 0, k, 0)]
    while stack:
        seq, lo, hi, depth = stack.pop()
        if hi - lo <= 1 or not seq:
            continue
        mid = (lo + hi) >> 1
        left, right = [], []
        ctx = 0
        for v in seq:
            b = 0 if v < mid else 1
            pm.bitctx(depth, ctx, b)
            ctx = ((ctx << 1) | b) & 7
            (right if b else left).append(v)
        # Push right first so left pops first: the decoder recurses left
        # before right, and the two traversals must match exactly.
        stack.append((right, mid, hi, depth + 1))
        stack.append((left, lo, mid, depth + 1))


def _wave_read(pm, n, k):
    def rec(m, lo, hi, depth):
        if hi - lo <= 1:
            return [lo] * m
        if m == 0:
            return []
        mid = (lo + hi) >> 1
        bits = []
        ctx = 0
        nl = 0
        for _ in range(m):
            b = pm.bitctx(depth, ctx)
            bits.append(b)
            ctx = ((ctx << 1) | b) & 7
            nl += b ^ 1
        L = rec(nl, lo, mid, depth + 1)
        R = rec(m - nl, mid, hi, depth + 1)
        out = []
        li = ri = 0
        for b in bits:
            if b:
                out.append(R[ri])
                ri += 1
            else:
                out.append(L[li])
                li += 1
        return out

    return rec(n, 0, k, 0)


def _emit_disp(pm, order, n):
    """Displacement: how far this cell MOVED, d[j] = order[j] - j, delta-coded.

    Note d[j]-d[j-1] == order[j]-order[j-1]-1, which is exactly the gap coder's
    number.  The difference is what happens at a run boundary: gap restarts and
    pays a full absolute row number, displacement just carries on and pays a
    small signed jump.  So this is gap coding without the resets, and it should
    win precisely where gap is weakest -- an all-distinct column, where every
    run has length 1 and gap pays an absolute number for every single cell."""
    prev = 0
    for j in range(n):
        d = order[j] - j
        pm.sint(d - prev)
        prev = d


def _read_disp(pm, order, n):
    prev = 0
    for j in range(n):
        prev += pm.sint()
        order[j] = prev + j


def _emit_anchor(pm, order, n, B):
    """Anchor cells every B positions, everything else stored as its offset
    from its anchor.  A cell that has not moved relative to its anchor costs a
    zero, which the adaptive model gives away nearly free.  Natural anchors --
    stretches that did not move -- fall out of this; the stride is the
    'artificial ones' for when there are none."""
    anchor = 0
    for j in range(n):
        d = order[j] - j
        if j % B == 0:
            anchor = d
            pm.sint(d)
        else:
            pm.sint(d - anchor)


def _read_anchor(pm, order, n, B):
    anchor = 0
    for j in range(n):
        v = pm.sint()
        if j % B == 0:
            anchor = v
            d = v
        else:
            d = anchor + v
        order[j] = d + j


ANCHOR_STRIDES = (64, 1024)


def _wave_ranks(vals):
    distinct = sorted(set(vals))
    rank = {v: i for i, v in enumerate(distinct)}
    return [rank[v] for v in vals], len(distinct)


PERM_MODES = ("gap", "reg", "wave", "disp", "anchor")


def _pick_perm(order, sv, vals, n):
    """Invariant 2: encode every candidate and compare, do not reason about
    it.  The throwaway streams start with cold models, so this understates all
    of them -- equally, which is what matters for the choice.  They are
    genuinely complementary: gap is cheap inside long runs, reg is exact when
    the file is already sorted by this column, wave sees clustering, disp is
    gap without the per-run reset, anchor is disp with a coarser reference."""
    def cost(fn):
        st = OutStream()
        fn(st)
        return len(st.blob())

    A, B = _fit(order)
    ranks, k = _wave_ranks(vals)
    costs = [cost(lambda s: _emit_gap(s, order, sv, n)),
             cost(lambda s: _emit_reg(s, order, n, A, B)) + 12,
             cost(lambda s: _wave_emit(s, ranks, k)),
             cost(lambda s: _emit_disp(s, order, n))]
    anchor = [cost(lambda s, b=b: _emit_anchor(s, order, n, b)) + 2
              for b in ANCHOR_STRIDES]
    costs.append(min(anchor))
    stride = ANCHOR_STRIDES[anchor.index(min(anchor))]
    return costs.index(min(costs)), A, B, stride


def encode(table, ids="lex", sort=True, pred="linear", perm="gap",
           xz_text=False, shared=False):
    n, m = len(table.rows), len(table.columns)
    S = {k: OutStream() for k in
         ("hdr", "name", "dict", "resid", "perm", "run")}
    if xz_text:
        S["dict"] = RawOutStream()
        S["name"] = RawOutStream()
    hdr, nm, dc, rs, pm, rn = (S["hdr"], S["name"], S["dict"], S["resid"],
                               S["perm"], S["run"])

    hdr.uint(n)
    hdr.uint(m)
    for name in table.columns:
        nm.text(name)

    if shared:
        # ONE order for the whole table, so its cost is paid once and divided
        # by m instead of paid m times.  This is the only change from the
        # per-column scheme, and it is the whole difference.
        plans = _numberise(table, ids)
        keys = list(zip(*[p[3] for p in plans])) if m else [()] * n
        order = sorted(range(n), key=keys.__getitem__)
        for i in order:
            pm.uint(i)
        for kind, dec, alpha, vals in plans:
            hdr.uint(kind)
            if kind == 1:
                hdr.uint(dec)
            else:
                hdr.uint(len(alpha))
                for s in alpha:
                    dc.text(s)
            p = 0
            for i in order:
                v = vals[i]
                rs.sint(v - p)
                p = v
        return pack(S, ("dict", "name") if xz_text else ())

    for ci in range(m):
        cells = table.column(ci)
        num = numeric_plan(cells)
        if num is not None:
            vals, dec = num
            hdr.uint(1)
            hdr.uint(dec)
        else:
            alpha, vals, _ = dict_plan(cells, ids)
            hdr.uint(0)
            hdr.uint(len(alpha))
            for s in alpha:
                dc.text(s)

        if sort:
            order = sorted(range(n), key=vals.__getitem__)   # stable
            sv = [vals[i] for i in order]
        else:
            order = None
            sv = vals

        if pred == "linear":
            A, B = _fit(sv)
            hdr.sint(A)
            hdr.sint(B)
            for j, v in enumerate(sv):
                rs.sint(v - ((A + B * j) >> SHIFT))
        elif pred == "delta":
            p = 0
            for v in sv:
                rs.sint(v - p)
                p = v
        elif pred == "rle":
            # The control that matters: exploit the runs the file ALREADY has,
            # in the original row order, with no permutation to pay for.
            j = 0
            p = 0
            while j < n:
                k = j + 1
                while k < n and sv[k] == sv[j]:
                    k += 1
                rs.sint(sv[j] - p)
                rn.uint(k - j - 1)
                p = sv[j]
                j = k
        else:
            for v in sv:
                rs.sint(v)

        if sort:
            mode, A2, B2, stride = perm, 0, 0, ANCHOR_STRIDES[0]
            if perm == "best":
                pick, A2, B2, stride = _pick_perm(order, sv, vals, n)
                hdr.uint(pick)
                mode = PERM_MODES[pick]
            if mode == "raw":
                for i in order:
                    pm.uint(i)
            elif mode == "disp":
                _emit_disp(pm, order, n)
            elif mode == "anchor":
                hdr.uint(stride)
                _emit_anchor(pm, order, n, stride)
            elif mode == "wave":
                # The tree needs dense ranks; sv is sorted, so its distinct
                # values in order ARE the rank space, and the decoder gets
                # them from the run-length stage for free.
                ranks, k = _wave_ranks(vals)
                _wave_emit(pm, ranks, k)
            elif mode == "reg":
                if perm != "best":
                    A2, B2 = _fit(order)
                hdr.sint(A2)
                hdr.sint(B2)
                _emit_reg(pm, order, n, A2, B2)
            else:
                _emit_gap(pm, order, sv, n)

    xz_names = ("dict", "name") if xz_text else ()
    return pack(S, xz_names)


def _unmap(rows, ci, n, alpha, dec, vals, ids):
    if alpha is None:
        for r in range(n):
            rows[r][ci] = _render(vals[r], dec)
    elif ids == "cumfreq":
        # ids are cumulative offsets, ascending in the same order the
        # alphabet was written, so the t-th distinct value is alpha[t].
        back = {}
        for v in sorted(set(vals)):
            back[v] = alpha[len(back)]
        for r in range(n):
            rows[r][ci] = back[vals[r]]
    else:
        for r in range(n):
            rows[r][ci] = alpha[vals[r]]


def decode(data, ids="lex", sort=True, pred="linear", perm="gap",
           xz_text=False, shared=False):
    S = unpack(data, ("dict", "name") if xz_text else ())
    hdr, nm, dc, rs, pm, rn = (S["hdr"], S["name"], S["dict"], S["resid"],
                               S["perm"], S["run"])
    n = hdr.uint()
    m = hdr.uint()
    columns = [nm.text() for _ in range(m)]
    rows = [[None] * m for _ in range(n)]

    if shared:
        order = [pm.uint() for _ in range(n)]
        for ci in range(m):
            kind = hdr.uint()
            if kind == 1:
                dec, alpha = hdr.uint(), None
            else:
                dec, alpha = None, [dc.text() for _ in range(hdr.uint())]
            vals = [0] * n
            p = 0
            for i in order:
                p += rs.sint()
                vals[i] = p
            _unmap(rows, ci, n, alpha, dec, vals, ids)
        return dtz.Table(columns, rows)

    for ci in range(m):
        kind = hdr.uint()
        if kind == 1:
            dec = hdr.uint()
            alpha = None
        else:
            k = hdr.uint()
            dec = None
            alpha = [dc.text() for _ in range(k)]

        if pred == "linear":
            A = hdr.sint()
            B = hdr.sint()
            sv = [0] * n
            for j in range(n):
                sv[j] = rs.sint() + ((A + B * j) >> SHIFT)
        elif pred == "delta":
            sv = [0] * n
            p = 0
            for j in range(n):
                p += rs.sint()
                sv[j] = p
        elif pred == "rle":
            sv = []
            p = 0
            while len(sv) < n:
                p += rs.sint()
                sv.extend([p] * (rn.uint() + 1))
        else:
            sv = [rs.sint() for _ in range(n)]

        if sort:
            mode = PERM_MODES[hdr.uint()] if perm == "best" else perm
            if mode == "wave":
                distinct = []
                for v in sv:
                    if not distinct or distinct[-1] != v:
                        distinct.append(v)
                vals = [distinct[r] for r in _wave_read(pm, n, len(distinct))]
                _unmap(rows, ci, n, alpha, dec, vals, ids)
                continue

            order = [0] * n
            if mode == "raw":
                for j in range(n):
                    order[j] = pm.uint()
            elif mode == "disp":
                _read_disp(pm, order, n)
            elif mode == "anchor":
                _read_anchor(pm, order, n, hdr.uint())
            elif mode == "reg":
                _read_reg(pm, order, n, hdr.sint(), hdr.sint())
            else:
                _read_gap(pm, order, sv, n)
            vals = [0] * n
            for j in range(n):
                vals[order[j]] = sv[j]
        else:
            vals = sv

        _unmap(rows, ci, n, alpha, dec, vals, ids)

    return dtz.Table(columns, rows)


# ===================================================== the bench

VARIANTS = [
    ("ctrl  no-sort",       dict(sort=False, pred="none",   ids="lex")),
    ("ctrl  no-sort delta", dict(sort=False, pred="delta",  ids="lex")),
    ("ctrl  no-sort RLE",   dict(sort=False, pred="rle",    ids="lex")),
    ("SORTREG raw-perm",    dict(sort=True,  pred="linear", perm="raw", ids="lex")),
    ("SORTREG gap-perm",    dict(sort=True,  pred="linear", perm="gap", ids="lex")),
    ("SORTREG gap cumfreq", dict(sort=True,  pred="linear", perm="gap", ids="cumfreq")),
    ("SORTREG gap delta",   dict(sort=True,  pred="delta",  perm="gap", ids="lex")),
    ("SORTREG gap RLE",     dict(sort=True,  pred="rle",    perm="gap", ids="lex")),
    # "what if the row number was stored in the regression as well?"
    ("SORTREG reg-perm",    dict(sort=True,  pred="rle",    perm="reg", ids="lex")),
    ("SORTREG best-perm",   dict(sort=True,  pred="rle",    perm="best", ids="lex")),
    # the wavelet tree: one bitmap per node of a binary tree over the alphabet
    ("SORTREG wavelet",     dict(sort=True,  pred="rle",    perm="wave", ids="lex")),
    ("SORTREG displacement", dict(sort=True, pred="rle",    perm="disp", ids="lex")),
    ("SORTREG anchor",      dict(sort=True,  pred="rle",    perm="anchor", ids="lex")),
    ("SORTREG gap +xz text", dict(sort=True, pred="linear", perm="gap", ids="lex",
                                 xz_text=True)),
    # ROUND FIVE: the id assignment itself. Every variant above numbers the
    # alphabet lexicographically; the proposal as actually stated numbers it by
    # ORDER OF FIRST APPEARANCE. That is not cosmetic. Sorting by a
    # first-appearance id sorts by the order the values show up in the file, so
    # a column whose equal values arrive in contiguous blocks sorts to the
    # IDENTITY permutation -- which displacement coding stores for nothing.
    # Lexicographic ids scramble that same column against file order. Since the
    # permutation is 85-95% of the archive, this aims at the expensive part.
    ("SORTREG best first",  dict(sort=True,  pred="rle",    perm="best", ids="first")),
    ("SORTREG disp first",  dict(sort=True,  pred="rle",    perm="disp", ids="first")),
    ("SORTREG wave first",  dict(sort=True,  pred="rle",    perm="wave", ids="first")),
    ("SORTREG gap first",   dict(sort=True,  pred="rle",    perm="gap",  ids="first")),
    # freq: most common value first. Between the two -- it ignores file order
    # like lex does, but makes the long runs land together at the low ids.
    ("SORTREG best freq",   dict(sort=True,  pred="rle",    perm="best", ids="freq")),
    # Same three stages, one difference: ONE order for the whole table, so the
    # permutation is paid once instead of once per column.
    ("SHARED-perm sort",    dict(shared=True, ids="lex")),
]

if os.environ.get("SORTREG_ONLY"):
    keep = os.environ["SORTREG_ONLY"].split(",")
    VARIANTS = [v for v in VARIANTS if any(k in v[0] for k in keep)]


def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def load(path, max_mb):
    cap = int(max_mb * 1024 * 1024)
    if os.path.getsize(path) <= cap:
        return dtz.read_any(path).normalise(), False
    with open(path, "rb") as fh:
        head = fh.read(cap)
    head = head[:head.rfind(b"\n") + 1]
    tmp = path + ".__sortreg_cut"
    with open(tmp, "wb") as fh:
        fh.write(head)
    try:
        return dtz.read_any(tmp).normalise(), True
    finally:
        os.unlink(tmp)


def run(path, max_mb):
    t, cut = load(path, max_mb)
    canon = dtz.canonical_csv(t)
    rows = []

    blob, s = timed(lambda: lzma.compress(canon, **_XZ))
    rows.append(("xz -9e (reference)", len(blob), s, True, None))
    blob, s = timed(lambda: fast.encode(t))
    rows.append(("POLYPRESS (reference)", len(blob), s, True, None))

    for name, opts in VARIANTS:
        try:
            (blob, parts), s = timed(lambda o=opts: encode(t, **o))
            back = decode(blob, **opts)
            ok = back.columns == t.columns and back.rows == t.rows
        except Exception as exc:                              # noqa: BLE001
            print(f"   {name:<24} ERROR {type(exc).__name__}: {exc}")
            continue
        rows.append((name, len(blob), s, ok, parts))
    return t.shape, len(canon), cut, rows


def main():
    max_mb = float(os.environ.get("PROBE_MAX_MB", "2"))
    totals = {}
    for path in sys.argv[1:]:
        shape, raw, cut, rows = run(path, max_mb)
        note = " (truncated)" if cut else ""
        print(f"\n{os.path.basename(path)}  {shape[0]:,} rows x {shape[1]} "
              f"cols, {raw:,} bytes CSV{note}")
        base = next(r[1] for r in rows if r[0].startswith("xz"))
        print(f"   {'method':<24}{'bytes':>12}{'vs CSV':>9}{'vs xz':>8}"
              f"{'secs':>7}  ok   breakdown")
        for name, size, secs, ok, parts in sorted(rows, key=lambda r: r[1]):
            bd = ""
            if parts:
                bd = "  ".join(f"{k}={v:,}" for k, v in parts.items() if v > 32)
            flag = "" if ok else "  <-- BROKEN"
            print(f"   {name:<24}{size:>12,}{raw / size:>8.2f}x"
                  f"{size / base:>7.2f}x{secs:>7.1f}  {'y' if ok else 'N'}   "
                  f"{bd}{flag}")
            if ok:
                totals.setdefault(name, 0)
                totals[name] += size
        sys.stdout.flush()

    if len(sys.argv) > 2 and totals:
        print("\n=== totals ===")
        base = totals["xz -9e (reference)"]
        for name, size in sorted(totals.items(), key=lambda kv: kv[1]):
            print(f"   {name:<24}{size:>12,}{size / base:>8.2f}x vs xz")


if __name__ == "__main__":
    main()
