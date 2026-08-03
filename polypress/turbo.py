"""Turbo: the speed fork. Same ideas as fast.py, none of the double encodes.

Master spends 83-92% of encode time compressing bytes it then throws away
(measured 2026-08-03, cProfile, 10 MB slices):

    cdc_nndss        2.38s total, 1.97s of it the plain-fallback guard (83%)
    chicago_permits 13.02s total, only ~1.1s producing bytes anyone keeps (9%)

The guards exist for one structural reason: master concatenates every column
into ONE lzma stream, so no column has a size of its own. Shrinking column A
can swell column B, so nothing can be judged locally and every decision has to
be made by encoding the whole table twice and comparing.

Measured cost of making the streams independent (2026-08-03):

    payload         cdc_nndss  chicago_permits  usgs_quakes  seattle_fire911
    binary columns      +4.2%            +5.2%        +0.2%            +0.3%
    string groups      +13.2%           +13.1%        -1.0%           -12.4%

Cheap for the binary payloads, expensive for strings on tables whose text
columns resemble each other. So turbo splits the binary payloads (and gets
parallelism and local decisions from it) and keeps the string groups in one
pile by default -- `text_mode` exposes the trade because it goes both ways.

What that buys, and it is the whole point: **a column's compressed size is now
its own**, so every decision can be measured on that column alone instead of by
re-encoding the table. No never-worse guard, no lenient/strict second pass, no
2D on/off second pass, no front-coding trial over the whole pile.

Threading is free on top: lzma and bz2 release the GIL (measured 2.53x on 4
threads for xz-9e, 3.85x for bzip2), so the per-column compressions run
concurrently with no pickling and no copied table.

NOT byte-compatible with fast.py, and deliberately so -- this is a different
container (`PPZT`). Invariant 1 binds fast.py and the C encoder to each other;
it does not bind this.
"""

from __future__ import annotations

import bz2
import json
import lzma
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import dtz
from . import fast

MAGIC = b"PPZT"

# Kept identical to fast.py so the two produce comparable payloads and any size
# difference is attributable to the container and the decisions, not the codec.
XZ = fast.XZ
PROBE = fast._PROBE

# How many rows a decision may look at. Every choice here is a comparison
# between two orderings of the same column, and a comparison does not need the
# whole column -- master probes full columns and spends 1.06s of a 13s encode
# doing it. Generous enough that a rare value still appears.
DECIDE_ROWS = 20000

# The sample-scale model check that replaces the plain fallback. Validated on
# 81 corpus100 datasets 2026-08-03: at margin 0.95 it costs 1,575 bytes of
# 30,335,521 (+0.005%) against always encoding both ways, and runs in 0.11s
# where the thing it replaces is 31x dearer.
#
# The margin is one-sided on purpose. A sample under-rates the columnar model
# in two ways that both push the same direction -- fixed costs (alphabets,
# metadata) do not shrink with the sample while the payload does, and the
# reorder win grows with row count. So the sample never over-rates the model,
# and requiring the row-wise candidate to win by a margin cannot discard a win.
SAMPLE_ROWS = 4000
SAMPLE_MARGIN_NUM, SAMPLE_MARGIN_DEN = 95, 100

_CPUS = max(1, (os.cpu_count() or 2) - 1)
# Each concurrent lzma encoder wants its own dictionary. Measured 2026-08-03:
# 4 threads on a 10 MB table cost +486 MB. Cap the pool by input size so a big
# table cannot walk through the 1-2 GB ceiling.
_XZ_ENCODER_MB = 130


