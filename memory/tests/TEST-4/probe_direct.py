#!/usr/bin/env python3
"""TEST-4 -- is xz earning its keep on the streams the model already shaped?

The hypothesis: Polypress sorts a column *specifically* to turn it into
`3 3 3 3 3 ...`, then pays LZMA's optimal parser to go and find those runs.
If so, the structured streams could be coded directly -- RLE + an entropy
coder, one linear pass, no match finder -- at similar size and far less work,
leaving xz for the one stream that genuinely needs a match finder: the
alphabet.

This measures, per table:

  bins  the binary payload -- dictionary ids after parent-sorting, packed
        numeric differences, 2D group residuals. The model already shaped it.
  txt   the string pile -- distinct values, front-coded. Repetition here is
        ACROSS strings, which is what LZ77 is for and an order-0 coder cannot
        see (sortreg finding 6).

Against xz on each, plus where the encode TIME goes. Size is decided here;
speed is not -- a Python rANS runs at ~5 MB/s and cannot settle a claim about
a C implementation. What the timing column does settle is which stream the
current encoder actually spends its time on.

Run from work/:  python3 ../memory/tests/TEST-4/probe_direct.py ../IN/corpus/*.csv
"""
from __future__ import annotations

import glob
import lzma
import os
import sys
import time
from typing import List

import numpy as np

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "TEST-2"))

from polypress import dtz, fast          # noqa: E402
import rans                              # noqa: E402

XZ = fast.XZ
CAP = int(os.environ.get("T4_CAP", "8000000"))


def build_streams(table):
    """Exactly `_encode_plan`'s two payloads, without the container."""
    plan = fast.classify(table)
    nrows = len(table.rows)
    groups = fast.find_2d_groups(plan, nrows)
    in_group = {pos: gi for gi, g in enumerate(groups) for pos in g}
    parent, order = fast.pick_parents(plan, nrows)

    bins: List[bytes] = []
    sgroups: List[List[str]] = []
    specs = [None] * len(plan)

    for pos in order:
        col = plan[pos]
        ids = col["ids"]
        par = parent.get(pos)
        if par is not None:
            perm = np.argsort(plan[par]["ids"], kind="stable")
            ids = ids[perm]
        w = fast._width(len(col["alpha"]))
        bins.append(ids.astype(w).tobytes())
        sgroups.append(col["alpha"])
        specs[pos] = {"kind": "dict"}

    tparent = fast.pick_text_parents(plan, nrows, parent, order)
    for pos, col in enumerate(plan):
        if col["kind"] == "text":
            tp = tparent.get(pos)
            cells = col["cells"]
            if tp is not None:
                perm = np.argsort(plan[tp]["ids"], kind="stable")
                cells = [cells[i] for i in perm]
            sgroups.append(cells)
        elif col["kind"] == "num" and pos in in_group:
            pass
        elif col["kind"] == "num":
            a = col["ints"]
            k = fast.diff_order(a)
            bins.append(fast.pack_ints(np.diff(a, n=k) if k else a))

    for g in groups:
        M = np.stack([plan[pos]["ints"] for pos in g], axis=1)
        D = np.diff(np.diff(M, axis=0), axis=1)
        side = np.concatenate([M[0], np.diff(M, axis=0)[:, 0]])
        bins.append(fast.pack_ints(np.concatenate([side, D.ravel()])))

    front, (txt_data, smeta, length_arrays), txt_b = \
        fast._choose_front(sgroups, len(order))
    bins.extend(fast.pack_ints(a) for a in length_arrays)
    return b"".join(bins), txt_data, txt_b, len(order)


# ---------------------------------------------------------------- direct code

def rle(b: bytes):
    """Run-length split: the run values, and the run lengths as varints.

    One linear pass. This is the information the sort deliberately created,
    read off directly instead of searched for.
    """
    if not b:
        return b"", b""
    a = np.frombuffer(b, dtype=np.uint8)
    change = np.empty(len(a), dtype=bool)
    change[0] = True
    np.not_equal(a[1:], a[:-1], out=change[1:])
    idx = np.flatnonzero(change)
    vals = a[idx].tobytes()
    lens = np.diff(np.append(idx, len(a)))
    out = bytearray()
    for v in lens:
        v = int(v)
        while v >= 0x80:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        out.append(v)
    return vals, bytes(out)


