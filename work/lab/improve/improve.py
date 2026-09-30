"""Three improvements to the real codec, each measured end to end.

Mahdi, 2026-09-28: "it's also worth trying to improve polypress's current
algorithms." Each idea wraps or patches the shipping `fast.encode`, gets its
own decoder, and is round-trip checked. Every idea is also reported the way
the codec would ship it -- `min(base, idea)`, invariant 2 -- so a loss costs
one flag byte, never a bigger file.

    A  sort     measured global row sort. Rows sorted by every column, lowest
                cardinality first; the permutation is stored. From the
                reordering lab: the only new order that ever won (-10% vs the
                full codec on m_financial_shuffled).
    B  firstid  dictionary ids by FIRST APPEARANCE instead of sorted value.
                Moves both the id streams and the parent sorts' group order.
                sortreg (retired) measured +7.3% for this; never tried here.
    C  derive   numbers inside text cells that another column of the same row
                already holds -- geometry republished as text, `location =
                "POINT (lon lat)"`. Backlog item 2 in CLAUDE.md, measured as a
                15-21% ceiling on such tables, never built. Each derivable
                number becomes a reference `\\x01<col>:<decimals>\\x02` to the
                column it came from, so the whole column collapses to one
                repeated skeleton, which the dictionary path eats.

Lab code: nothing here changes fast.py or the C port (invariant 1). A winner
gets ported to both in one commit, after the numbers are seen.

    python3 lab/improve/improve.py ../IN/suite/X.csv ... [--json out]
"""

from __future__ import annotations

import json
import lzma
import os
import re
import sys
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(HERE, "..", "reorder"))
from polypress import dtz, fast  # noqa: E402
import reorder  # noqa: E402   (factorize, pack_perm, by_cardinality)

XZ = fast.XZ


def _box(tag: bytes, side: dict, inner: bytes, extra: bytes = b"") -> bytes:
    """tag + xz(json side info) + optional extra blob + the fast.encode blob."""
    sb = lzma.compress(json.dumps(side, separators=(",", ":")).encode(), **XZ)
    return (tag + len(sb).to_bytes(4, "big") + len(extra).to_bytes(4, "big")
            + sb + extra + inner)


def _unbox(blob: bytes):
    tag = blob[:1]
    sl = int.from_bytes(blob[1:5], "big")
    el = int.from_bytes(blob[5:9], "big")
    o = 9
    side = json.loads(lzma.decompress(blob[o:o + sl], **XZ))
    o += sl
    return tag, side, blob[o:o + el], blob[o + el:]


# ------------------------------------------------------------ A: sort

def enc_sort(table):
    n = len(table.rows)
    cols = [table.column(j) for j in range(len(table.columns))]
    if n < 2 or not cols:
        return None
    keys = reorder.by_cardinality(cols)
    perm = np.lexsort(tuple(reorder.factorize(cols[j]) for j in reversed(keys)))
    sorted_t = dtz.Table(table.columns, [table.rows[i] for i in perm])
    return _box(b"S", {"n": n}, fast.encode(sorted_t), reorder.pack_perm(perm))


def dec_sort(blob):
    _, side, extra, inner = _unbox(blob)
    t = fast.decode(inner)
    perm = reorder.unpack_perm(extra, side["n"])
    rows = [None] * side["n"]
    for k, i in enumerate(perm):
        rows[i] = t.rows[k]
    return dtz.Table(t.columns, rows)


# ------------------------------------------------------------ B: firstid

@contextmanager
def first_appearance_ids():
    """fast.classify with dictionary alphabets in first-appearance order.
    The decoder needs nothing: it looks ids up in whatever alphabet it is
    handed. Serial, so the patch cannot leak into another thread's encode."""
    real = fast.classify

    def classify(table, lenient=True):
        plan = real(table, lenient)
        for c in plan:
            if c["kind"] != "dict":
                continue
            ids = c["ids"]
            # order of first appearance of each sorted-id
            _, first = np.unique(ids, return_index=True)
            order = np.argsort(first, kind="stable")      # new -> old id
            remap = np.empty_like(order)
            remap[order] = np.arange(order.size)          # old -> new id
            c["ids"] = remap[ids]
            c["alpha"] = [c["alpha"][o] for o in order]
        return plan

    fast.classify = classify
    try:
        yield
    finally:
        fast.classify = real


def enc_firstid(table):
    with first_appearance_ids():
        return _box(b"F", {}, fast.encode(table, parallel=False))


def dec_firstid(blob):
    return fast.decode(_unbox(blob)[3])


# ------------------------------------------------------------ C: derive

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_REF = re.compile(r"\x01(\d+):(\d*)\x02")
SAMPLE = 500
MIN_HIT = 0.5           # a candidate column must explain this share of rows
MAX_CANDS = 4