def _pool_size(nbytes: int) -> int:
    """Threads we can afford for compression on an input of this size."""
    budget_mb = 900
    per = min(_XZ_ENCODER_MB, 40 + nbytes / 1e6)
    return max(1, min(_CPUS, int(budget_mb // max(per, 1))))


# ------------------------------------------------------------------ helpers

def _xz(b: bytes) -> bytes:
    return lzma.compress(b, **XZ)


def _unxz(b: bytes) -> bytes:
    return lzma.decompress(b, **XZ)


def _probe(b: bytes) -> int:
    return len(lzma.compress(b, **PROBE))


def _stable_perm(ids: np.ndarray) -> np.ndarray:
    return np.argsort(ids, kind="stable")


# --------------------------------------------------------------- decisions

def _as_dict_or_text(cells, j, nrows) -> dict:
    uniq = sorted(set(cells))
    if len(uniq) <= fast.DICT_MAX and len(uniq) * 2 <= max(nrows, 2):
        idx = {v: i for i, v in enumerate(uniq)}
        return {"kind": "dict", "alpha": uniq,
                "ids": np.array([idx[v] for v in cells], dtype=np.int64),
                "j": j}
    return {"kind": "text", "cells": cells, "j": j}


def classify(table) -> List[dict]:
    """fast.classify, with the lenient-numeric choice made PER COLUMN.

    Recovering a column that is numeric apart from a few cells is worth a great
    deal where it applies -- 41% of the Treasury yield curve -- and a disaster
    where it does not, because it also moves the column out of the dictionary
    path. That is the trade CLAUDE.md records as making the reverted
    ragged-decimal experiment 9-23% worse.

    Master cannot decide it per column: its columns share one stream, so it
    encodes the WHOLE TABLE both ways and keeps the smaller. Measured on the
    bench set, that second encode is worth a lot -- ev_pop 420,588 lenient
    against 274,425 strict, sideways 1,385,863 against 948,662 -- which is why
    turbo v1, which always took the lenient plan, shipped 52% and 70% larger.

    `fast._lenient_promising` already computes exactly the right comparison and
    master uses it only as a nominator for the global double encode. Here it is
    the decision, which is sound for the same reason everything else in this
    module is: the column really is its own stream.
    """
    plan = fast.classify(table, lenient=True)
    nrows = len(table.rows)
    out = []
    for c in plan:
        if c["kind"] == "num" and c.get("ex") and not c.get("exp"):
            out.append(_as_dict_or_text(table.column(c["j"]), c["j"], nrows))
        else:
            out.append(c)
    return out


def pick_parents(plan, nrows) -> Tuple[Dict[int, Optional[int]], List[int]]:
    """Same nomination as fast.pick_parents; the confirmation is now local.

    Master confirms a parent with `_probe_bytes` over the whole column, which
    is correct there only because it happens to be comparing two orderings of
    the same bytes. Here the confirmation is also *sufficient*, because the
    column really is its own stream -- master's version could be right about
    the column and wrong about the file.
    """
    dict_pos = [p for p, c in enumerate(plan) if c["kind"] == "dict"]
    if nrows < fast.MIN_ROWS_FOR_PARENTS or len(dict_pos) < 2:
        return {p: None for p in dict_pos}, list(dict_pos)

    npairs = max(1, len(dict_pos) * (len(dict_pos) - 1))
    rows = min(fast.MI_SAMPLE,
               max(fast.MI_MIN_SAMPLE, fast.MI_BUDGET // npairs))
    step = max(1, nrows // rows)
    sample = {p: np.ascontiguousarray(plan[p]["ids"][::step]) for p in dict_pos}
    sizes = {p: len(plan[p]["alpha"]) for p in dict_pos}
    base, distinct = {}, {}
    for p in dict_pos:
        base[p], distinct[p] = fast._entropy_and_distinct(sample[p])

    from collections import defaultdict
    gain = defaultdict(dict)
    for a in dict_pos:
        ha, ma = base[a], distinct[a]
        for b in dict_pos:
            if a == b:
                continue
            g = fast._score(base[b] - fast._cond_entropy_corrected(
                sample[a], sample[b], sizes[b], ha, ma))
            if g > fast._MIN_GAIN_SCORE:
                gain[b][a] = g

    root = min(dict_pos, key=lambda p: fast._score(base[p]))
    placed, order = {root}, [root]
    parent: Dict[int, Optional[int]] = {root: None}
    remaining = [p for p in dict_pos if p != root]
    while remaining:
        best = None
        for b in remaining:
            for a, g in gain[b].items():
                if a in placed and (best is None or g > best[2]):
                    best = (b, a, g)
        if best is None:
            b = min(remaining, key=lambda p: fast._score(base[p]))
            parent[b] = None
        else:
            b, a, _ = best
            parent[b] = a
        placed.add(b)
        order.append(b)
        remaining.remove(b)

    # Confirm on a sample rather than the whole column -- this is a comparison
    # between two orderings, and a comparison converges long before the column
    # is exhausted.
    dstep = max(1, nrows // DECIDE_ROWS)
    for b in order:
        a = parent.get(b)
        if a is None:
            continue
        ids = plan[b]["ids"]
        perm = _stable_perm(plan[a]["ids"])
        w = fast._width(len(plan[b]["alpha"]))
        got = ids[perm][::dstep].astype(w).tobytes()
        raw = ids[::dstep].astype(w).tobytes()
        if _probe(got) >= _probe(raw):
            parent[b] = None
    return parent, order


def pick_text_parents(plan, nrows, order) -> Dict[int, Optional[int]]:
    """As fast.pick_text_parents, but every trial runs on a row sample.

    Master probes `TEXT_PARENT_CANDIDATES` full columns per text column; on
    chicago_permits that is 1.06s of a 13s encode, for a decision that is a
    comparison between orderings.
    """
    text_pos = [p for p, c in enumerate(plan) if c["kind"] == "text"]
    out: Dict[int, Optional[int]] = {p: None for p in text_pos}
    dict_pos = [p for p, c in enumerate(plan) if c["kind"] == "dict"]
    if nrows < fast.MIN_ROWS_FOR_PARENTS or not dict_pos or not text_pos:
        return out

    npairs = max(1, len(text_pos) * len(dict_pos))
    rows = min(fast.MI_SAMPLE,
               max(fast.MI_MIN_SAMPLE, fast.MI_BUDGET // npairs))
    step = max(1, nrows // rows)
    dsample = {p: np.ascontiguousarray(plan[p]["ids"][::step])
               for p in dict_pos}
    dbase = {p: fast._entropy_and_distinct(dsample[p]) for p in dict_pos}
    dstep = max(1, nrows // DECIDE_ROWS)

    def choose(tp):
        cells = plan[tp]["cells"][::step]
        uniq = sorted(set(cells))
        idx = {v: i for i, v in enumerate(uniq)}
        ids = np.array([idx[v] for v in cells], dtype=np.int64)
        base_t = fast._entropy(ids)
        ranked = []
        for dp in dict_pos:
            ha, ma = dbase[dp]
            g = fast._score(base_t - fast._cond_entropy_corrected(
                dsample[dp], ids, len(uniq), ha, ma))
            if g > fast._MIN_GAIN_SCORE:
                ranked.append((g, dp))
        if not ranked:
            return tp, None
        ranked.sort(key=lambda r: (-r[0], r[1]))
        full = plan[tp]["cells"]
        keep = _probe("\n".join(full[::dstep]).encode("utf-8"))
        best, best_cost = None, keep
        for _g, dp in ranked[:fast.TEXT_PARENT_CANDIDATES]:
            perm = _stable_perm(plan[dp]["ids"])
            trial = [full[i] for i in perm[::dstep]]
            cost = _probe("\n".join(trial).encode("utf-8"))
            if cost < best_cost:
                best, best_cost = dp, cost
        return tp, best

    with ThreadPoolExecutor(_CPUS) as ex:
        for tp, best in ex.map(choose, text_pos):
            out[tp] = best
    return out


def _choose_front(groups: List[List[str]], ndict: int) -> frozenset:
    """Front-code decision, per group, decided locally.

    Master cannot decide this per group -- it measured three such designs and
    all lost, because the groups share one stream so a locally better group can
    make the pile worse. That reasoning is a consequence of the shared stream.
    Here each group is confirmed against itself, which is the whole cost.
    """
    front = set()
    for i, g in enumerate(groups):
        if not g or len(g) < 8 or any("\n" in s for s in g):
            continue
        sh, tot = fast._prefix_stats(g)
        if sh <= 0 or tot <= 0 or sh * fast.FC_MIN_DEN <= tot * fast.FC_MIN_NUM:
            continue
        plainb = "\n".join(g).encode("utf-8")
        if _probe(fast._front_code(g)) < _probe(plainb):
            front.add(i)
    return frozenset(front)


# ------------------------------------------------------------ the row-wise
# candidate. Not a fallback -- a member of the model family, chosen by the
# same sample the other decisions use. It is what covers tables whose
# redundancy runs across the row rather than down the column: on
# covid_19_vaccinations, splitting into columns makes the data 100.2% larger,
# and master loses 21.9% there because this candidate is only reachable
# through a guard.

def _canonical(table) -> bytes:
    return fast._canonical_bytes(table)


class _Cap:
    """A size the row-wise candidate must beat, which can TIGHTEN mid-flight.

    The two candidates race on separate cores, so when the columnar one lands
    first it can publish its size and let the row-wise one give up immediately
    instead of finishing a compression that has already lost. Without this,
    `text_dissimilar` -- nominated by the sample, beaten by a mile in reality
    -- cost 7.04 MB/s down to 2.24 MB/s for a candidate that never had a
    chance. An int assignment is atomic under the GIL, so no lock is needed.
    """

    __slots__ = ("v",)

    def __init__(self, v: int) -> None:
        self.v = v

    def tighten(self, v: int) -> None:
        if v < self.v:
            self.v = v


_CAP_CHUNK = 1 << 20


def _compress_capped(data: bytes, cap: _Cap, kind: str) -> Optional[bytes]:
    """`data` compressed, or None as soon as it cannot fit under `cap`.

    Compressed output only grows, so abandoning cannot change WHICH candidate
    wins, only the time spent losing. Feeding LZMA2 and bzip2 in chunks is
    byte-identical to a single call -- verified on master, which is what makes
    the cap safe rather than merely fast.
    """
    comp = (lzma.LZMACompressor(**XZ) if kind == "xz" else bz2.BZ2Compressor(9))
    out, total = [], 0
    for i in range(0, len(data), _CAP_CHUNK):
        piece = comp.compress(data[i:i + _CAP_CHUNK])
        total += len(piece)
        if total >= cap.v:
            return None
        out.append(piece)
    piece = comp.flush()
    total += len(piece)
    if total >= cap.v:
        return None
    out.append(piece)
    return b"".join(out)


def _rowwise(table, cap: Optional[_Cap] = None) -> Optional[bytes]:
    canon = _canonical(table)
    try:
        back = fast._from_canonical(canon)
        if back.rows != table.rows or back.columns != table.columns:
            return None
    except Exception:
        return None
    del back
    if cap is None:
        cap = _Cap(len(canon))
    best = None
    for magic, kind in ((b"X", "xz"), (b"B", "bz2")):
        blob = _compress_capped(canon, cap, kind)
        if blob is not None and (best is None or len(blob) + 1 < len(best)):
            best = magic + blob
            cap.tighten(len(blob))
    return best


def _sample_ratio(table) -> float:
    """Row-wise size over columnar size, on a sample. A NOMINATOR only.

    Measured 2026-08-03 and it is not safe as a decider: on `sideways` the
    probe says 1.099 where the truth is 0.860, and it still says 1.736 when
    given every row -- so the error is not sampling, it is the compressor. The
    two candidates respond differently to compression strength, because the
    row-major CSV holds long-range redundancy that a strong coder finds and a
    weak one does not, while the columnar payload is already de-correlated.

    Raising the probe's dictionary to 64 MB was tried on the hypothesis that
    reach was the problem: 1.088 against 1.099, i.e. no effect. Recorded so it
    is not tried again.

    So this only nominates, and the real capped candidate decides. Sweeping the
    threshold over 81 corpus100 datasets: at 1.05 all four genuine row-wise
    wins are nominated and 14 of 81 pay; 1.15 keeps the margin that `sideways`
    needs at 27 of 81.
    """
    n = len(table.rows)
    if n < 200:
        return 1e9
    step = max(1, n // SAMPLE_ROWS)
    sub = dtz.Table(list(table.columns), table.rows[::step])
    # BOTH sides must use the SAME compressor or the comparison is meaningless.
    # Patching `fast.XZ` does not do it: turbo's `_xz` reads turbo's own module
    # global, bound to the same dict at import. Getting that wrong measured the
    # columnar candidate at 9e and the row-wise one at preset 1, so row-wise
    # could never win and `sideways` shipped 70.6% larger than master.
    global XZ
    real = XZ
    try:
        XZ = PROBE
        col = len(_encode_columnar(sub, threads=1, probe=True))
        canon = fast._canonical_bytes(sub)
        row = min(len(lzma.compress(canon, **PROBE)),
                  len(bz2.compress(canon, 1))) + 1
    except Exception:
        return 1e9
    finally:
        XZ = real
    return row / float(col) if col else 1e9


# Nominate the row-wise candidate below this ratio. Generous on purpose: a
# nomination costs TIME (the real candidate is built) and never bytes, because
# the real candidate has to actually be smaller to win.
NOMINATE_ROWWISE = 1.15


# ------------------------------------------------------------------- codec

def encode(table, text_mode: str = "pile") -> bytes:
    """Columnar, with the row-wise candidate raced against it when nominated.

    The two candidates are independent, so when the sample nominates the
    row-wise one they are built concurrently and the smaller wins. That makes
    the guarantee a measurement rather than a prediction, while the tables the
    sample confidently rejects -- 67 of 81 in the corpus sweep -- never build
    the second candidate at all.
    """
    if _sample_ratio(table) >= NOMINATE_ROWWISE:
        return _encode_columnar(table, text_mode=text_mode)

    cap = _Cap(1 << 62)

    def columnar():
        blob = _encode_columnar(table, text_mode, max(1, _CPUS - 2))
        cap.tighten(len(blob) - 5)      # the row-wise container costs 5 bytes
        return blob

    with ThreadPoolExecutor(2) as ex:
        f_col = ex.submit(columnar)
        f_row = ex.submit(_rowwise, table, cap)
        col = f_col.result()
        try:
            row = f_row.result()
        except Exception:
            row = None
    if row is not None and len(row) + 5 < len(col):
        return MAGIC + b"R" + row
    return col


def _encode_columnar(table, text_mode: str = "pile", threads: int = 0,
                     probe: bool = False) -> bytes:
    plan = classify(table)
    nrows = len(table.rows)
    parent, order = pick_parents(plan, nrows)
    # In probe mode skip the text-parent search -- it is the dearest part of
    # the analysis and omitting it only makes the columnar candidate look
    # WORSE, which lowers the ratio and nominates the row-wise candidate more
    # readily. Nominations cost time and never bytes, so the error is on the
    # safe side by construction.
    tparent = ({p: None for p, c in enumerate(plan) if c["kind"] == "text"}
               if probe else pick_text_parents(plan, nrows, order))

    bins: List[bytes] = []
    sgroups: List[List[str]] = []
    specs: List[Optional[dict]] = [None] * len(plan)

    for pos in order:
        col = plan[pos]
        ids = col["ids"]
        par = parent.get(pos)
        if par is not None:
            ids = ids[_stable_perm(plan[par]["ids"])]
        w = fast._width(len(col["alpha"]))
        bins.append(ids.astype(w).tobytes())
        sgroups.append(col["alpha"])
        specs[pos] = {"kind": "dict", "n": len(col["alpha"]), "w": w,
                      "parent": par}

    for pos, col in enumerate(plan):
        if col["kind"] == "text":
            tp = tparent.get(pos)
            cells = col["cells"]
            if tp is not None:
                cells = [cells[i] for i in _stable_perm(plan[tp]["ids"])]
            sgroups.append(cells)
            specs[pos] = {"kind": "text"} if tp is None else \
                         {"kind": "text", "parent": tp}
        elif col["kind"] == "num":
            a = col["ints"]
            k = fast.diff_order(a)
            bins.append(fast.pack_ints(np.diff(a, n=k) if k else a))
            specs[pos] = {"kind": "num", "dec": col["dec"], "k": k,
                          "warm": a[:k].tolist()}
            fast._emit_exceptions(col, specs[pos], bins, sgroups)

    front = _choose_front(sgroups, len(order))
    txt_data, smeta, length_arrays = fast._pack_strings(sgroups, front)
    n_before = len(bins)
    bins.extend(fast.pack_ints(a) for a in length_arrays)

    # --- the parallel part. Independent payloads, one compressor each.
    if text_mode == "split":
        parts = list(bins) + _split_pile(txt_data, smeta)
        nstr = len(parts) - len(bins)
    else:
        parts = list(bins) + [txt_data]
        nstr = 1
    total = sum(len(p) for p in parts)
    nthreads = threads or _pool_size(total)
    if nthreads > 1 and len(parts) > 1:
        with ThreadPoolExecutor(nthreads) as ex:
            blobs = list(ex.map(_xz, parts))
    else:
        blobs = [_xz(p) for p in parts]

    meta = {"columns": table.columns, "nrows": nrows, "cols": specs,
            "order": order, "smeta": smeta, "nlenbins": len(bins) - n_before,
            "nbin": len(bins), "nstr": nstr,
            "sizes": [len(b) for b in blobs],
            "raw": [len(p) for p in parts]}
    meta_b = _xz(json.dumps(meta, separators=(",", ":")).encode())
    return (MAGIC + b"C" + len(meta_b).to_bytes(4, "big") + meta_b
            + b"".join(blobs))


def _split_pile(txt_data: bytes, smeta) -> List[bytes]:
    out, at = [], 0
    for m in smeta:
        out.append(txt_data[at:at + m["b"]])
        at += m["b"]
    return out


def decode(blob: bytes):
    if blob[:4] != MAGIC:
        raise ValueError("not a turbo archive")
    mode = blob[4:5]
    if mode == b"R":
        body = blob[5:]
        if body[:1] == b"X":
            return fast._from_canonical(_unxz(body[1:]))
        if body[:1] == b"B":
            return fast._from_canonical(bz2.decompress(body[1:]))
        raise ValueError("bad row-wise marker")
    if mode != b"C":
        raise ValueError("bad turbo mode")

    ml = int.from_bytes(blob[5:9], "big")
    meta = json.loads(_unxz(blob[9:9 + ml]))
    at = 9 + ml
    parts = []
    for size in meta["sizes"]:
        parts.append(_unxz(blob[at:at + size]))
        at += size

    nbin = meta["nbin"]
    cuts = parts[:nbin]
    if meta["nstr"] == 1:
        txt_data = parts[nbin]
    else:
        txt_data = b"".join(parts[nbin:])

    nrows, specs = meta["nrows"], meta["cols"]
    nlen = meta["nlenbins"]
    length_bins = cuts[len(cuts) - nlen:] if nlen else []
    texts = fast._unpack_strings(txt_data, meta["smeta"], length_bins, 0)

    cols: List[Optional[List[str]]] = [None] * len(specs)
    ids_by_pos: Dict[int, np.ndarray] = {}
    bi = ti = 0
    for pos in meta["order"]:
        sp = specs[pos]
        alpha = texts[ti]
        ti += 1
        ids = np.frombuffer(cuts[bi], dtype=sp["w"]).astype(np.int64)
        bi += 1
        par = sp["parent"]
        if par is not None:
            perm = _stable_perm(ids_by_pos[par])
            out = np.empty_like(ids)
            out[perm] = ids
            ids = out
        ids_by_pos[pos] = ids
        cols[pos] = (np.array(alpha, dtype=object)[ids].tolist()
                     if alpha else [])

    for pos, sp in enumerate(specs):
        if sp["kind"] == "text":
            cells = list(texts[ti])
            ti += 1
            tp = sp.get("parent")
            if tp is not None:
                perm = _stable_perm(ids_by_pos[tp])
                restored = [None] * len(cells)
                for k, src in enumerate(perm):
                    restored[src] = cells[k]
                cells = restored
            cols[pos] = cells
        elif sp["kind"] == "num":
            k = sp["k"]
            d = fast.unpack_ints(cuts[bi], nrows - k)
            bi += 1
            a = fast._undiff(d, np.array(sp["warm"], dtype=np.int64), k) \
                if k else d
            cells = fast.ints_to_cells(a, sp["dec"])
            cells, bi, ti = fast._apply_exceptions(cells, sp, cuts, texts,
                                                   bi, ti)
            cols[pos] = cells

    rows = [list(r) for r in zip(*cols)]
    return dtz.Table(list(meta["columns"]), rows)
