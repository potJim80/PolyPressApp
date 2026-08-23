#!/usr/bin/env python3
"""TEST-19 -- structured text columns: template + per-slot integer streams.

The float/timestamp research pass measured, on isolated columns through xz:

    usgs `time`          64,955 -> 48,764   -24.9%
    austin `rep_date_time`  63,389 -> 47,413   -25.1%
    austin `occ_date_time`  83,543 -> 73,890   -11.7%

and noted that `fast.classify` puts every one of these columns on the `text`
path -- **the codec does nothing with them today except hand them to xz.**

It also measured that `updated` and `occ_date` get WORSE under the same
treatment (8-58%), so the gate has to be per column and measured.

Every previous transform this session gained less through Polypress than
through xz -- TEST-17's functional dependencies went from +12.7% (xz) to -4.8%
(polypress) because the row reorder already had them. So this is measured
through `fast.encode`, not through xz.

THE TRANSFORM, deliberately more general than a datetime parser

A cell is split into its modal TEMPLATE and its numeric tokens (TEST-15's
`parse_cell`):

    "2026-07-30T18:16:00.000"
      -> "\0-\0-\0T\0:\0:\0.\0" + [2026, 07, 30, 18, 16, 00, 000]

Each slot becomes its own integer stream. Per slot, and MEASURED per slot:

    RAW    varint of the value
    DELTA  zigzag varint of the difference from the previous row

Leading zeros are kept by storing the digit width when it is constant down the
slot (it always is, for a datetime), and falling back to raw strings when it is
not. No format detection, no strptime, no locale -- which means it also fires
on `"108 km SE of Kuril'sk"` and on ids like `F260106656`.

Rows not matching the modal template are whole-cell exceptions, as in TEST-15,
because that gate is the one that took that test from 0 tables to 3.
"""
from __future__ import annotations

import lzma
import re
from typing import Dict, List, Optional, Tuple

XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])
NUMTOK = re.compile(r"\d+")
SLOT = "\x00"

RAW, DELTA, STR = 0, 1, 2


def xz(b: bytes) -> bytes:
    return lzma.compress(b, **XZ)


def unxz(b: bytes) -> bytes:
    return lzma.decompress(b, **XZ)


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


def zig(v: int) -> int:
    return (v << 1) if v >= 0 else ((-v) << 1) - 1


def unzig(u: int) -> int:
    return -((u + 1) >> 1) if u & 1 else u >> 1


def parse_cell(s: str) -> Tuple[str, List[str]]:
    toks = NUMTOK.findall(s)
    if not toks:
        return s, []
    return NUMTOK.sub(SLOT, s), toks


def fill(tpl: str, toks: List[str]) -> str:
    out, k = [], 0
    for ch in tpl:
        if ch == SLOT:
            out.append(toks[k])
            k += 1
        else:
            out.append(ch)
    return "".join(out)


def _pack_slot(vals: List[int], mode: int) -> bytes:
    out = bytearray()
    if mode == RAW:
        for v in vals:
            varint(v, out)
    else:
        prev = 0
        for v in vals:
            varint(zig(v - prev), out)
            prev = v
    return bytes(out)


def _unpack_slot(b: bytes, n: int, mode: int) -> List[int]:
    out, at, prev = [], 0, 0
    for _ in range(n):
        u, at = read_varint(b, at)
        if mode == RAW:
            out.append(u)
        else:
            prev = prev + unzig(u)
            out.append(prev)
    return out