def _round(v: str, d: int):
    try:
        q = Decimal(v).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError):
        return None
    return str(q)


def _decimals(tok: str) -> int:
    i = tok.find(".")
    return 0 if i < 0 else len(tok) - i - 1


def _ref_for(tok: str, row, cands):
    """Reference text for a token, or None. Exact copies first, then a
    rounding of the source; the first candidate column that reproduces the
    token wins, so encoder and decoder agree by construction."""
    for k, j in enumerate(cands):
        v = row[j]
        if v == tok:
            return "\x01{}:\x02".format(k)
    d = _decimals(tok)
    for k, j in enumerate(cands):
        v = row[j]
        if v and _decimals(v) > d and _round(v, d) == tok:
            return "\x01{}:{}\x02".format(k, d)
    return None


def _candidates(table, t: int, banned):
    rows = table.rows
    step = max(1, len(rows) // SAMPLE)
    hits, seen = {}, 0
    for row in rows[::step]:
        toks = _NUM.findall(row[t])
        if not toks:
            continue
        seen += 1
        used = set()
        for tok in toks:
            d = _decimals(tok)
            for j, v in enumerate(row):
                if j == t or j in banned or not v:
                    continue
                if v == tok or (_decimals(v) > d and _round(v, d) == tok):
                    used.add(j)
        for j in used:
            hits[j] = hits.get(j, 0) + 1
    if not seen:
        return []
    good = sorted(j for j, h in hits.items() if h >= MIN_HIT * seen)
    return good[:MAX_CANDS]


def enc_derive(table):
    ncol = len(table.columns)
    if not table.rows:
        return None
    derived, banned = {}, set()
    for t in range(ncol):
        if t in banned:               # already a source for another column
            continue
        col = table.column(t)
        if any("\x01" in c or "\x02" in c for c in col):
            continue
        cands = _candidates(table, t, banned | set(derived))
        if not cands:
            continue
        derived[t] = cands
        banned.update(cands)          # a source column is never derived
    if not derived:
        return None
    rows = [list(r) for r in table.rows]
    for t, cands in derived.items():
        for r in rows:
            cell = r[t]
            if not cell:
                continue

            def sub(m, r=r, cands=cands):
                ref = _ref_for(m.group(0), r, cands)
                return ref if ref is not None else m.group(0)
            r[t] = _NUM.sub(sub, cell)
    side = {"d": {str(t): c for t, c in derived.items()}}
    return _box(b"D", side, fast.encode(dtz.Table(table.columns, rows)))


def dec_derive(blob):
    _, side, _, inner = _unbox(blob)
    t = fast.decode(inner)
    rows = t.rows
    for ts, cands in side["d"].items():
        ti = int(ts)
        for r in rows:
            def put(m, r=r, cands=cands):
                v = r[cands[int(m.group(1))]]
                return v if m.group(2) == "" else _round(v, int(m.group(2)))
            r[ti] = _REF.sub(put, r[ti])
    return dtz.Table(t.columns, rows)


IDEAS = [("sort", enc_sort, dec_sort),
         ("firstid", enc_firstid, dec_firstid),
         ("derive", enc_derive, dec_derive)]


# ------------------------------------------------------------ driver

def measure(path: str) -> dict:
    table = dtz.read_any(path)
    res = {"name": os.path.basename(path), "bytes": os.path.getsize(path),
           "rows": len(table.rows), "cols": len(table.columns)}
    t = time.perf_counter()
    base = fast.encode(table)
    res["t_base"] = time.perf_counter() - t
    res["base"] = len(base)
    for name, enc, dec in IDEAS:
        t = time.perf_counter()
        blob = enc(table)
        res["t_" + name] = time.perf_counter() - t
        if blob is None:                       # idea does not apply
            res[name], res["ok_" + name] = None, True
            continue
        back = dec(blob)
        res[name] = len(blob)
        res["ok_" + name] = (back.columns == table.columns
                             and back.rows == table.rows)
    return res


def main(argv):
    out = None
    if "--json" in argv:
        i = argv.index("--json")
        out, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    results = []
    for p in argv:
        r = measure(p)
        results.append(r)
        parts = []
        for name, _, _ in IDEAS:
            v = r[name]
            parts.append("{}={}{}".format(
                name, "n/a" if v is None else "{:,} ({:+.2f}%)".format(
                    v, 100 * (v / r["base"] - 1)),
                "" if r["ok_" + name] else " FAILED"))
        print("{:24s} base={:,}  {}".format(r["name"], r["base"],
                                            "  ".join(parts)), flush=True)
        if out:
            with open(out, "w") as fh:
                json.dump(results, fh, indent=1)
    return results


if __name__ == "__main__":
    main(sys.argv[1:])
