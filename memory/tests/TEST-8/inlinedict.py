#!/usr/bin/env python3
"""TEST-8 -- store the dictionary IN the table.

Mahdi's proposal (THINKING-LOG Thought 11): the dictionary need not be a block
at the front of the file. Walk the table left to right; the FIRST time a value
is seen, emit the value itself; every later time, emit its id. The decoder
rebuilds the table and the dictionary in the same single left-to-right pass,
because by the time an id arrives the value it names has already gone past.

Six variants, all sharing one framing so only the intended thing differs:

  A  xz -9e on the canonical CSV                      (external baseline)
  B  OUT-OF-BAND dictionary, row-major                (TEST-5/7's winner)
  C  INLINE dictionary, row-major                     (the proposal)
  D  INLINE, single pass, no gate, kind from cell 1   (the pure form)
  E  INLINE, single pass, self-synchronising gate     (D + causal 50% rule)
  F  INLINE, column-major                             (layout control on C)

B and C are deliberately identical in every respect except WHERE the distinct
strings live -- same column kinds, same 50% gate, same ids, same separators.
That pair alone answers the question. D and E then ask whether the analysis
pass can be dropped altogether, which is the part of the idea that buys speed
rather than bytes.

Framing, shared by B-F so that no variant wins on separator luck:

  \\x02  field separator
  \\n    row separator
  \\x01  escape
  \\x03  literal marker

A literal is emitted with \\x01, \\x02, \\n and \\x03 each escaped by a leading
\\x01. If the escaped form would read as an id (all ASCII digits) it carries a
leading \\x03. So, inside a dictionary column:

  starts with \\x03   -> a literal that looks like an id; register it
  all digits         -> an id into what has been registered so far
  anything else      -> a literal, first occurrence, register it

The marker cannot be the escape byte itself: a leading \\x01 would pair with
the literal's first byte and eat it. That cost one round of the selftest.

Every variant is decoded and compared cell by cell before its bytes count.
"""
from __future__ import annotations

import lzma
import re
from typing import Dict, List, Optional, Tuple

XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2,
                    "preset": 9 | lzma.PRESET_EXTREME}])

NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
DIGITS = re.compile(r"^\d+$")

ESC = "\x01"
FS = "\x02"
RS = "\n"
LIT = "\x03"      # marks "this token is a literal, not an id"
SPECIAL = (ESC, FS, RS, LIT)

# column kinds
DICT, NUMV, TEXT = 0, 1, 2

GATE = 0.5      # dictionary-code only if distinct/rows <= GATE
MIN_ROWS = 64   # E: rows observed before the causal gate may fire


def xz(b: bytes) -> bytes:
    return lzma.compress(b, **XZ)


def unxz(b: bytes) -> bytes:
    return lzma.decompress(b, **XZ)


# --------------------------------------------------------------------------
# container / varint plumbing (same shape as TEST-5's, kept local)
# --------------------------------------------------------------------------

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
    out, at2 = [], at
    for ln in lens:
        out.append(blob[at2:at2 + ln])
        at2 += ln
    return out


# --------------------------------------------------------------------------
# literal escaping
# --------------------------------------------------------------------------

def esc(s: str, guard: bool) -> str:
    """Escape a literal for the body.

    ESC, the separators and LIT are escaped by a leading ESC, so an escaped
    literal can never start with a bare LIT. `guard` is set in dictionary
    columns: there a token of pure digits means an id, so a literal that looks
    like one is prefixed with LIT. The marker cannot be ESC -- an ESC at the
    front would pair with the first byte of the literal and eat it."""
    if any(c in s for c in SPECIAL):
        out = []
        for ch in s:
            if ch in SPECIAL:
                out.append(ESC)
            out.append(ch)
        s2 = "".join(out)
    else:
        s2 = s
    if guard and DIGITS.match(s2):
        return LIT + s2
    return s2


def unesc(s: str) -> str:
    if ESC not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == ESC and i + 1 < n:
            out.append(s[i + 1])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def split_body(body: str) -> List[List[str]]:
    """Split on unescaped FS / RS. One pass, no regex."""
    rows, cur, tok, i, n = [], [], [], 0, len(body)
    while i < n:
        ch = body[i]
        if ch == ESC and i + 1 < n:
            tok.append(ch)
            tok.append(body[i + 1])
            i += 2
            continue
        if ch == FS:
            cur.append("".join(tok))
            tok = []
        elif ch == RS:
            cur.append("".join(tok))
            rows.append(cur)
            cur, tok = [], []
        else:
            tok.append(ch)
        i += 1
    if tok or cur or not rows:
        cur.append("".join(tok))
        rows.append(cur)
    return rows


