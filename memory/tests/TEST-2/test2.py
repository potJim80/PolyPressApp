#!/usr/bin/env python3
"""TEST-2 -- one column: plain xz, or an ANS entropy coder?

Case A  plain `xz -9e` over the column as text, one value per line.
Case B  a static rANS coder over the same bytes, order 0 and order 1.

Two further variants exist because a bare order-0 byte coder is not what
anyone would actually ship, and reporting only the bare version would answer
a question nobody asked:

  D-xz    number-encode (first-appearance dictionary), then xz the code
          planes and the dictionary. This is TEST-1's winning scheme.
  D-ans   the same model, code planes handed to rANS instead of xz. This is
          the one that isolates the entropy coder: same bytes in, different
          coder, and LAW 1's caveat said the bit-packed variant sat 1.20x
          above the order-0 floor.

Every variant here has a decoder and is compared value by value before its
size is counted. Sizes are whole self-contained containers -- the frequency
table, the dictionary and the lengths are all charged.
"""
from __future__ import annotations

import lzma
import math
from typing import Dict, List, Tuple

import rans
from rans import _varint, _read_varint

XZ = rans.XZ


def xz(data: bytes) -> bytes:
    return lzma.compress(data, **XZ)


def unxz(data: bytes) -> bytes:
    return lzma.decompress(data, **XZ)


# --------------------------------------------------------------------------
# the column as bytes -- length-prefixed, so a value may contain anything
# --------------------------------------------------------------------------

def flatten(values: List[str]) -> bytes:
    """One value per line. A cell holding a newline is why callers screen."""
    return "\n".join(values).encode("utf-8")


def unflatten(blob: bytes, n: int) -> List[str]:
    return blob.decode("utf-8").split("\n") if n else []


def pack_strings(strs: List[str]) -> bytes:
    """Length-prefixed concatenation: never ambiguous, whatever the bytes."""
    body = bytearray()
    heads = bytearray()
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
# the dictionary model, shared by D-xz and D-ans
# --------------------------------------------------------------------------

def number_encode(values: List[str]) -> Tuple[List[str], List[int]]:
    """First-appearance ids. LAW 1b measured these as 7.3% better than
    lexicographic, so use the rule that won."""
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
    return alphabet, codes


def code_planes(codes: List[int], k: int) -> Tuple[int, List[bytes]]:
    """Fixed-width big-endian codes, split into byte planes.

    Split rather than interleaved because the high plane of a 3-byte code is
    nearly constant and the low plane is nearly uniform -- a single stream
    makes the coder average the two and lose both.
    """
    width = max(1, (max(1, k - 1).bit_length() + 7) // 8)
    planes = [bytearray(len(codes)) for _ in range(width)]
    for i, c in enumerate(codes):
        for p in range(width):
            planes[width - 1 - p][i] = (c >> (8 * p)) & 0xFF
    return width, [bytes(p) for p in planes]


def unplanes(planes: List[bytes], n: int) -> List[int]:
    width = len(planes)
    out = [0] * n
    for i in range(n):
        c = 0
        for p in range(width):
            c = (c << 8) | planes[p][i]
        out[i] = c
    return out


def _container(parts: List[bytes]) -> bytes:
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


def dict_encode(values: List[str], coder: str) -> bytes:
    alphabet, codes = number_encode(values)
    width, planes = code_planes(codes, len(alphabet))
    dic = xz(pack_strings(alphabet))
    if coder == "xz":
        bodies = [xz(p) for p in planes]
    else:
        bodies = [rans.rans0(p) for p in planes]
    head = bytearray()
    _varint(len(values), head)
    _varint(width, head)
    return _container([bytes(head), dic] + bodies)


def dict_decode(blob: bytes, coder: str) -> List[str]:
    parts = _uncontainer(blob)
    n, at = _read_varint(parts[0], 0)
    width, at = _read_varint(parts[0], at)
    alphabet = unpack_strings(unxz(parts[1]))
    if coder == "xz":
        planes = [unxz(p) for p in parts[2:2 + width]]
    else:
        planes = [rans.unrans0(p) for p in parts[2:2 + width]]
    return [alphabet[c] for c in unplanes(planes, n)]


# --------------------------------------------------------------------------
# the order-0 floor, for reading the table against
# --------------------------------------------------------------------------

def order0_floor(data: bytes) -> float:
    """Bytes an ideal order-0 byte coder would need. Not achievable with the
    table included -- this is the target rANS is trying to reach, not a rival."""
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    bits = -sum(c * math.log2(c / n) for c in counts if c)
    return bits / 8.0


# --------------------------------------------------------------------------
# measure one column
# --------------------------------------------------------------------------

VARIANTS = ("xz", "ans0", "ans1", "D-xz", "D-ans")


def measure(name: str, kind: str, values: List[str]) -> dict:
    raw = flatten(values)
    n = len(values)
    k = len(set(values))

    sizes = {}

    a = xz(raw)
    assert unxz(a) == raw
    sizes["xz"] = len(a)

    b0 = rans.rans0(raw)
    assert rans.unrans0(b0) == raw, f"{name}: order-0 rANS round-trip failed"
    sizes["ans0"] = len(b0)

    b1 = rans.rans1(raw)
    assert rans.unrans1(b1) == raw, f"{name}: order-1 rANS round-trip failed"
    sizes["ans1"] = len(b1)

    for coder, tag in (("xz", "D-xz"), ("ans", "D-ans")):
        blob = dict_encode(values, coder)
        assert dict_decode(blob, coder) == values, f"{name}: {tag} round-trip failed"
        sizes[tag] = len(blob)

    return {"name": name, "kind": kind, "n": n, "k": k, "raw": len(raw),
            "floor": order0_floor(raw), **sizes}
