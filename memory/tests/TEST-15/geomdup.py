#!/usr/bin/env python3
"""TEST-15 -- cross-column redundancy, built rather than bounded.

`benchmarks/probe_cross_column.py` measured the CEILING in 2026-08-01: 40 of
100 unselected tables carry a derivable column, 9 clear 10%, best 47.35%. It
did that by encoding twice, whole and with the redundant part stripped. It did
NOT build a predictor, so nothing shipped.

LAW 3 says this is where the work belongs: on the seven tables where row
reordering does not fire, a table-blind context model ties us. Cross-column
redundancy is the one structure a general compressor cannot see at all --
`location = "POINT (-87.62 41.89)"` next to `longitude = -87.62` and
`latitude = 41.89` is invisible to LZ77 at any window size, because the two
copies are at different PRECISIONS and share no long byte run.

This builds the narrow version the repo's own note recommends -- "geometry
republished as text" and "concatenated keys" -- with a real decoder.

THE TRANSFORM

A cell is parsed into a TEMPLATE and its NUMERIC TOKENS:

    "POINT (-87.675844598018 41.891)"
     -> template "POINT (\\0 \\0)", numbers ["-87.675844598018", "41.891"]

If every row of a column yields the same template and the same slot count, the
column is a candidate. For each slot the encoder looks for a source column
whose value, ROW BY ROW, reproduces the slot string under one of two rules:

    EXACT     source string is the slot string
    TRUNC(n)  slot string is the source string cut to n characters

TRUNC is the rule that matters and the reason a substring detector finds
nothing: the repo already measured that switching from substring to numeric
token matching took `chicago_permits` from 0.49% to 5.47%. Real duplication is
at a different precision, not a different position.

Rows that break the rule are stored as exceptions, by row index and literal,
so one bad cell costs one cell -- not the column. That is the all-or-nothing
lesson from CLAUDE.md applied to a new gate.

The column is then dropped from the table. The decoder rebuilds it from the
recipe and the source columns, which it has already decoded.

INVARIANT 2 applies and is enforced by the caller: the stripped table plus the
recipe must actually compress smaller than the whole table, measured, or the
transform is refused. The repo's own probe found 6 of 40 tables get WORSE.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

NUMTOK = re.compile(r"-?\d+(?:\.\d+)?")
SLOT = "\x00"

EXACT, TRUNC = 0, 1


def parse_cell(s: str) -> Tuple[str, List[str]]:
    """-> (template with numeric runs replaced by SLOT, the numeric runs)."""
    nums = NUMTOK.findall(s)
    if not nums:
        return s, []
    return NUMTOK.sub(SLOT, s), nums


def fill(template: str, nums: List[str]) -> str:
    out, k = [], 0
    for ch in template:
        if ch == SLOT:
            out.append(nums[k])
            k += 1
        else:
            out.append(ch)
    return "".join(out)


def _rule_for(slot_vals: List[str], src_vals: List[str]) -> Optional[Tuple]:
    """The cheapest rule reproducing slot_vals from src_vals, or None.

    Tried in order of how much they save. A rule is accepted with up to 10%
    exceptions; beyond that the column pairing is noise."""
    n = len(slot_vals)
    if not n:
        return None
    bad = sum(1 for a, b in zip(slot_vals, src_vals) if a != b)
    if bad <= n // 10:
        return (EXACT, 0, bad)
    # TRUNC: the slot is a prefix of the source. Pick the length that fits the
    # most rows, then charge the rest as exceptions.
    lens: Dict[int, int] = {}
    for a, b in zip(slot_vals, src_vals):
        if b.startswith(a) and len(a) < len(b):
            lens[len(a)] = lens.get(len(a), 0) + 1
    if lens:
        best_len = max(lens, key=lambda k: lens[k])
        hit = sum(1 for a, b in zip(slot_vals, src_vals)
                  if b[:best_len] == a)
        if n - hit <= n // 10:
            return (TRUNC, best_len, n - hit)
    return None


def detect(rows: List[List[str]], ncol: int, cover: float = 0.9
           ) -> List[dict]:
    """Every column derivable from other columns, as a list of recipes.

    The template is the MODAL template, not the universal one, and rows that do
    not match it are stored as whole-cell exceptions. The first version of this
    required every row to share a template and found NOTHING on the whole
    corpus -- `seattle_fire911.report_location` is
    `POINT (-122.272834 47.52331)` on 39,922 rows of 40,000 and blank on 78,
    and those 78 blanks cost the entire column. That is the all-or-nothing gate
    from CLAUDE.md, walked into while quoting it."""
    if not rows:
        return []
    nrow = len(rows)
    recipes = []
    for j in range(ncol):
        counts: Dict[str, int] = {}
        tpls = []
        for r in rows:
            t, ns = parse_cell(r[j])
            tpls.append((t, ns))
            counts[t] = counts.get(t, 0) + 1
        tpl = max(counts, key=lambda k: counts[k])
        if counts[tpl] < cover * nrow:
            continue
        nslot = tpl.count(SLOT)
        if nslot == 0 or tpl == SLOT:
            # no template around the numbers means the cell IS a number:
            # a numeric column, not a republished one
            continue

        idx = [i for i, (t, ns) in enumerate(tpls)
               if t == tpl and len(ns) == nslot]
        if len(idx) < cover * nrow:
            continue
        cellexc = [(i, rows[i][j]) for i in range(nrow) if i not in set(idx)]             if len(idx) != nrow else []
        slots = [[tpls[i][1][k] for i in idx] for k in range(nslot)]

        rules = []
        for k in range(nslot):
            found = None
            for c in range(ncol):
                if c == j:
                    continue
                rule = _rule_for(slots[k], [rows[i][c] for i in idx])
                if rule and (found is None or rule[2] < found[1][2]):
                    found = (c, rule)
                    if rule[2] == 0:
                        break
            rules.append(found)
        # EVERY slot must have a source. A partial recipe -- some slots
        # derived, the rest not -- is just the column again with extra steps.
        if not all(rules):
            continue

        exc = []
        for k, (c, (kind, arg, _)) in enumerate(rules):
            e = []
            for pos, i in enumerate(idx):
                want = slots[k][pos]
                got = rows[i][c] if kind == EXACT else rows[i][c][:arg]
                if got != want:
                    e.append((i, want))
            exc.append(e)
        recipes.append(dict(col=j, template=tpl, rules=rules, exc=exc,
                            cellexc=cellexc, nslot=nslot))
    return _resolve(recipes)


def _resolve(recipes: List[dict]) -> List[dict]:
    """Drop any recipe whose source is itself a dropped column.

    Found by real data on the first corpus run, as a TypeError: two columns can
    each be derivable from the other, so both get dropped and neither can be
    rebuilt. The `turbo` branch hit the same shape and fixed it by decoding
    derived columns in dependency ORDER (commit 12fb00d); that is the stronger
    fix and needs cycle detection. This takes the cheaper rule -- a source must
    survive -- and iterates to a fixed point, because dropping one recipe can
    invalidate another."""
    while True:
        dropped = {r["col"] for r in recipes}
        keep = [r for r in recipes
                if all(f[0] not in dropped for f in r["rules"])]
        if len(keep) == len(recipes):
            return keep
        recipes = keep


def strip(rows: List[List[str]], recipes: List[dict]
          ) -> Tuple[List[List[str]], List[int]]:
    """The table without the derivable columns."""
    drop = {r["col"] for r in recipes}
    keep = [j for j in range(len(rows[0])) if j not in drop]
    return [[r[j] for j in keep] for r in rows], keep


def rebuild(stripped: List[List[str]], keep: List[int], ncol: int,
            recipes: List[dict]) -> List[List[str]]:
    """Put the derivable columns back. `_resolve` guarantees every source is a
    surviving column, so one pass suffices and no ordering is needed."""
    nrow = len(stripped)
    full = [[None] * ncol for _ in range(nrow)]
    for i, row in enumerate(stripped):
        for pos, j in enumerate(keep):
            full[i][j] = row[pos]
    for rec in recipes:
        j = rec["col"]
        emap = [dict(e) for e in rec["exc"]]
        cmap = dict(rec.get("cellexc", []))
        for i in range(nrow):
            if i in cmap:
                full[i][j] = cmap[i]
                continue
            nums = []
            for k, found in enumerate(rec["rules"]):
                c, (kind, arg, _) = found
                if i in emap[k]:
                    nums.append(emap[k][i])
                else:
                    src = full[i][c]
                    nums.append(src if kind == EXACT else src[:arg])
            full[i][j] = fill(rec["template"], nums)
    return full


def recipe_bytes(recipes: List[dict]) -> bytes:
    """A deliberately plain serialisation -- this is measured, not shipped, so
    it must not flatter itself. Exceptions dominate when a rule is weak."""
    out = bytearray()
    out += str(len(recipes)).encode() + b"\n"
    for r in recipes:
        out += f"{r['col']}\t{r['template']!r}\t{r['nslot']}\n".encode()
        for found in r["rules"]:
            out += (b"none\n" if found is None else
                    f"{found[0]}\t{found[1][0]}\t{found[1][1]}\n".encode())
        for e in r["exc"]:
            out += str(len(e)).encode() + b"\n"
            for i, v in e:
                out += f"{i}\t{v}\n".encode()
        ce = r.get("cellexc", [])
        out += str(len(ce)).encode() + b"\n"
        for i, v in ce:
            out += f"{i}\t{v}\n".encode()
    return bytes(out)
