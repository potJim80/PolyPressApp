#!/usr/bin/env python3
"""The ANS family, one module: uABS, rABS, rANS, tANS, and interleaved rANS.

ANS is not one algorithm. Duda's construction has a binary branch and a
multi-symbol branch, and each has a "range" form and a "table" form:

  uABS   uniform binary ANS -- the original formulation, symbols spread
         uniformly over the naturals. Binary alphabet only.
  rABS   range binary ANS -- rANS restricted to two symbols.
  rANS   range ANS -- multi-symbol, one division and one modulo per symbol.
  tANS   table ANS -- the state machine is precomputed, so coding is a table
         lookup and a shift. This is FSE, the coder inside zstd.

Interleaving is NOT a fifth type. It is rANS with several states in flight so
a SIMD unit has something to do; it changes speed, and changes size only by
the extra flush words. It is measured here to show exactly that.

Two axes get confused with the variant and are held separate on purpose:

  the ALPHABET  -- multi-symbol coders here work on bytes; binary coders work
                   on an 8-deep bit tree, the LZMA/CABAC decomposition
  the MODEL     -- order 0 or order 1, static (table transmitted, charged in
                   the output) or adaptive (nothing transmitted, both sides
                   learn as they go)

Every coder here is verified by decoding, in test_ansfam.py and again on
every column measured.
"""
from __future__ import annotations

from array import array
from typing import Dict, List, Tuple

import rans
from rans import _varint, _read_varint, normalise, cumulative, SCALE_BITS, SCALE

# --------------------------------------------------------------------------
# bit stream: pushed forwards, read backwards
#
# Every ANS coder encodes symbols in reverse and decodes them forwards, so
# the bits the decoder wants first are the ones the encoder wrote last.
# --------------------------------------------------------------------------


class BitW:
    __slots__ = ("acc", "n", "buf", "total")

    def __init__(self):
        self.acc = 0
        self.n = 0
        self.buf = bytearray()
        self.total = 0

    def push(self, v: int, nb: int) -> None:
        if not nb:
            return
        self.acc |= (v & ((1 << nb) - 1)) << self.n
        self.n += nb
        self.total += nb
        while self.n >= 8:
            self.buf.append(self.acc & 0xFF)
            self.acc >>= 8
            self.n -= 8

    def done(self) -> Tuple[bytes, int]:
        if self.n:
            self.buf.append(self.acc & 0xFF)
        return bytes(self.buf), self.total


class BitR:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, total: int):
        self.buf = buf
        self.pos = total

    def pop(self, nb: int) -> int:
        if not nb:
            return 0
        self.pos -= nb
        p = self.pos
        b0 = p >> 3
        nbytes = ((p & 7) + nb + 7) >> 3
        chunk = int.from_bytes(self.buf[b0:b0 + nbytes], "little")
        return (chunk >> (p & 7)) & ((1 << nb) - 1)


# ==========================================================================
# tANS / FSE -- the table-driven multi-symbol coder
# ==========================================================================

TLOG = SCALE_BITS          # 12; table has 1<<12 states
TSIZE = 1 << TLOG


def _spread(freq: List[int]) -> array:
    """Collet's spread: step around the table so equal symbols land apart.

    Any step coprime with the table size visits every slot exactly once;
    this one is the FSE constant and keeps symbols well distributed.
    """
    step = (TSIZE >> 1) + (TSIZE >> 3) + 3
    tbl = array("H", bytes(2 * TSIZE))
    pos = 0
    for s in range(256):
        for _ in range(freq[s]):
            tbl[pos] = s
            pos = (pos + step) & (TSIZE - 1)
    return tbl


def _tans_tables(freq: List[int]):
    """(encode side, decode side) for one normalised frequency table."""
    sp = _spread(freq)
    cum, _ = cumulative(freq)

    # decode: state index -> symbol, bits to read, base of the next state
    d_sym = array("H", bytes(2 * TSIZE))
    d_nb = array("B", bytes(TSIZE))
    d_new = array("H", bytes(2 * TSIZE))
    counter = [freq[s] for s in range(256)]
    for t in range(TSIZE):
        s = sp[t]
        x = counter[s]
        counter[s] = x + 1
        nb = TLOG - (x.bit_length() - 1)
        d_sym[t] = s
        d_nb[t] = nb
        d_new[t] = (x << nb) - TSIZE

    # encode: position -> state, plus the per-symbol bit-count rule
    st = array("i", bytes(4 * TSIZE))
    tmp = cum[:]
    for t in range(TSIZE):
        s = sp[t]
        st[tmp[s]] = TSIZE + t
        tmp[s] += 1

    dnb = [0] * 256
    dfind = [0] * 256
    for s in range(256):
        f = freq[s]
        if not f:
            continue
        maxb = TLOG if f == 1 else TLOG - ((f - 1).bit_length() - 1)
        dnb[s] = (maxb << 16) - (f << maxb)
        dfind[s] = cum[s] - f
    return (st, dnb, dfind), (d_sym, d_nb, d_new)


