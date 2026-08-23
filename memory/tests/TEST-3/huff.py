#!/usr/bin/env python3
"""Canonical Huffman, over BYTES and over VALUES. Encoder and decoder.

The idea under test: forget the permutation and the pancake, treat each
distinct **cell value** as one Huffman symbol, and let the tree carry the
code assignment.

One correction the implementation forces into the open: **the tree does not
replace the dictionary.** A Huffman tree over values assigns a code length to
each symbol, but the decoder still has to be told what string each symbol
*is*. So the container is:

    distinct values (the dictionary -- unavoidable)
  + one code length per value (the tree, in canonical form)
  + the bitstream

Canonical Huffman is used because it is the form that makes the tree cheap:
code lengths alone determine the codes, so no shape needs transmitting.
"""
from __future__ import annotations

import heapq
import lzma
from typing import Dict, List, Sequence, Tuple

XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2,
                    "preset": 9 | lzma.PRESET_EXTREME}])


def xz(b: bytes) -> bytes:
    return lzma.compress(b, **XZ)


def unxz(b: bytes) -> bytes:
    return lzma.decompress(b, **XZ)


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


def pack_ints(vals: Sequence[int]) -> bytes:
    out = bytearray()
    _varint(len(vals), out)
    for v in vals:
        _varint(v, out)
    return bytes(out)


def unpack_ints(raw: bytes) -> List[int]:
    n, at = _read_varint(raw, 0)
    out = []
    for _ in range(n):
        v, at = _read_varint(raw, at)
        out.append(v)
    return out


def pack_strings(strs: Sequence[str]) -> bytes:
    heads, body = bytearray(), bytearray()
    _varint(len(strs), heads)
    for s in strs:
        b = s.encode("utf-8")
        _varint(len(b), heads)
        body += b
    return bytes(heads) + bytes(body)


def unpack_strings(raw: bytes) -> List[str]:
    n, at = _read_varint(raw, 0)
    lens = []
    for _ in range(n):
        v, at = _read_varint(raw, at)
        lens.append(v)
    out = []
    for ln in lens:
        out.append(raw[at:at + ln].decode("utf-8"))
        at += ln
    return out


# --------------------------------------------------------------------------
# code lengths
# --------------------------------------------------------------------------

def code_lengths(counts: Sequence[int]) -> List[int]:
    """Huffman code lengths for `counts`. Symbols with count 0 get length 0.

    A one-symbol alphabet gets length 0 as well: there is nothing to
    distinguish, so the bitstream is empty and the decoder emits n copies.
    That case is a real one -- a constant column -- and dropping it produces
    a decoder that hangs on zero-length codes.
    """
    live = [(c, s) for s, c in enumerate(counts) if c]
    if len(live) <= 1:
        return [0] * len(counts)
    heap = [(c, s, None, None) for c, s in live]
    heapq.heapify(heap)
    # build the tree, then walk it for depths
    nodes: Dict[int, Tuple] = {}
    nxt = len(counts)
    while len(heap) > 1:
        c1, s1, _, _ = heapq.heappop(heap)
        c2, s2, _, _ = heapq.heappop(heap)
        nodes[nxt] = (s1, s2)
        heapq.heappush(heap, (c1 + c2, nxt, None, None))
        nxt += 1
    root = heap[0][1]
    lens = [0] * len(counts)
    stack = [(root, 0)]
    while stack:
        node, d = stack.pop()
        kids = nodes.get(node)
        if kids is None:
            lens[node] = max(1, d)
        else:
            stack.append((kids[0], d + 1))
            stack.append((kids[1], d + 1))
    return lens


def canonical(lens: Sequence[int]) -> Tuple[Dict[int, Tuple[int, int]], dict]:
    """Canonical codes from lengths.

    Returns (encode map symbol -> (code, length), decode tables). Both sides
    derive these from the lengths alone, which is the whole point of the
    canonical form -- the tree shape is never transmitted.
    """
    order = sorted((l, s) for s, l in enumerate(lens) if l)
    enc: Dict[int, Tuple[int, int]] = {}
    code = 0
    prev = 0
    first_code: Dict[int, int] = {}
    first_idx: Dict[int, int] = {}
    count: Dict[int, int] = {}
    syms: List[int] = []
    for i, (l, s) in enumerate(order):
        code <<= (l - prev)
        prev = l
        if l not in first_code:
            first_code[l] = code
            first_idx[l] = i
            count[l] = 0
        enc[s] = (code, l)
        count[l] += 1
        syms.append(s)
        code += 1
    return enc, {"first_code": first_code, "first_idx": first_idx,
                 "count": count, "syms": syms,
                 "lengths": sorted(first_code)}