def analyse(cells: List[str], cover: float = 0.9) -> Optional[dict]:
    """A recipe for this column, or None if it is not template-shaped."""
    n = len(cells)
    counts: Dict[str, int] = {}
    parsed = []
    for c in cells:
        t, toks = parse_cell(c)
        parsed.append((t, toks))
        counts[t] = counts.get(t, 0) + 1
    tpl = max(counts, key=lambda k: counts[k])
    nslot = tpl.count(SLOT)
    if nslot == 0 or counts[tpl] < cover * n:
        return None
    if tpl == SLOT:
        return None                     # a bare number: the numeric path's job
    idx = [i for i, (t, k) in enumerate(parsed) if t == tpl and len(k) == nslot]
    if len(idx) < cover * n:
        return None

    slots = []
    for k in range(nslot):
        toks = [parsed[i][1][k] for i in idx]
        widths = {len(t) for t in toks}
        if len(widths) == 1:
            w = widths.pop()
            slots.append(dict(kind="int", width=w,
                              vals=[int(t) for t in toks]))
        elif all(t == str(int(t)) for t in toks):
            slots.append(dict(kind="int", width=0,
                              vals=[int(t) for t in toks]))
        else:
            slots.append(dict(kind="str", width=-1, toks=toks))
    exc = [(i, cells[i]) for i in range(n) if i not in set(idx)]
    return dict(template=tpl, nslot=nslot, slots=slots, exc=exc, idx=idx, n=n)


def encode(cells: List[str]) -> Optional[bytes]:
    rec = analyse(cells)
    if rec is None:
        return None
    head = bytearray()
    varint(rec["n"], head)
    varint(len(rec["idx"]), head)
    tb = rec["template"].encode("utf-8")
    varint(len(tb), head)
    head += tb
    varint(rec["nslot"], head)
    parts: List[bytes] = []
    for s in rec["slots"]:
        if s["kind"] == "str":
            head.append(STR)
            varint(0, head)
            blob = bytearray()
            for t in s["toks"]:
                b = t.encode()
                varint(len(b), blob)
                blob += b
            parts.append(bytes(blob))
            continue
        # MEASURED per slot -- invariant 2 at slot granularity
        cand = [(len(xz(_pack_slot(s["vals"], m))), m) for m in (RAW, DELTA)]
        cand.sort()
        mode = cand[0][1]
        head.append(mode)
        varint(s["width"], head)
        parts.append(_pack_slot(s["vals"], mode))
    eb = bytearray()
    varint(len(rec["exc"]), eb)
    for i, v in rec["exc"]:
        varint(i, eb)
        b = v.encode("utf-8")
        varint(len(b), eb)
        eb += b
    body = bytearray()
    varint(len(parts), body)
    for p in parts:
        varint(len(p), body)
    for p in parts:
        body += p
    return xz(bytes(head) + bytes(eb) + bytes(body))


def decode(blob: bytes) -> List[str]:
    raw = unxz(blob)
    n, at = read_varint(raw, 0)
    nidx, at = read_varint(raw, at)
    tl, at = read_varint(raw, at)
    tpl = raw[at:at + tl].decode("utf-8")
    at += tl
    nslot, at = read_varint(raw, at)
    modes, widths = [], []
    for _ in range(nslot):
        modes.append(raw[at])
        at += 1
        w, at = read_varint(raw, at)
        widths.append(w)
    nexc, at = read_varint(raw, at)
    exc = []
    for _ in range(nexc):
        i, at = read_varint(raw, at)
        ln, at = read_varint(raw, at)
        exc.append((i, raw[at:at + ln].decode("utf-8")))
        at += ln
    npart, at = read_varint(raw, at)
    lens = []
    for _ in range(npart):
        v, at = read_varint(raw, at)
        lens.append(v)
    parts = []
    for ln in lens:
        parts.append(raw[at:at + ln])
        at += ln

    cols = []
    for k in range(nslot):
        if modes[k] == STR:
            toks, p, o = [], parts[k], 0
            for _ in range(nidx):
                ln, o = read_varint(p, o)
                toks.append(p[o:o + ln].decode())
                o += ln
            cols.append(toks)
        else:
            vals = _unpack_slot(parts[k], nidx, modes[k])
            w = widths[k]
            cols.append([str(v).zfill(w) if w else str(v) for v in vals])

    emap = dict(exc)
    out: List[str] = []
    pos = 0
    for i in range(n):
        if i in emap:
            out.append(emap[i])
        else:
            out.append(fill(tpl, [cols[k][pos] for k in range(nslot)]))
            pos += 1
    return out