def direct_rle_rans(b: bytes) -> int:
    vals, lens = rle(b)
    return len(rans.rans0(vals)) + len(rans.rans0(lens))


def main():
    paths = []
    for pat in (sys.argv[1:] or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))
    print(f"# TEST-4  is xz earning its keep on the modelled streams?")
    print(f"# bins = model output (sorted ids, deltas). txt = the alphabet.")
    print()
    hdr = (f"{'table':<26}{'bins raw':>11}{'xz':>10}{'rANS0':>10}"
           f"{'rANS1':>10}{'RLE+rANS':>10}{'best/xz':>9}"
           f"{'txt raw':>10}{'txt xz':>9}{'t_bins':>8}{'t_txt':>8}")
    print(hdr)
    print("-" * len(hdr))
    tot = {k: 0 for k in ("braw", "bxz", "r0", "r1", "rle", "traw", "txz")}
    tb = tt = 0.0
    for p in paths:
        try:
            table = dtz.read_any(p)
        except Exception as e:
            print(f"{os.path.basename(p)[:25]:<26} SKIP {type(e).__name__}")
            continue
        if len(table.rows) > 400_000:
            table.rows = table.rows[:400_000]
        try:
            bins, txt_data, txt_b, ndict = build_streams(table)
        except Exception as e:
            print(f"{os.path.basename(p)[:25]:<26} SKIP {type(e).__name__}: {e}")
            continue
        if len(bins) > CAP:
            bins = bins[:CAP]

        t = time.time(); bxz = lzma.compress(bins, **XZ); tbin = time.time() - t
        t = time.time(); _ = lzma.compress(txt_data, **XZ); ttxt = time.time() - t

        r0 = len(rans.rans0(bins))
        r1 = len(rans.rans1(bins))
        rl = direct_rle_rans(bins)
        best = min(r0, r1, rl)
        name = os.path.basename(p).replace(".csv", "")[:25]
        print(f"{name:<26}{len(bins):>11,}{len(bxz):>10,}{r0:>10,}{r1:>10,}"
              f"{rl:>10,}{best / len(bxz) if len(bxz) else 0:>9.2f}"
              f"{len(txt_data):>10,}{len(txt_b):>9,}"
              f"{tbin:>8.2f}{ttxt:>8.2f}", flush=True)
        tot["braw"] += len(bins); tot["bxz"] += len(bxz)
        tot["r0"] += r0; tot["r1"] += r1; tot["rle"] += rl
        tot["traw"] += len(txt_data); tot["txz"] += len(txt_b)
        tb += tbin; tt += ttxt

    print("-" * len(hdr))
    bestt = min(tot["r0"], tot["r1"], tot["rle"])
    print(f"{'TOTAL':<26}{tot['braw']:>11,}{tot['bxz']:>10,}{tot['r0']:>10,}"
          f"{tot['r1']:>10,}{tot['rle']:>10,}"
          f"{bestt / tot['bxz'] if tot['bxz'] else 0:>9.2f}"
          f"{tot['traw']:>10,}{tot['txz']:>9,}{tb:>8.2f}{tt:>8.2f}")
    print()
    print(f"  binary payload  {tot['braw']:>12,} raw -> {tot['bxz']:,} xz")
    print(f"  text pile       {tot['traw']:>12,} raw -> {tot['txz']:,} xz")
    print(f"  xz time         bins {tb:.2f}s   txt {tt:.2f}s   "
          f"bins is {100 * tb / (tb + tt) if tb + tt else 0:.0f}% of it")
    print(f"\n  If RLE+rANS is close to 1.00 on bins, xz is redoing work the")
    print(f"  model already did. If it is well above, xz is earning its keep.")


if __name__ == "__main__":
    main()
