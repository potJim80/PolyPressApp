#!/usr/bin/env python3
"""TEST-13 -- the alphabet's ORDER, which the design agent measured at 36%.

The claim to check, from the design pass (a size probe with no decoder, so it
gets checked here with one): across the 13-table corpus the distinct strings
are ~60% of the compressed output, and they are shipped in FIRST-APPEARANCE
order. Sorting them lexicographically makes neighbours share prefixes, and
front coding then deletes the shared prefix outright.

  6,739,572 B  appearance order   (what TEST-7/8 ship)
  4,318,784 B  sorted             0.641
  4,231,292 B  sorted + front     0.628

`polypress/fast.py` already does this -- `uniq = sorted(set(cells))`,
`_front_code`, `_choose_front` -- and the row-major fast tier never inherited
it. If the claim holds end to end, the fast tier is leaving ~20% on the table
for a one-line change plus a front coder.

The catch, and the reason this needs measuring rather than assuming: sorting
the alphabet changes the IDS. Appearance-order ids are Zipf-friendly -- the
value you meet first is usually the common one, so it gets a short id and the
body compresses well. Rank ids scatter frequent values across the id space.
The alphabet gets smaller and the body may get bigger. Only the total counts.

Variants, all decoded and compared cell by cell:

  B    out-of-band alphabet, appearance ids     (TEST-8's B, the baseline)
  Bs   out-of-band alphabet, SORTED, rank ids
  Bsf  Bs + front coding
  C    inline dictionary                        (TEST-8's C, for reference --
                                                 it cannot sort, the order is
                                                 the order values arrive)
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "TEST-8"))
import inlinedict as I

DICT, NUMV, TEXT = I.DICT, I.NUMV, I.TEXT


# --------------------------------------------------------------------------
# front coding: each string against its predecessor, longest common prefix
# --------------------------------------------------------------------------

def front_code(strs: List[str]) -> bytes:
    heads, body = bytearray(), bytearray()
    I._varint(len(strs), heads)
    prev = b""
    for s in strs:
        b = s.encode("utf-8")
        n, lim = 0, min(len(b), len(prev))
        while n < lim and b[n] == prev[n]:
            n += 1
        I._varint(n, heads)
        I._varint(len(b) - n, heads)
        body += b[n:]
        prev = b
    return bytes(heads) + bytes(body)


def un_front(raw: bytes, at: int = 0) -> Tuple[List[str], int]:
    n, at = I._read_varint(raw, at)
    pairs = []
    for _ in range(n):
        lcp, at = I._read_varint(raw, at)
        tail, at = I._read_varint(raw, at)
        pairs.append((lcp, tail))
    out, prev = [], b""
    for lcp, tail in pairs:
        b = prev[:lcp] + raw[at:at + tail]
        at += tail
        out.append(b.decode("utf-8"))
        prev = b
    return out, at


# --------------------------------------------------------------------------

def _kinds_and_maps(rows, ncol, sort_alpha: bool):
    kinds = I.analyse(rows, ncol)
    alphabets: List[List[str]] = []
    maps: List[Dict[str, int]] = []
    for j in range(ncol):
        if kinds[j] != DICT:
            alphabets.append([])
            maps.append({})
            continue
        if sort_alpha:
            a = sorted({r[j] for r in rows})
        else:
            a, seen = [], set()
            for r in rows:
                v = r[j]
                if v not in seen:
                    seen.add(v)
                    a.append(v)
        alphabets.append(a)
        maps.append({v: i for i, v in enumerate(a)})
    return kinds, alphabets, maps


def _body(rows, ncol, kinds, maps) -> bytes:
    out = []
    for r in rows:
        toks = []
        for j in range(ncol):
            k = kinds[j]
            if k == NUMV:
                toks.append(r[j])
            elif k == TEXT:
                toks.append(I.esc(r[j], False))
            else:
                toks.append(str(maps[j][r[j]]))
        out.append(I.FS.join(toks))
    return I.RS.join(out).encode("utf-8")


def _header(ncol, nrow, kinds, alphabets, front: bool) -> bytes:
    h = bytearray()
    I._varint(ncol, h)
    I._varint(nrow, h)
    for k in kinds:
        h.append(k)
    h.append(1 if front else 0)
    parts = [bytes(h)] + [(front_code(a) if front else I.pack_strings(a))
                          for a in alphabets]
    return I.xz(I._container(parts))


def _unheader(blob: bytes):
    parts = I._uncontainer(I.unxz(blob))
    ncol, at = I._read_varint(parts[0], 0)
    nrow, at = I._read_varint(parts[0], at)
    kinds = [parts[0][at + j] for j in range(ncol)]
    front = bool(parts[0][at + ncol])
    alphabets = [(un_front(parts[1 + j])[0] if front
                  else I.unpack_strings(parts[1 + j])[0])
                 for j in range(ncol)]
    return ncol, nrow, kinds, alphabets


def encode(rows: List[List[str]], sort_alpha: bool, front: bool) -> bytes:
    ncol = len(rows[0])
    kinds, alphabets, maps = _kinds_and_maps(rows, ncol, sort_alpha)
    body = _body(rows, ncol, kinds, maps)
    return I._container([_header(ncol, len(rows), kinds, alphabets, front),
                         I.xz(body)])


def decode(blob: bytes) -> List[List[str]]:
    p = I._uncontainer(blob)
    ncol, nrow, kinds, alphabets = _unheader(p[0])
    out = []
    for toks in I.split_body(I.unxz(p[1]).decode("utf-8")):
        row = []
        for j in range(ncol):
            t = toks[j]
            k = kinds[j]
            if k == NUMV:
                row.append(t)
            elif k == TEXT:
                row.append(I.unesc(t))
            else:
                row.append(alphabets[j][int(t)])
        out.append(row)
    return out


def encode_B(rows):   return encode(rows, False, False)
def encode_Bs(rows):  return encode(rows, True, False)
def encode_Bsf(rows): return encode(rows, True, True)


# --------------------------------------------------------------------------
# the size probe the claim came from, so the two can be compared directly:
# the alphabet block ALONE, three ways
# --------------------------------------------------------------------------

def alphabet_only(rows) -> Dict[str, int]:
    ncol = len(rows[0])
    kinds = I.analyse(rows, ncol)
    app, srt = [], []
    for j in range(ncol):
        if kinds[j] != DICT:
            continue
        a, seen = [], set()
        for r in rows:
            v = r[j]
            if v not in seen:
                seen.add(v)
                a.append(v)
        app.append(a)
        srt.append(sorted(a))
    return {
        "appearance": len(I.xz(b"".join(I.pack_strings(a) for a in app))),
        "sorted": len(I.xz(b"".join(I.pack_strings(a) for a in srt))),
        "sorted+front": len(I.xz(b"".join(front_code(a) for a in srt))),
    }