def encode_bits(codes: Sequence[int], enc: Dict[int, Tuple[int, int]]) -> Tuple[bytes, int]:
    acc = 0
    nbits = 0
    out = bytearray()
    for s in codes:
        c, l = enc[s]
        acc = (acc << l) | c
        nbits += l
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
            acc &= (1 << nbits) - 1
    total = len(out) * 8 + nbits
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out), total


def decode_bits(buf: bytes, total: int, n: int, tab: dict) -> List[int]:
    """Canonical decode: accumulate bits until the code falls inside a length's
    range. No tree walk, no per-bit node objects."""
    first_code = tab["first_code"]
    first_idx = tab["first_idx"]
    count = tab["count"]
    syms = tab["syms"]
    out = []
    pos = 0
    code = 0
    ln = 0
    while len(out) < n:
        byte = buf[pos >> 3]
        bit = (byte >> (7 - (pos & 7))) & 1
        pos += 1
        code = (code << 1) | bit
        ln += 1
        fc = first_code.get(ln)
        if fc is not None and code - fc < count[ln]:
            out.append(syms[first_idx[ln] + (code - fc)])
            code = 0
            ln = 0
    return out


# --------------------------------------------------------------------------
# over VALUES -- each distinct cell is one symbol
# --------------------------------------------------------------------------

def _container(parts: Sequence[bytes]) -> bytes:
    hdr = bytearray()
    _varint(len(parts), hdr)
    for p in parts:
        _varint(len(p), hdr)
    return bytes(hdr) + b"".join(parts)


def _uncontainer(blob: bytes) -> List[bytes]:
    n, at = _read_varint(blob, 0)
    lens = []
    for _ in range(n):
        v, at = _read_varint(blob, at)
        lens.append(v)
    out = []
    for ln in lens:
        out.append(blob[at:at + ln])
        at += ln
    return out


def huff_values(values: Sequence[str], xz_alphabet: bool = True) -> bytes:
    seen: Dict[str, int] = {}
    codes = []
    for v in values:
        c = seen.get(v)
        if c is None:
            c = seen[v] = len(seen)
        codes.append(c)
    alphabet = [""] * len(seen)
    for v, c in seen.items():
        alphabet[c] = v
    counts = [0] * len(alphabet)
    for c in codes:
        counts[c] += 1

    lens = code_lengths(counts)
    enc, _ = canonical(lens)
    if enc:
        body, total = encode_bits(codes, enc)
    else:
        body, total = b"", 0

    alpha = pack_strings(alphabet)
    head = bytearray()
    _varint(len(values), head)
    _varint(total, head)
    return _container([bytes(head),
                       xz(alpha) if xz_alphabet else alpha,
                       xz(pack_ints(lens)),
                       body])


def unhuff_values(blob: bytes, xz_alphabet: bool = True) -> List[str]:
    parts = _uncontainer(blob)
    n, at = _read_varint(parts[0], 0)
    total, at = _read_varint(parts[0], at)
    alphabet = unpack_strings(unxz(parts[1]) if xz_alphabet else parts[1])
    lens = unpack_ints(unxz(parts[2]))
    _, tab = canonical(lens)
    if not tab["syms"]:
        return [alphabet[0]] * n if alphabet else []
    return [alphabet[c] for c in decode_bits(parts[3], total, n, tab)]


# --------------------------------------------------------------------------
# over BYTES -- the ordinary thing, for contrast
# --------------------------------------------------------------------------

def huff_bytes(data: bytes) -> bytes:
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    lens = code_lengths(counts)
    enc, _ = canonical(lens)
    if enc:
        body, total = encode_bits(data, enc)
    else:
        body, total = b"", 0
    # A one-symbol alphabet has all-zero code lengths, so the lengths alone
    # cannot say WHICH byte it was -- the value path gets this for free from
    # its stored alphabet, the byte path does not. Carry the symbol.
    single = data[0] if (data and not enc) else 0
    head = bytearray()
    _varint(len(data), head)
    _varint(total, head)
    _varint(single, head)
    return _container([bytes(head), xz(pack_ints(lens)), body])


def unhuff_bytes(blob: bytes) -> bytes:
    parts = _uncontainer(blob)
    n, at = _read_varint(parts[0], 0)
    total, at = _read_varint(parts[0], at)
    single, at = _read_varint(parts[0], at)
    lens = unpack_ints(unxz(parts[1]))
    _, tab = canonical(lens)
    if not tab["syms"]:
        return bytes([single]) * n
    return bytes(decode_bits(parts[2], total, n, tab))