def tans_encode(data: bytes, tables: Dict[int, list], ctx_of) -> Tuple[bytes, int, int]:
    """Encode backwards. `tables[ctx]` is an encode side; ctx_of(i) gives the
    context of position i, which the decoder can also compute."""
    w = BitW()
    state = TSIZE
    for i in range(len(data) - 1, -1, -1):
        st, dnb, dfind = tables[ctx_of(i)]
        s = data[i]
        nb = (state + dnb[s]) >> 16
        w.push(state, nb)
        state = st[(state >> nb) + dfind[s]]
    buf, total = w.done()
    return buf, total, state - TSIZE


def tans_decode(buf: bytes, total: int, start: int, n: int,
                tables: Dict[int, list], order: int) -> bytes:
    """`order` 0 keeps one table; order 1 switches on the previous byte. The
    context must be advanced only when there IS one -- an earlier version
    always set ctx = s and so looked up a table that order 0 never built."""
    r = BitR(buf, total)
    t = start
    out = bytearray()
    ap = out.append
    ctx = 0
    for _ in range(n):
        d_sym, d_nb, d_new = tables[ctx]
        s = d_sym[t]
        ap(s)
        t = d_new[t] + r.pop(d_nb[t])
        if order:
            ctx = s
    return bytes(out)


def _hdr(*vals) -> bytes:
    h = bytearray()
    for v in vals:
        _varint(v, h)
    return bytes(h)


def tans0(data: bytes) -> bytes:
    if not data:
        return b"\x00"
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    freq = normalise(counts)
    enc, dec = _tans_tables(freq)
    tbl = rans.pack_table(freq)
    buf, total, start = tans_encode(data, {0: enc}, lambda i: 0)
    return b"\x01" + _hdr(len(data), total, start, len(tbl)) + tbl + buf


def untans0(blob: bytes) -> bytes:
    if blob[:1] == b"\x00":
        return b""
    at = 1
    n, at = _read_varint(blob, at)
    total, at = _read_varint(blob, at)
    start, at = _read_varint(blob, at)
    tlen, at = _read_varint(blob, at)
    freq = rans.unpack_table(blob[at:at + tlen])
    _, dec = _tans_tables(freq)
    return tans_decode(blob[at + tlen:], total, start, n, {0: dec}, 0)


def tans1(data: bytes) -> bytes:
    """Order 1. One table per context that occurs; a context is a byte."""
    if not data:
        return b"\x00"
    counts: Dict[int, List[int]] = {}
    prev = 0
    for b in data:
        c = counts.get(prev)
        if c is None:
            c = counts[prev] = [0] * 256
        c[b] += 1
        prev = b
    freqs = {ctx: normalise(c) for ctx, c in counts.items()}
    tbl = rans._pack_o1(freqs)
    enc = {ctx: _tans_tables(f)[0] for ctx, f in freqs.items()}
    buf, total, start = tans_encode(
        data, enc, lambda i: data[i - 1] if i else 0)
    return b"\x01" + _hdr(len(data), total, start, len(tbl)) + tbl + buf


def untans1(blob: bytes) -> bytes:
    if blob[:1] == b"\x00":
        return b""
    at = 1
    n, at = _read_varint(blob, at)
    total, at = _read_varint(blob, at)
    start, at = _read_varint(blob, at)
    tlen, at = _read_varint(blob, at)
    freqs = rans._unpack_o1(blob[at:at + tlen])
    dec = {ctx: _tans_tables(f)[1] for ctx, f in freqs.items()}
    return tans_decode(blob[at + tlen:], total, start, n, dec, 1)


# ==========================================================================
# interleaved rANS -- four states in flight, the shape a SIMD unit wants
# ==========================================================================

NLANE = 4
L = rans.L
MASK = rans.MASK


