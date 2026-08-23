#!/usr/bin/env python3
"""TEST-12 -- Huffman over WORDS, not characters.  Mahdi's Thought 12b.

    "huffman but instead of individual characters its whole words or something"

Taken literally and built with a real decoder, then pushed past the literal
version to find where the idea actually pays.

The tokenizer is the whole model here, so it is stated exactly. A CSV text
stream is split into maximal runs of:

    [A-Za-z_]+      a word
    [0-9]+          a number run
    any other byte  one token each  (comma, newline, dot, minus, space, ...)

so `"1234 N HALSTED ST",41.9` becomes
    " | 1234 | (space) | N | (space) | HALSTED | ... | , | 41 | . | 9
Every token is a dictionary entry; the stream is a sequence of ids. This is
the "spaceless words" idea from the text-compression literature, applied to a
CSV rather than to prose.

Variants, all lossless, all decoded and compared before their bytes count:

  H0    canonical Huffman over tokens, order 0            <- the literal idea
  H1c   canonical Huffman over tokens, one code table per
        COLUMN INDEX (which field of the row we are in)   <- the cheap context
  ID+xz token ids as varints, xz behind                   <- word-level LZ
  ID+pp token ids as varints, PPMd behind                 <- word-level PPMd
  raw   the text itself through xz / PPMd                 <- TEST-9 baselines

and the two floors, which are not achievable but bound the idea:

  n*H0  order-0 entropy of the token stream, no table cost
  n*H1  entropy conditioned on the previous token, no table cost

The point of H1c: while streaming a CSV you always know which field you are
in, for free -- count the commas. If token distributions differ per column
(they do: column 3 is always a date, column 9 always one of four codes), a
per-column code table is a genuine model improvement that costs only the
tables. That is the cheapest context a text-stream view can exploit.
"""
from __future__ import annotations

import heapq
import lzma
import re
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2,
                    "preset": 9 | lzma.PRESET_EXTREME}])

TOKEN = re.compile(rb"[A-Za-z_]+|[0-9]+|.", re.S)


def xz(b: bytes) -> bytes:
    return lzma.compress(b, **XZ)


def unxz(b: bytes) -> bytes:
    return lzma.decompress(b, **XZ)


# --------------------------------------------------------------------------
# tokenizer
# --------------------------------------------------------------------------

def tokenize(data: bytes) -> Tuple[List[int], List[bytes]]:
    """-> (id stream, alphabet). Ids are first-appearance order."""
    ids: List[int] = []
    index: Dict[bytes, int] = {}
    alpha: List[bytes] = []
    for m in TOKEN.finditer(data):
        t = m.group(0)
        i = index.get(t)
        if i is None:
            i = index[t] = len(alpha)
            alpha.append(t)
        ids.append(i)
    return ids, alpha


def detokenize(ids: List[int], alpha: List[bytes]) -> bytes:
    return b"".join(alpha[i] for i in ids)


def column_of(ids: List[int], alpha: List[bytes]) -> List[int]:
    """Which field of the row each token belongs to. Counts commas and
    newlines in the token stream -- the decoder can do exactly the same thing
    from tokens it has already decoded, so this context is free."""
    comma = alpha.index(b",") if b"," in alpha else -1
    nl = alpha.index(b"\n") if b"\n" in alpha else -1
    out = []
    col = 0
    for i in ids:
        out.append(col)
        if i == comma:
            col += 1
        elif i == nl:
            col = 0
    return out


# --------------------------------------------------------------------------
# canonical Huffman
# --------------------------------------------------------------------------

def code_lengths(freq: Dict[int, int], limit: int = 30) -> Dict[int, int]:
    """Huffman code lengths, depth-limited by the crude but correct route of
    flattening the frequency distribution until the tree fits."""
    if not freq:
        return {}
    if len(freq) == 1:
        return {next(iter(freq)): 1}
    while True:
        h = [(f, i, None) for i, f in freq.items()]
        heapq.heapify(h)
        parent: Dict = {}
        nxt = -1
        while len(h) > 1:
            a = heapq.heappop(h)
            b = heapq.heappop(h)
            node = nxt
            nxt -= 1
            parent[a[1]] = node
            parent[b[1]] = node
            heapq.heappush(h, (a[0] + b[0], node, None))
        root = h[0][1]
        depth: Dict[int, int] = {}
        ok = True
        for i in freq:
            d, cur = 0, i
            while cur != root:
                cur = parent[cur]
                d += 1
            depth[i] = d
            if d > limit:
                ok = False
        if ok:
            return depth
        freq = {i: (f + 1) // 2 + 1 for i, f in freq.items()}


def canonical(lengths: Dict[int, int]) -> Dict[int, Tuple[int, int]]:
    """-> symbol: (code, length), assigned in (length, symbol) order so the
    decoder can rebuild the table from the lengths alone."""
    items = sorted(lengths.items(), key=lambda kv: (kv[1], kv[0]))
    code, prev = 0, None
    out = {}
    for sym, ln in items:
        if prev is not None:
            code = (code + 1) << (ln - prev)
        out[sym] = (code, ln)
        prev = ln
    return out


class BitWriter:
    def __init__(self):
        self.buf = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, code: int, ln: int) -> None:
        self.acc = (self.acc << ln) | code
        self.n += ln
        while self.n >= 8:
            self.n -= 8
            self.buf.append((self.acc >> self.n) & 0xFF)
        self.acc &= (1 << self.n) - 1

    def done(self) -> bytes:
        if self.n:
            self.buf.append((self.acc << (8 - self.n)) & 0xFF)
        return bytes(self.buf)


