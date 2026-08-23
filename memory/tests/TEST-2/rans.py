#!/usr/bin/env python3
"""Static rANS over a byte alphabet -- encoder and decoder, order 0 and order 1.

This is the real thing, not a size estimate: every measurement in TEST-2 is
decoded and compared to the input before its size is counted. The layout is
ryg's rans_byte (Fabian Giesen, public domain), transcribed:

  state is 32-bit, renormalised a byte at a time, L = 1<<23, 12-bit frequency
  scale. Encoding runs backwards over the symbols and the byte stream comes
  out reversed; decoding runs forwards.

The frequency table is part of the output and is charged in full. An entropy
coder that reports only its payload is measuring nothing -- the table is
exactly what an adaptive coder would not have to send, and on a short column
it is most of the file.
"""
from __future__ import annotations

import lzma
from typing import List, Tuple

SCALE_BITS = 12
SCALE = 1 << SCALE_BITS
L = 1 << 23          # lower bound of the normalised interval
MASK = SCALE - 1

XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])


# --------------------------------------------------------------------------
# frequency tables
# --------------------------------------------------------------------------

def normalise(counts: List[int]) -> List[int]:
    """Scale `counts` so they sum to exactly SCALE, every used symbol >= 1."""
    total = sum(counts)
    if total == 0:
        return [0] * 256
    freq = [0] * 256
    # proportional first pass, then fix the residue on the largest symbols
    for s, c in enumerate(counts):
        if c:
            freq[s] = max(1, (c * SCALE) // total)
    diff = SCALE - sum(freq)
    order = sorted((s for s in range(256) if freq[s]),
                   key=lambda s: -freq[s])
    i = 0
    while diff != 0:
        s = order[i % len(order)]
        if diff > 0:
            freq[s] += 1
            diff -= 1
        elif freq[s] > 1:
            freq[s] -= 1
            diff += 1
        i += 1
    return freq


def cumulative(freq: List[int]) -> Tuple[List[int], bytearray]:
    """Cumulative starts, plus the slot -> symbol lookup the decoder needs."""
    cum = [0] * 257
    for s in range(256):
        cum[s + 1] = cum[s] + freq[s]
    look = bytearray(SCALE)
    for s in range(256):
        if freq[s]:
            look[cum[s]:cum[s] + freq[s]] = bytes([s]) * freq[s]
    return cum, look


def _varint(v: int, out: bytearray) -> None:
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def _read_varint(buf: bytes, at: int) -> Tuple[int, int]:
    v, sh = 0, 0
    while True:
        b = buf[at]
        at += 1
        v |= (b & 0x7F) << sh
        if not b & 0x80:
            return v, at
        sh += 7


def pack_table(freq: List[int]) -> bytes:
    """A table as bytes. xz'd, because the cost is charged and this is fair."""
    raw = bytearray()
    for s in range(256):
        _varint(freq[s], raw)
    return lzma.compress(bytes(raw), **XZ)


def unpack_table(blob: bytes) -> List[int]:
    raw = lzma.decompress(blob, **XZ)
    freq, at = [], 0
    for _ in range(256):
        v, at = _read_varint(raw, at)
        freq.append(v)
    return freq


# --------------------------------------------------------------------------
# order 0
# --------------------------------------------------------------------------

def encode0(data: bytes, freq: List[int]) -> bytes:
    cum, _ = cumulative(freq)
    x = L
    out = bytearray()
    ap = out.append
    for s in reversed(data):
        f = freq[s]
        x_max = ((L >> SCALE_BITS) << 8) * f
        while x >= x_max:
            ap(x & 0xFF)
            x >>= 8
        x = ((x // f) << SCALE_BITS) + (x % f) + cum[s]
    for _ in range(4):
        ap(x & 0xFF)
        x >>= 8
    out.reverse()
    return bytes(out)


def decode0(blob: bytes, n: int, freq: List[int]) -> bytes:
    cum, look = cumulative(freq)
    x = int.from_bytes(blob[:4], "big")
    at = 4
    out = bytearray()
    ap = out.append
    for _ in range(n):
        slot = x & MASK
        s = look[slot]
        ap(s)
        x = freq[s] * (x >> SCALE_BITS) + slot - cum[s]
        while x < L:
            x = (x << 8) | blob[at]
            at += 1
    return bytes(out)


def rans0(data: bytes) -> bytes:
    """A self-contained order-0 rANS container: table + length + payload."""
    if not data:
        return b"\x00"
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    freq = normalise(counts)
    tbl = pack_table(freq)
    body = encode0(data, freq)
    hdr = bytearray(b"\x01")
    _varint(len(data), hdr)
    _varint(len(tbl), hdr)
    return bytes(hdr) + tbl + body


def unrans0(blob: bytes) -> bytes:
    if blob[:1] == b"\x00":
        return b""
    n, at = _read_varint(blob, 1)
    tlen, at = _read_varint(blob, at)
    freq = unpack_table(blob[at:at + tlen])
    return decode0(blob[at + tlen:], n, freq)


# --------------------------------------------------------------------------
# order 1 -- context is the previous byte
# --------------------------------------------------------------------------

def _order1_tables(data: bytes):
    """Counts per context. Contexts that never occur cost nothing."""
    counts = {}
    prev = 0
    for b in data:
        c = counts.get(prev)
        if c is None:
            c = counts[prev] = [0] * 256
        c[b] += 1
        prev = b
    return {ctx: normalise(c) for ctx, c in counts.items()}


def _pack_o1(tables) -> bytes:
    raw = bytearray()
    _varint(len(tables), raw)
    for ctx in sorted(tables):
        raw.append(ctx)
        for s in range(256):
            _varint(tables[ctx][s], raw)
    return lzma.compress(bytes(raw), **XZ)


def _unpack_o1(blob: bytes):
    raw = lzma.decompress(blob, **XZ)
    n, at = _read_varint(raw, 0)
    tables = {}
    for _ in range(n):
        ctx = raw[at]
        at += 1
        freq = []
        for _ in range(256):
            v, at = _read_varint(raw, at)
            freq.append(v)
        tables[ctx] = freq
    return tables


def rans1(data: bytes) -> bytes:
    if not data:
        return b"\x00"
    tables = _order1_tables(data)
    cums = {ctx: cumulative(f)[0] for ctx, f in tables.items()}
    tbl = _pack_o1(tables)

    x = L
    out = bytearray()
    ap = out.append
    n = len(data)
    for i in range(n - 1, -1, -1):
        s = data[i]
        ctx = data[i - 1] if i else 0
        f = tables[ctx][s]
        x_max = ((L >> SCALE_BITS) << 8) * f
        while x >= x_max:
            ap(x & 0xFF)
            x >>= 8
        x = ((x // f) << SCALE_BITS) + (x % f) + cums[ctx][s]
    for _ in range(4):
        ap(x & 0xFF)
        x >>= 8
    out.reverse()

    hdr = bytearray(b"\x01")
    _varint(n, hdr)
    _varint(len(tbl), hdr)
    return bytes(hdr) + tbl + bytes(out)


def unrans1(blob: bytes) -> bytes:
    if blob[:1] == b"\x00":
        return b""
    n, at = _read_varint(blob, 1)
    tlen, at = _read_varint(blob, at)
    tables = _unpack_o1(blob[at:at + tlen])
    looks = {ctx: cumulative(f)[1] for ctx, f in tables.items()}
    cums = {ctx: cumulative(f)[0] for ctx, f in tables.items()}
    body = blob[at + tlen:]

    x = int.from_bytes(body[:4], "big")
    pos = 4
    out = bytearray()
    ap = out.append
    ctx = 0
    for _ in range(n):
        slot = x & MASK
        s = looks[ctx][slot]
        ap(s)
        freq = tables[ctx]
        x = freq[s] * (x >> SCALE_BITS) + slot - cums[ctx][s]
        while x < L:
            x = (x << 8) | body[pos]
            pos += 1
        ctx = s
    return bytes(out)
