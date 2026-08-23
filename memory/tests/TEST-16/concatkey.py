#!/usr/bin/env python3
"""TEST-16 -- concatenated keys, the second pattern the ceiling probe named.

`probe_cross_column.py` measured two narrow patterns doing nearly all of the
cross-column win: **geometry republished as text** (TEST-15, built, 19.3%
through the shipping codec) and **concatenated keys** -- `row_id` = four
columns pasted together, measured at **47.35%**, the single best result in that
probe. This builds the second one.

THE PATTERN

    incident_key   = "2026-07-30|SEATTLE|F260106656"
    date           = "2026-07-30"
    city           = "SEATTLE"
    incident_number= "F260106656"

The cell is a concatenation of other columns' values with fixed literal glue.
Unlike TEST-15's geometry case the pieces are whole cell values, not numeric
tokens at reduced precision, so the detector is different: it segments the cell
left to right into COLUMN and LITERAL parts.

The recipe is derived from ONE row and then verified against every row, which
is the cheap direction -- a wrong segmentation fails verification immediately.
Rows that disagree are stored as whole-cell exceptions, up to a fraction, so
one odd row does not cost the column. That is the same all-or-nothing lesson
that TEST-15's first version had to learn the hard way.

Why it should pay where a general compressor cannot: the pieces are far apart
in the byte stream, at different offsets on every row, and Polypress compresses
its columns separately so the copy is never adjacent to its source.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

COLUMN, LITERAL = 0, 1
MIN_PIECE = 2       # a one-character "match" is noise, not a reference
MIN_PARTS = 2       # at least two column references, else it is not a key


def segment(cell: str, srcs: List[str], self_col: int) -> Optional[List[Tuple]]:
    """Segment one cell into COLUMN / LITERAL parts, greedily, longest first.

    Greedy is the right trade here: it is O(len * ncol) and a wrong split is
    caught by verification against the other rows rather than by search."""
    parts: List[Tuple] = []
    lit: List[str] = []
    pos, n = 0, len(cell)
    ncol_used = 0
    while pos < n:
        best_c, best_len = -1, 0
        for c, v in enumerate(srcs):
            if c == self_col or len(v) < MIN_PIECE or len(v) <= best_len:
                continue
            if cell.startswith(v, pos):
                best_c, best_len = c, len(v)
        if best_c >= 0:
            if lit:
                parts.append((LITERAL, "".join(lit)))
                lit = []
            parts.append((COLUMN, best_c))
            ncol_used += 1
            pos += best_len
        else:
            lit.append(cell[pos])
            pos += 1
    if lit:
        parts.append((LITERAL, "".join(lit)))
    if ncol_used < MIN_PARTS:
        return None
    return parts


def apply(parts: List[Tuple], row: List[str]) -> str:
    out = []
    for kind, v in parts:
        out.append(row[v] if kind == COLUMN else v)
    return "".join(out)


def detect(rows: List[List[str]], ncol: int, cover: float = 0.9
           ) -> List[dict]:
    """Columns that are a concatenation of other columns plus fixed glue."""
    if not rows:
        return []
    nrow = len(rows)
    out = []
    for j in range(ncol):
        # derive the recipe from the first row with a non-trivial value, and
        # from a second one too -- a single row can be segmented by accident
        cand = None
        for seed in range(min(8, nrow)):
            if len(rows[seed][j]) < MIN_PIECE * MIN_PARTS:
                continue
            p = segment(rows[seed][j], rows[seed], j)
            if p is None:
                continue
            hit = sum(1 for r in rows if apply(p, r) == r[j])
            if hit >= cover * nrow:
                cand = (p, hit)
                break
        if cand is None:
            continue
        parts, hit = cand
        exc = [(i, rows[i][j]) for i in range(nrow)
               if apply(parts, rows[i]) != rows[i][j]]
        out.append(dict(col=j, parts=parts, exc=exc))
    return _resolve(out)


def _resolve(recipes: List[dict]) -> List[dict]:
    """A source must be a surviving column. Same fixed-point rule as TEST-15 --
    two columns can be concatenations of each other's pieces."""
    while True:
        dropped = {r["col"] for r in recipes}
        keep = [r for r in recipes
                if all(k != COLUMN or v not in dropped for k, v in r["parts"])]
        if len(keep) == len(recipes):
            return keep
        recipes = keep


def strip(rows: List[List[str]], recipes: List[dict]):
    drop = {r["col"] for r in recipes}
    keep = [j for j in range(len(rows[0])) if j not in drop]
    return [[r[j] for j in keep] for r in rows], keep


def rebuild(stripped: List[List[str]], keep: List[int], ncol: int,
            recipes: List[dict]) -> List[List[str]]:
    nrow = len(stripped)
    full = [[None] * ncol for _ in range(nrow)]
    for i, row in enumerate(stripped):
        for pos, j in enumerate(keep):
            full[i][j] = row[pos]
    for rec in recipes:
        j = rec["col"]
        emap = dict(rec["exc"])
        for i in range(nrow):
            full[i][j] = emap[i] if i in emap else apply(rec["parts"], full[i])
    return full


def recipe_bytes(recipes: List[dict]) -> bytes:
    out = bytearray()
    out += str(len(recipes)).encode() + b"\n"
    for r in recipes:
        out += f"{r['col']}\t{len(r['parts'])}\n".encode()
        for kind, v in r["parts"]:
            out += (f"c{v}\n" if kind == COLUMN else f"l{v}\n").encode()
        out += str(len(r["exc"])).encode() + b"\n"
        for i, v in r["exc"]:
            out += f"{i}\t{v}\n".encode()
    return bytes(out)