# --------------------------------------------------------------------------
# two-pass analysis, shared by B and C so they cannot differ on it
# --------------------------------------------------------------------------

def analyse(rows: List[List[str]], ncol: int) -> List[int]:
    nrow = len(rows)
    kinds = []
    for j in range(ncol):
        col = [r[j] for r in rows]
        if all(NUM.match(v) for v in col):
            kinds.append(NUMV)
            continue
        kinds.append(DICT if len(set(col)) <= GATE * nrow else TEXT)
    return kinds


def _pack_header(ncol: int, nrow: int, kinds: List[int],
                 alphabets: Optional[List[List[str]]]) -> bytes:
    h = bytearray()
    _varint(ncol, h)
    _varint(nrow, h)
    for k in kinds:
        h.append(k)
    parts = [bytes(h)]
    if alphabets is not None:
        parts += [pack_strings(a) for a in alphabets]
    return xz(_container(parts))


def _unpack_header(blob: bytes, with_alphabets: bool):
    parts = _uncontainer(unxz(blob))
    ncol, at = _read_varint(parts[0], 0)
    nrow, at = _read_varint(parts[0], at)
    kinds = [parts[0][at + j] for j in range(ncol)]
    alphabets = ([unpack_strings(parts[1 + j])[0] for j in range(ncol)]
                 if with_alphabets else None)
    return ncol, nrow, kinds, alphabets


# --------------------------------------------------------------------------
# B -- OUT-OF-BAND dictionary, row-major.  The alphabet is a block in the
#      header; the body holds nothing but ids and numbers.
# --------------------------------------------------------------------------

def encode_B(rows: List[List[str]]) -> bytes:
    ncol = len(rows[0])
    kinds = analyse(rows, ncol)
    maps: List[Dict[str, int]] = [{} for _ in range(ncol)]
    out = []
    for r in rows:
        toks = []
        for j in range(ncol):
            v = r[j]
            k = kinds[j]
            if k == NUMV:
                toks.append(v)
            elif k == TEXT:
                toks.append(esc(v, False))
            else:
                m = maps[j]
                i = m.get(v)
                if i is None:
                    i = m[v] = len(m)
                toks.append(str(i))
        out.append(FS.join(toks))
    alphabets = []
    for j in range(ncol):
        a = [""] * len(maps[j])
        for v, i in maps[j].items():
            a[i] = v
        alphabets.append(a)
    body = RS.join(out).encode("utf-8")
    return _container([_pack_header(ncol, len(rows), kinds, alphabets), xz(body)])


def decode_B(blob: bytes) -> List[List[str]]:
    p = _uncontainer(blob)
    ncol, nrow, kinds, alphabets = _unpack_header(p[0], True)
    out = []
    for toks in split_body(unxz(p[1]).decode("utf-8")):
        row = []
        for j in range(ncol):
            t = toks[j]
            k = kinds[j]
            if k == NUMV:
                row.append(t)
            elif k == TEXT:
                row.append(unesc(t))
            else:
                row.append(alphabets[j][int(t)])
        out.append(row)
    return out


# --------------------------------------------------------------------------
# C -- INLINE dictionary, row-major.  Same kinds, same gate, same ids as B.
#      The ONLY difference is that the distinct strings ride in the body at
#      their first occurrence instead of in a block at the front.
# --------------------------------------------------------------------------

def _emit_inline(rows, ncol, kinds):
    seen: List[Dict[str, int]] = [{} for _ in range(ncol)]
    grid = []
    for r in rows:
        toks = []
        for j in range(ncol):
            v = r[j]
            k = kinds[j]
            if k == NUMV:
                toks.append(v)
            elif k == TEXT:
                toks.append(esc(v, False))
            else:
                m = seen[j]
                i = m.get(v)
                if i is None:
                    m[v] = len(m)
                    toks.append(esc(v, True))
                else:
                    toks.append(str(i))
        grid.append(toks)
    return grid


def encode_C(rows: List[List[str]]) -> bytes:
    ncol = len(rows[0])
    kinds = analyse(rows, ncol)
    grid = _emit_inline(rows, ncol, kinds)
    body = RS.join(FS.join(t) for t in grid).encode("utf-8")
    return _container([_pack_header(ncol, len(rows), kinds, None), xz(body)])