class BitReader:
    def __init__(self, b: bytes):
        self.b = b
        self.i = 0
        self.acc = 0
        self.n = 0

    def bit(self) -> int:
        if not self.n:
            self.acc = self.b[self.i]
            self.i += 1
            self.n = 8
        self.n -= 1
        return (self.acc >> self.n) & 1


def decode_table(lengths: Dict[int, int]):
    """Canonical decode tables. The code for the first symbol of length l is
    `(code[l-1] + count[l-1]) << 1`, which has to be advanced for EVERY length
    including the empty ones -- skipping them silently mis-assigns every code
    after the first gap, and a gap is normal on a skewed alphabet."""
    maxlen = max(lengths.values())
    syms_by_len = defaultdict(list)
    for s, l in sorted(lengths.items(), key=lambda kv: (kv[1], kv[0])):
        syms_by_len[l].append(s)
    count = [0] * (maxlen + 2)
    for l, ss in syms_by_len.items():
        count[l] = len(ss)
    first_code, first_index, symbols = {}, {}, []
    code = 0
    for l in range(1, maxlen + 1):
        code = (code + count[l - 1]) << 1
        first_code[l] = code
        first_index[l] = len(symbols)
        symbols.extend(syms_by_len[l])
    return first_code, first_index, symbols, maxlen, count


def huff_decode(br: BitReader, tbl, n: int) -> List[int]:
    first_code, first_index, symbols, maxlen, count = tbl
    out = []
    for _ in range(n):
        code, ln = 0, 0
        while True:
            code = (code << 1) | br.bit()
            ln += 1
            if ln > maxlen:
                raise ValueError("bad code")
            if count[ln] and code - first_code[ln] < count[ln]:
                out.append(symbols[first_index[ln] + code - first_code[ln]])
                break
    return out


# --------------------------------------------------------------------------
# containers
# --------------------------------------------------------------------------

def varint(v: int, out: bytearray) -> None:
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def read_varint(b: bytes, at: int) -> Tuple[int, int]:
    v, sh = 0, 0
    while True:
        c = b[at]
        at += 1
        v |= (c & 0x7F) << sh
        if not c & 0x80:
            return v, at
        sh += 7


def pack_alpha(alpha: List[bytes]) -> bytes:
    h, body = bytearray(), bytearray()
    varint(len(alpha), h)
    for t in alpha:
        varint(len(t), h)
        body += t
    return bytes(h) + bytes(body)


def unpack_alpha(b: bytes, at: int = 0) -> Tuple[List[bytes], int]:
    n, at = read_varint(b, at)
    lens = []
    for _ in range(n):
        v, at = read_varint(b, at)
        lens.append(v)
    out = []
    for ln in lens:
        out.append(b[at:at + ln])
        at += ln
    return out, at


def pack_lengths(lengths: Dict[int, int], nsym: int) -> bytes:
    """One byte per symbol, 0 meaning absent. xz squashes it."""
    a = bytearray(nsym)
    for s, l in lengths.items():
        a[s] = l
    return bytes(a)


def container(parts: List[bytes]) -> bytes:
    h = bytearray()
    varint(len(parts), h)
    for p in parts:
        varint(len(p), h)
    return bytes(h) + b"".join(parts)


def uncontainer(b: bytes) -> List[bytes]:
    n, at = read_varint(b, 0)
    lens = []
    for _ in range(n):
        v, at = read_varint(b, at)
        lens.append(v)
    out = []
    for ln in lens:
        out.append(b[at:at + ln])
        at += ln
    return out


# --------------------------------------------------------------------------
# H0 -- canonical Huffman over tokens, order 0.  The literal idea.
# --------------------------------------------------------------------------

def encode_H0(data: bytes) -> bytes:
    ids, alpha = tokenize(data)
    freq = Counter(ids)
    lengths = code_lengths(freq)
    codes = canonical(lengths)
    bw = BitWriter()
    for i in ids:
        c, l = codes[i]
        bw.put(c, l)
    head = bytearray()
    varint(len(ids), head)
    return container([bytes(head), xz(pack_alpha(alpha)),
                      xz(pack_lengths(lengths, len(alpha))), bw.done()])


