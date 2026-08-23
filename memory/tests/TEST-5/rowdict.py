#!/usr/bin/env python3
"""TEST-5 -- number-encode every cell, stay ROW-major, one xz stream.

Mahdi's proposal: forget columnar reorganisation. Walk row by row, left to
right; every non-numeric cell becomes a number from a growing dictionary;
numeric cells pass through verbatim. Feed each row into the SAME xz stream.

The appeal is real: it is single-pass, O(1) memory, chunkable by row, and the
LZ dictionary still spans the whole file. It would dissolve the chunking
problem entirely.

What it gives up is column locality. This measures exactly what that costs, by
encoding the identical numbers in both layouts:

  B  row-major   -- the proposal
  C  col-major   -- the same ids, transposed, nothing else changed

so the difference between B and C is the layout and nothing else.

Everything is decoded and compared cell by cell before its size is counted.
"""
from __future__ import annotations

import lzma
import re
from typing import Dict, List, Tuple

XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2,
                    "preset": 9 | lzma.PRESET_EXTREME}])

NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


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


def pack_strings(strs: List[str]) -> bytes:
    heads, body = bytearray(), bytearray()
    _varint(len(strs), heads)
    for s in strs:
        b = s.encode("utf-8")
        _varint(len(b), heads)
        body += b
    return bytes(heads) + bytes(body)


def unpack_strings(raw: bytes, at: int = 0) -> Tuple[List[str], int]:
    n, at = _read_varint(raw, at)
    lens = []
    for _ in range(n):
        v, at = _read_varint(raw, at)
        lens.append(v)
    out = []
    for ln in lens:
        out.append(raw[at:at + ln].decode("utf-8"))
        at += ln
    return out, at


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


# --------------------------------------------------------------------------
# classification: a column passes through verbatim only if EVERY cell is a
# number. A single non-numeric cell makes the whole column dictionary-coded,
# because the decoder must be able to tell an id from a value without being
# told per cell.
# --------------------------------------------------------------------------

def classify(rows: List[List[str]], ncol: int) -> List[bool]:
    is_num = [True] * ncol
    for r in rows:
        for j in range(ncol):
            if is_num[j] and not NUM.match(r[j]):
                is_num[j] = False
    return is_num


def build_dicts(rows, ncol, is_num):
    """First-appearance ids, per column, assigned in ROW order -- which is the
    order the proposal's single pass would see them in."""
    maps: List[Dict[str, int]] = [{} for _ in range(ncol)]
    for r in rows:
        for j in range(ncol):
            if not is_num[j]:
                m = maps[j]
                if r[j] not in m:
                    m[r[j]] = len(m)
    alphabets = []
    for j in range(ncol):
        a = [""] * len(maps[j])
        for v, i in maps[j].items():
            a[i] = v
        alphabets.append(a)
    return maps, alphabets


def _cells(rows, ncol, is_num, maps):
    """Every cell as its transmitted token: a verbatim number or a decimal id."""
    out = []
    for r in rows:
        out.append([r[j] if is_num[j] else str(maps[j][r[j]])
                    for j in range(ncol)])
    return out


def _header(ncol, nrow, is_num, alphabets) -> bytes:
    h = bytearray()
    _varint(ncol, h)
    _varint(nrow, h)
    for j in range(ncol):
        h.append(1 if is_num[j] else 0)
    parts = [bytes(h)] + [pack_strings(a) for a in alphabets]
    return xz(_container(parts))


def _unheader(blob: bytes):
    parts = _uncontainer(unxz(blob))
    ncol, at = _read_varint(parts[0], 0)
    nrow, at = _read_varint(parts[0], at)
    is_num = [bool(parts[0][at + j]) for j in range(ncol)]
    alphabets = [unpack_strings(parts[1 + j])[0] for j in range(ncol)]
    return ncol, nrow, is_num, alphabets


# --------------------------------------------------------------------------
# B -- row-major, the proposal
# --------------------------------------------------------------------------

def encode_row(rows: List[List[str]]) -> bytes:
    ncol = len(rows[0])
    is_num = classify(rows, ncol)
    maps, alphabets = build_dicts(rows, ncol, is_num)
    cells = _cells(rows, ncol, is_num, maps)
    body = "\n".join(",".join(r) for r in cells).encode()
    return _container([_header(ncol, len(rows), is_num, alphabets), xz(body)])


def decode_row(blob: bytes) -> List[List[str]]:
    p = _uncontainer(blob)
    ncol, nrow, is_num, alphabets = _unheader(p[0])
    lines = unxz(p[1]).decode().split("\n")
    out = []
    for ln in lines:
        f = ln.split(",")
        out.append([f[j] if is_num[j] else alphabets[j][int(f[j])]
                    for j in range(ncol)])
    return out


# --------------------------------------------------------------------------
# C -- identical ids, column-major. The ONLY difference from B is layout.
# --------------------------------------------------------------------------

def encode_col(rows: List[List[str]]) -> bytes:
    ncol = len(rows[0])
    is_num = classify(rows, ncol)
    maps, alphabets = build_dicts(rows, ncol, is_num)
    cells = _cells(rows, ncol, is_num, maps)
    cols = ["\n".join(cells[i][j] for i in range(len(rows)))
            for j in range(ncol)]
    body = "\n".join(cols).encode()
    return _container([_header(ncol, len(rows), is_num, alphabets), xz(body)])


def decode_col(blob: bytes) -> List[List[str]]:
    p = _uncontainer(blob)
    ncol, nrow, is_num, alphabets = _unheader(p[0])
    toks = unxz(p[1]).decode().split("\n")
    out = [[None] * ncol for _ in range(nrow)]
    at = 0
    for j in range(ncol):
        for i in range(nrow):
            t = toks[at]; at += 1
            out[i][j] = t if is_num[j] else alphabets[j][int(t)]
    return out