def _absorb(t: str, alpha: List[str]) -> str:
    """One inline dictionary cell -> its value, growing `alpha` as it goes.

    A leading LIT means the literal would otherwise have read as an id; it is
    stripped before unescaping. Any other non-digit token is a first
    occurrence too."""
    if t[:1] == LIT:
        v = unesc(t[1:])
    elif DIGITS.match(t):
        return alpha[int(t)]
    else:
        v = unesc(t)
    alpha.append(v)
    return v


def decode_C(blob: bytes) -> List[List[str]]:
    p = _uncontainer(blob)
    ncol, nrow, kinds, _ = _unpack_header(p[0], False)
    alpha: List[List[str]] = [[] for _ in range(ncol)]
    out = []
    for toks in split_body(unxz(p[1]).decode("utf-8")):
        row = []
        for j in range(ncol):
            t = toks[j]
            k = kinds[j]
            if k == NUMV:
                row.append(t)
            elif k == TEXT:
                row.append(unesc(t))
            else:
                row.append(_absorb(t, alpha[j]))
        out.append(row)
    return out


# --------------------------------------------------------------------------
# F -- INLINE, column-major.  Identical to C except the grid is transposed
#      before it is serialised, so ids are assigned down a column instead of
#      across a row.
# --------------------------------------------------------------------------

def encode_F(rows: List[List[str]]) -> bytes:
    ncol = len(rows[0])
    nrow = len(rows)
    kinds = analyse(rows, ncol)
    seen: List[Dict[str, int]] = [{} for _ in range(ncol)]
    cols = []
    for j in range(ncol):
        k = kinds[j]
        toks = []
        for i in range(nrow):
            v = rows[i][j]
            if k == NUMV:
                toks.append(v)
            elif k == TEXT:
                toks.append(esc(v, False))
            else:
                m = seen[j]
                x = m.get(v)
                if x is None:
                    m[v] = len(m)
                    toks.append(esc(v, True))
                else:
                    toks.append(str(x))
        cols.append(FS.join(toks))
    body = RS.join(cols).encode("utf-8")
    return _container([_pack_header(ncol, nrow, kinds, None), xz(body)])


def colmajor_parts(rows: List[List[str]]):
    """(header, body) for variant F before compression."""
    ncol = len(rows[0])
    kinds = analyse(rows, ncol)
    seen: List[Dict[str, int]] = [{} for _ in range(ncol)]
    cols = []
    for j in range(ncol):
        k = kinds[j]
        toks = []
        for i in range(len(rows)):
            v = rows[i][j]
            if k == NUMV:
                toks.append(v)
            elif k == TEXT:
                toks.append(esc(v, False))
            else:
                m = seen[j]
                x = m.get(v)
                if x is None:
                    m[v] = len(m)
                    toks.append(esc(v, True))
                else:
                    toks.append(str(x))
        cols.append(FS.join(toks))
    h = bytearray()
    _varint(ncol, h)
    _varint(len(rows), h)
    for k in kinds:
        h.append(k)
    return bytes(h), RS.join(cols).encode("utf-8")


def decode_F(blob: bytes) -> List[List[str]]:
    p = _uncontainer(blob)
    ncol, nrow, kinds, _ = _unpack_header(p[0], False)
    lines = split_body(unxz(p[1]).decode("utf-8"))
    out = [[None] * ncol for _ in range(nrow)]
    for j in range(ncol):
        toks = lines[j]
        k = kinds[j]
        alpha: List[str] = []
        for i in range(nrow):
            t = toks[i]
            if k == NUMV:
                out[i][j] = t
            elif k == TEXT:
                out[i][j] = unesc(t)
            else:
                out[i][j] = _absorb(t, alpha)
    return out


# --------------------------------------------------------------------------
# D / E -- SINGLE PASS.  No analysis pass at all: the column's kind is decided
#      from evidence the decoder also has.
#
#      D: kind is NUMV iff the column's FIRST cell parses as a number.  A
#         later non-numeric cell in a NUMV column is an exception, marked by a
#         leading ESC and stored raw (no dictionary), exactly as fast.py does.
#      E: D plus a self-synchronising gate -- after MIN_ROWS rows, a DICT
#         column whose distinct/seen ratio exceeds GATE switches permanently
#         to TEXT.  Both sides evaluate the rule at the top of each row using
#         only rows already emitted/decoded, so they always agree.
# --------------------------------------------------------------------------