def rans0x4(data: bytes) -> bytes:
    if not data:
        return b"\x00"
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    freq = normalise(counts)
    cum, _ = cumulative(freq)
    tbl = rans.pack_table(freq)

    n = len(data)
    x = [L] * NLANE
    out = bytearray()
    ap = out.append
    for i in range(n - 1, -1, -1):
        lane = i & (NLANE - 1)
        s = data[i]
        f = freq[s]
        xi = x[lane]
        x_max = ((L >> SCALE_BITS) << 8) * f
        while xi >= x_max:
            ap(xi & 0xFF)
            xi >>= 8
        x[lane] = ((xi // f) << SCALE_BITS) + (xi % f) + cum[s]
    for lane in range(NLANE - 1, -1, -1):
        xi = x[lane]
        for _ in range(4):
            ap(xi & 0xFF)
            xi >>= 8
    out.reverse()
    return b"\x01" + _hdr(len(data), len(tbl)) + tbl + bytes(out)


def unrans0x4(blob: bytes) -> bytes:
    if blob[:1] == b"\x00":
        return b""
    at = 1
    n, at = _read_varint(blob, at)
    tlen, at = _read_varint(blob, at)
    freq = rans.unpack_table(blob[at:at + tlen])
    cum, look = cumulative(freq)
    body = blob[at + tlen:]

    x = []
    for lane in range(NLANE):
        x.append(int.from_bytes(body[4 * lane:4 * lane + 4], "big"))
    pos = 4 * NLANE
    out = bytearray()
    ap = out.append
    for i in range(n):
        lane = i & (NLANE - 1)
        xi = x[lane]
        slot = xi & MASK
        s = look[slot]
        ap(s)
        xi = freq[s] * (xi >> SCALE_BITS) + slot - cum[s]
        while xi < L:
            xi = (xi << 8) | body[pos]
            pos += 1
        x[lane] = xi
    return bytes(out)


# ==========================================================================
# binary ANS over an 8-deep bit tree, adaptive
#
# The byte is decomposed the way LZMA's literal coder does it: node starts at
# 1, node = (node<<1)|bit, eight times. Each node carries its own probability
# and both sides update it identically, so nothing is transmitted.
# ==========================================================================

PBITS = 12
PONE = 1 << PBITS
PINIT = PONE >> 1
ADAPT = 5


def _bin_model_pass(data: bytes, order: int):
    """Run the model forwards, recording the probability used at each bit.

    ANS encodes backwards, so the encoder cannot simply adapt as it goes --
    it must know in advance the probability the decoder will hold when it
    reaches that bit. That is the real cost of an adaptive ANS coder and it
    is why LZMA uses a range coder instead: a range coder runs forwards and
    needs no such pass.
    """
    nctx = 256 * 256 if order else 256
    p = array("H", bytes(2 * nctx))
    for i in range(nctx):
        p[i] = PINIT
    nbits = len(data) * 8
    probs = array("H", bytes(2 * nbits))
    bits = bytearray(nbits)
    j = 0
    prev = 0
    for b in data:
        node = 1
        base = (prev << 8) if order else 0
        for sh in range(7, -1, -1):
            bit = (b >> sh) & 1
            idx = base + node
            pr = p[idx]
            probs[j] = pr
            bits[j] = bit
            j += 1
            if bit:
                p[idx] = pr - (pr >> ADAPT)
            else:
                p[idx] = pr + ((PONE - pr) >> ADAPT)
            node = (node << 1) | bit
        prev = b
    return probs, bits


def _clamp(pr: int) -> int:
    if pr < 1:
        return 1
    if pr > PONE - 1:
        return PONE - 1
    return pr


# ---- rABS: rANS with a two-symbol alphabet ------------------------------
# f0 = P(bit==0) scaled to PONE, f1 = PONE - f0, cum0 = 0, cum1 = f0.

def _rabs(data: bytes, order: int) -> bytes:
    if not data:
        return b"\x00"
    probs, bits = _bin_model_pass(data, order)
    x = L
    out = bytearray()
    ap = out.append
    for j in range(len(bits) - 1, -1, -1):
        p0 = _clamp(probs[j])
        if bits[j]:
            f, c = PONE - p0, p0
        else:
            f, c = p0, 0
        x_max = ((L >> PBITS) << 8) * f
        while x >= x_max:
            ap(x & 0xFF)
            x >>= 8
        x = ((x // f) << PBITS) + (x % f) + c
    for _ in range(4):
        ap(x & 0xFF)
        x >>= 8
    out.reverse()
    return b"\x01" + _hdr(len(data)) + bytes(out)


def _unrabs(blob: bytes, order: int) -> bytes:
    if blob[:1] == b"\x00":
        return b""
    n, at = _read_varint(blob, 1)
    body = blob[at:]
    x = int.from_bytes(body[:4], "big")
    pos = 4
    nctx = 256 * 256 if order else 256
    p = array("H", bytes(2 * nctx))
    for i in range(nctx):
        p[i] = PINIT
    out = bytearray()
    ap = out.append
    prev = 0
    for _ in range(n):
        node = 1
        base = (prev << 8) if order else 0
        for _ in range(8):
            idx = base + node
            pr = p[idx]
            p0 = _clamp(pr)
            slot = x & (PONE - 1)
            if slot < p0:
                bit, f, c = 0, p0, 0
            else:
                bit, f, c = 1, PONE - p0, p0
            x = f * (x >> PBITS) + slot - c
            while x < L:
                x = (x << 8) | body[pos]
                pos += 1
            if bit:
                p[idx] = pr - (pr >> ADAPT)
            else:
                p[idx] = pr + ((PONE - pr) >> ADAPT)
            node = (node << 1) | bit
        b = node & 0xFF
        ap(b)
        prev = b
    return bytes(out)


def rabs0(data: bytes) -> bytes:
    return _rabs(data, 0)


def unrabs0(blob: bytes) -> bytes:
    return _unrabs(blob, 0)


def rabs1(data: bytes) -> bytes:
    return _rabs(data, 1)


def unrabs1(blob: bytes) -> bytes:
    return _unrabs(blob, 1)


# ---- uABS: Duda's original uniform binary ANS ----------------------------
# p = P(bit==1) = P/PONE.
#   encode 1: x' = ceil((x+1)/p) - 1        encode 0: x' = floor(x/(1-p))
#   decode:   c = ceil(x*p); bit = ceil((x+1)*p) - c
#             x = c if bit else x - c
# Renormalisation is a bit at a time and generic: the encode map is
# non-decreasing in x, so shifting x down until the image fits the interval
# is always correct, whatever the probability.

UL = 1 << 16               # state interval is [UL, 2*UL)


def _uabs_enc(bit: int, y: int, P: int) -> int:
    """The exact inverse of the decode map, derived rather than recalled.

    Decoding reads position x and reports (bit, y) where y counts how many
    earlier positions carry that same bit. Encoding must return the unique x
    with that pair, so solve the two conditions together:

      bit 1:  ceil(x*p) == y  and  ceil((x+1)*p) == y+1
              -> x in (y/p - 1, y/p],   one integer,  x = floor(y/p)
      bit 0:  x - ceil(x*p) == y  and  ceil((x+1)*p) == ceil(x*p)
              -> x in [(y+p)/q, (y+1)/q),  one integer,  x = ceil((y+p)/q)

    Both intervals are exactly one long, which is why each has one solution.
    The first version here used the half-remembered `ceil((x+1)/p) - 1` and
    was off by one on every state, so nothing round-tripped.
    """
    if bit:
        return (y * PONE) // P
    return -((-(y * PONE + P)) // (PONE - P))


def uabs(data: bytes, order: int = 0) -> bytes:
    if not data:
        return b"\x00"
    probs, bits = _bin_model_pass(data, order)
    w = BitW()
    x = UL
    for j in range(len(bits) - 1, -1, -1):
        P = PONE - _clamp(probs[j])        # probability of a 1
        bit = bits[j]
        while _uabs_enc(bit, x, P) >= 2 * UL:
            w.push(x & 1, 1)
            x >>= 1
        x = _uabs_enc(bit, x, P)
    buf, total = w.done()
    return b"\x01" + _hdr(len(data), total, x) + buf


def ununabs(blob: bytes, order: int = 0) -> bytes:
    if blob[:1] == b"\x00":
        return b""
    at = 1
    n, at = _read_varint(blob, at)
    total, at = _read_varint(blob, at)
    x, at = _read_varint(blob, at)
    r = BitR(blob[at:], total)
    nctx = 256 * 256 if order else 256
    p = array("H", bytes(2 * nctx))
    for i in range(nctx):
        p[i] = PINIT
    out = bytearray()
    ap = out.append
    prev = 0
    for _ in range(n):
        node = 1
        base = (prev << 8) if order else 0
        for _ in range(8):
            idx = base + node
            pr = p[idx]
            P = PONE - _clamp(pr)
            c = -((-x * P) // PONE)
            bit = (-((-(x + 1) * P) // PONE)) - c
            x = c if bit else x - c
            while x < UL:
                x = (x << 1) | r.pop(1)
            if bit:
                p[idx] = pr - (pr >> ADAPT)
            else:
                p[idx] = pr + ((PONE - pr) >> ADAPT)
            node = (node << 1) | bit
        b = node & 0xFF
        ap(b)
        prev = b
    return bytes(out)


def uabs0(data: bytes) -> bytes:
    return uabs(data, 0)


def ununabs0(blob: bytes) -> bytes:
    return ununabs(blob, 0)