def decode_H0(blob: bytes) -> bytes:
    p = uncontainer(blob)
    n, _ = read_varint(p[0], 0)
    alpha, _ = unpack_alpha(unxz(p[1]))
    lb = unxz(p[2])
    lengths = {i: l for i, l in enumerate(lb) if l}
    ids = huff_decode(BitReader(p[3]), decode_table(lengths), n)
    return detokenize(ids, alpha)


# --------------------------------------------------------------------------
# H1c -- one Huffman table per COLUMN INDEX.  The context that is free.
# --------------------------------------------------------------------------

def encode_H1c(data: bytes, maxcol: int = 256) -> bytes:
    ids, alpha = tokenize(data)
    cols = [min(c, maxcol - 1) for c in column_of(ids, alpha)]
    ncol = max(cols) + 1
    freqs = [Counter() for _ in range(ncol)]
    for i, c in zip(ids, cols):
        freqs[c][i] += 1
    tables, codes = [], []
    for f in freqs:
        lg = code_lengths(f)
        tables.append(pack_lengths(lg, len(alpha)))
        codes.append(canonical(lg))
    bw = BitWriter()
    for i, c in zip(ids, cols):
        code, l = codes[c][i]
        bw.put(code, l)
    head = bytearray()
    varint(len(ids), head)
    varint(ncol, head)
    return container([bytes(head), xz(pack_alpha(alpha)),
                      xz(b"".join(tables)), bw.done()])


def decode_H1c(blob: bytes) -> bytes:
    p = uncontainer(blob)
    n, at = read_varint(p[0], 0)
    ncol, at = read_varint(p[0], at)
    alpha, _ = unpack_alpha(unxz(p[1]))
    nsym = len(alpha)
    flat = unxz(p[2])
    tbls = []
    for c in range(ncol):
        seg = flat[c * nsym:(c + 1) * nsym]
        lengths = {i: l for i, l in enumerate(seg) if l}
        tbls.append(decode_table(lengths) if lengths else None)
    comma = alpha.index(b",") if b"," in alpha else -1
    nl = alpha.index(b"\n") if b"\n" in alpha else -1
    br = BitReader(p[3])
    out: List[int] = []
    col = 0
    for _ in range(n):
        t = tbls[min(col, ncol - 1)]
        sym = huff_decode(br, t, 1)[0]
        out.append(sym)
        if sym == comma:
            col += 1
        elif sym == nl:
            col = 0
    return detokenize(out, alpha)


# --------------------------------------------------------------------------
# ID -- the token ids as varints, a general backend behind them
# --------------------------------------------------------------------------

def id_stream(data: bytes) -> Tuple[bytes, bytes]:
    ids, alpha = tokenize(data)
    b = bytearray()
    for i in ids:
        varint(i, b)
    return pack_alpha(alpha), bytes(b)


def id_rebuild(alpha_raw: bytes, stream: bytes) -> bytes:
    alpha, _ = unpack_alpha(alpha_raw)
    out = []
    at, n = 0, len(stream)
    while at < n:
        v, at = read_varint(stream, at)
        out.append(alpha[v])
    return b"".join(out)


# --------------------------------------------------------------------------
# the floors -- what the model could pay if the tables were free
# --------------------------------------------------------------------------

def entropy_floor(data: bytes) -> Tuple[float, float, int, int]:
    """(order-0 bytes, order-1 bytes, ntokens, alphabet size), tables free.

    numpy, not dicts. The dict version built a Counter per context and hit
    3.3 GB on a 34 MB table -- the watchdog killed it. Pair counting is done
    by sorting a single int64 array of (prev * A + cur), which is one
    allocation of 8 bytes per token."""
    import math
    import numpy as np
    ids, alpha = tokenize(data)
    n = len(ids)
    A = len(alpha)
    a = np.asarray(ids, dtype=np.int64)
    del ids

    c0 = np.bincount(a, minlength=A).astype(np.float64)
    c0 = c0[c0 > 0]
    p = c0 / n
    h0 = float(-(p * np.log2(p)).sum())

    if n > 1:
        keys = a[:-1] * A + a[1:]
        keys.sort()
        # run lengths of equal keys = joint counts; marginal = count of prev
        edge = np.flatnonzero(np.diff(keys))
        joint = np.diff(np.concatenate(([-1], edge, [len(keys) - 1]))).astype(
            np.float64)
        firsts = keys[np.concatenate(([0], edge + 1))] // A
        del keys, edge
        marg = np.bincount(firsts, minlength=A).astype(np.float64)
        # bincount of firsts counts DISTINCT successors, not occurrences --
        # recompute the true marginal from the joint counts instead
        marg = np.bincount(firsts, weights=joint, minlength=A)
        bits1 = float(-(joint * np.log2(joint / marg[firsts])).sum())
    else:
        bits1 = 0.0
    return n * h0 / 8, bits1 / 8, n, A