def stream_parts(rows: List[List[str]], adaptive: bool = True):
    """(header, body) for D/E BEFORE any compression, so a different backend
    can be put behind the same transform. TEST-9/10 use this."""
    hdr, body = _stream_raw(rows, adaptive)
    return hdr, body


def _stream_raw(rows: List[List[str]], adaptive: bool):
    ncol = len(rows[0])
    kind: List[Optional[int]] = [None] * ncol
    seen: List[Dict[str, int]] = [{} for _ in range(ncol)]
    cnt = [0] * ncol
    grid = []
    for r in rows:
        if adaptive:
            for j in range(ncol):
                if (kind[j] == DICT and cnt[j] >= MIN_ROWS
                        and len(seen[j]) > GATE * cnt[j]):
                    kind[j] = TEXT
        toks = []
        for j in range(ncol):
            v = r[j]
            if kind[j] is None:
                kind[j] = NUMV if NUM.match(v) else DICT
            k = kind[j]
            if k == NUMV:
                toks.append(v if NUM.match(v) else LIT + esc(v, False))
            elif k == TEXT:
                toks.append(esc(v, False))
            else:
                m = seen[j]
                i = m.get(v)
                if i is None:
                    m[v] = len(m)
                    toks.append(esc(v, True))
                else:
                    toks.append(str(i))
                cnt[j] += 1
        grid.append(FS.join(toks))
    body = RS.join(grid).encode("utf-8")
    h = bytearray()
    _varint(ncol, h)
    _varint(len(rows), h)
    return bytes(h), body


def _encode_stream(rows: List[List[str]], adaptive: bool) -> bytes:
    h, body = _stream_raw(rows, adaptive)
    return _container([xz(h), xz(body)])


def _decode_stream(blob: bytes, adaptive: bool) -> List[List[str]]:
    p = _uncontainer(blob)
    return decode_stream_parts(unxz(p[0]), unxz(p[1]), adaptive)


def decode_stream_parts(hb: bytes, body: bytes,
                        adaptive: bool = True) -> List[List[str]]:
    """Decode D/E from the UNCOMPRESSED header and body, so a different
    backend can sit in front of the same transform. TEST-10 uses this."""
    ncol, at = _read_varint(hb, 0)
    nrow, at = _read_varint(hb, at)
    kind: List[Optional[int]] = [None] * ncol
    alpha: List[List[str]] = [[] for _ in range(ncol)]
    idx: List[Dict[str, int]] = [{} for _ in range(ncol)]
    cnt = [0] * ncol
    out = []
    for toks in split_body(body.decode("utf-8")):
        if adaptive:
            for j in range(ncol):
                if (kind[j] == DICT and cnt[j] >= MIN_ROWS
                        and len(alpha[j]) > GATE * cnt[j]):
                    kind[j] = TEXT
        row = []
        for j in range(ncol):
            t = toks[j]
            if kind[j] is None:
                # the first cell was written verbatim if numeric, else as a
                # guarded literal -- both tell the decoder the same thing
                kind[j] = NUMV if (t[:1] != LIT and NUM.match(t)) else DICT
            k = kind[j]
            if k == NUMV:
                row.append(unesc(t[1:]) if t[:1] == LIT else t)
            elif k == TEXT:
                row.append(unesc(t))
            else:
                row.append(_absorb(t, alpha[j]))
                cnt[j] += 1
        out.append(row)
    return out


def decode_colmajor_parts(hb: bytes, body: bytes) -> List[List[str]]:
    """Decode variant F from the uncompressed header and body."""
    ncol, at = _read_varint(hb, 0)
    nrow, at = _read_varint(hb, at)
    kinds = [hb[at + j] for j in range(ncol)]
    lines = split_body(body.decode("utf-8"))
    out = [[None] * ncol for _ in range(nrow)]
    for j in range(ncol):
        toks = lines[j]
        k = kinds[j]
        alpha: List[str] = []
        for i in range(nrow):
            t = toks[i]
            if k == NUMV:
                out[i][j] = t
            elif k == TEXT:
                out[i][j] = unesc(t)
            else:
                out[i][j] = _absorb(t, alpha)
    return out


def encode_D(rows): return _encode_stream(rows, False)
def decode_D(blob): return _decode_stream(blob, False)
def encode_E(rows): return _encode_stream(rows, True)
def decode_E(blob): return _decode_stream(blob, True)
