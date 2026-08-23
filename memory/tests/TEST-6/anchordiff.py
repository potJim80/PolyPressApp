#!/usr/bin/env python3
"""TEST-6 -- anchor rows plus differences, on top of TEST-5's row-major scheme.

Mahdi's proposal: keep a few anchor cells holding the true encoded number;
store every other cell as a difference. Then compress.

Differencing is taken DOWN THE COLUMN (`v[i][j] - v[i-1][j]`), not across the
row, because adjacent cells in a row are unrelated quantities -- `zip` minus
`incident_type` means nothing -- while the same column one row later is the
one place a difference is meaningful. Anchors are whole rows at a fixed
interval, which is what makes them useful twice: they bound error propagation
and they are exactly where a chunk can start.

Three column kinds, decided once:

  dictid   non-numeric, distinct*2 <= nrows -- dictionary id, an integer
  intnum   every cell an exact integer that round-trips through str(int(x))
  text     everything else, passed through verbatim so xz can still match
           substrings across cells

Only the first two are differenced. `text` is left alone deliberately: TEST-5
measured that dictionary-encoding high-cardinality text costs more than it
saves, because it destroys the substring matching xz was living on.
"""
from __future__ import annotations

import lzma
import re
from typing import Dict, List, Tuple

XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2,
                    "preset": 9 | lzma.PRESET_EXTREME}])
INT = re.compile(r"^-?\d+$")


def xz(b: bytes) -> bytes:
    return lzma.compress(b, **XZ)


def unxz(b: bytes) -> bytes:
    return lzma.decompress(b, **XZ)


def zig(v: int) -> int:
    return (v << 1) ^ (v >> 63) if v >= 0 else ((-v) << 1) - 1


def unzig(u: int) -> int:
    return -((u + 1) >> 1) if u & 1 else u >> 1


def varint(v: int, out: bytearray) -> None:
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def read_varint(buf: bytes, at: int) -> Tuple[int, int]:
    v, sh = 0, 0
    while True:
        b = buf[at]
        at += 1
        v |= (b & 0x7F) << sh
        if not b & 0x80:
            return v, at
        sh += 7


def pack_strings(strs: List[str]) -> bytes:
    heads, body = bytearray(), bytearray()
    varint(len(strs), heads)
    for s in strs:
        b = s.encode("utf-8")
        varint(len(b), heads)
        body += b
    return bytes(heads) + bytes(body)


def unpack_strings(raw: bytes) -> List[str]:
    n, at = read_varint(raw, 0)
    lens = []
    for _ in range(n):
        v, at = read_varint(raw, at)
        lens.append(v)
    out = []
    for ln in lens:
        out.append(raw[at:at + ln].decode("utf-8"))
        at += ln
    return out


def container(parts: List[bytes]) -> bytes:
    h = bytearray()
    varint(len(parts), h)
    for p in parts:
        varint(len(p), h)
    return bytes(h) + b"".join(parts)


def uncontainer(blob: bytes) -> List[bytes]:
    n, at = read_varint(blob, 0)
    lens = []
    for _ in range(n):
        v, at = read_varint(blob, at)
        lens.append(v)
    out = []
    for ln in lens:
        out.append(blob[at:at + ln])
        at += ln
    return out


DICTID, INTNUM, TEXT = 0, 1, 2


def plan(rows: List[List[str]], ncol: int):
    """Column kinds, dictionaries, and the integer matrix to be differenced."""
    n = len(rows)
    kinds, maps, alpha = [], [], []
    for j in range(ncol):
        col = [r[j] for r in rows]
        if all(INT.match(c) and str(int(c)) == c for c in col):
            kinds.append(INTNUM); maps.append(None); alpha.append([])
            continue
        uniq = set(col)
        if len(uniq) * 2 <= n:
            m: Dict[str, int] = {}
            a: List[str] = []
            for c in col:
                if c not in m:
                    m[c] = len(m); a.append(c)
            kinds.append(DICTID); maps.append(m); alpha.append(a)
        else:
            kinds.append(TEXT); maps.append(None); alpha.append([])
    return kinds, maps, alpha


def encode(rows: List[List[str]], anchor: int) -> bytes:
    """`anchor` rows apart, a row is stored absolute. 0 means never difference."""
    ncol = len(rows[0])
    n = len(rows)
    kinds, maps, alpha = plan(rows, ncol)
    ints = [j for j in range(ncol) if kinds[j] != TEXT]

    prev = [0] * ncol
    num = bytearray()
    txt: List[str] = []
    for i, r in enumerate(rows):
        base = (anchor == 0 and i == 0) or (anchor and i % anchor == 0)
        for j in ints:
            v = maps[j][r[j]] if kinds[j] == DICTID else int(r[j])
            varint(zig(v if base else v - prev[j]), num)
            prev[j] = v
        for j in range(ncol):
            if kinds[j] == TEXT:
                txt.append(r[j])

    head = bytearray()
    varint(ncol, head); varint(n, head); varint(anchor, head)
    for j in range(ncol):
        head.append(kinds[j])
    parts = [bytes(head)] + [pack_strings(a) for a in alpha]
    return container([xz(container(parts)), xz(bytes(num)),
                      xz(pack_strings(txt))])


def decode(blob: bytes) -> List[List[str]]:
    p = uncontainer(blob)
    parts = uncontainer(unxz(p[0]))
    ncol, at = read_varint(parts[0], 0)
    n, at = read_varint(parts[0], at)
    anchor, at = read_varint(parts[0], at)
    kinds = [parts[0][at + j] for j in range(ncol)]
    alpha = [unpack_strings(parts[1 + j]) for j in range(ncol)]
    num = unxz(p[1])
    txt = unpack_strings(unxz(p[2]))

    ints = [j for j in range(ncol) if kinds[j] != TEXT]
    prev = [0] * ncol
    at = 0
    ti = 0
    out = []
    for i in range(n):
        base = (anchor == 0 and i == 0) or (anchor and i % anchor == 0)
        row = [None] * ncol
        for j in ints:
            u, at = read_varint(num, at)
            d = unzig(u)
            v = d if base else prev[j] + d
            prev[j] = v
            row[j] = alpha[j][v] if kinds[j] == DICTID else str(v)
        for j in range(ncol):
            if kinds[j] == TEXT:
                row[j] = txt[ti]; ti += 1
        out.append(row)
    return out


def encode_abs(rows: List[List[str]]) -> bytes:
    """No differencing at all -- TEST-5's best, as the control."""
    return encode(rows, 1)
