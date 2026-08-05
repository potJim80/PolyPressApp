#!/usr/bin/env python3
"""Probe: is a data table better compressed as an IMAGE?

    python3 benchmarks/probe_image.py ../IN/corpus_matrix/*.csv

The proposal was "run xz, then turn the xz output into an image". That half is
answered a priori: LZMA output is a bit-packed range-coded stream, so byte
position no longer corresponds to any row or column and there is no 2D
structure left to model. This probe measures the ONLY ordering that could
work -- table -> image -> image codec -- on the three matrix-shaped tables,
the shape an image codec has any case at all.

WHAT IS BEING SEPARATED
-----------------------
A PNG is two things bolted together: a 2D PREDICTOR (the per-scanline filter,
one of none/sub/up/average/paeth) and an ENTROPY CODER (deflate). Comparing a
.png against xz confounds them -- deflate is simply weaker than LZMA, so the
image would lose for a reason that has nothing to do with 2D.

So every filter is measured twice: once finished with zlib (a real PNG) and
once finished with xz (the filter's contribution, isolated). If the filters
are the good idea, filter+xz beats xz. If they are not, it does not, and the
whole family is dead regardless of which entropy coder is bolted on.

ORIENTATION IS THE OTHER HALF
-----------------------------
  normal      image row = table row  -- `up` differences DOWN a column
  transposed  image row = table column -- `sub` differences down a column,
              and each column's bytes land contiguously, which is what LZMA
              actually wants

FAIRNESS -- READ BEFORE QUOTING A NUMBER
----------------------------------------
An image is a rectangle of fixed-width integers, so the comparison is only
honest if every method is handed exactly that. This probe therefore builds a
CLEANED NUMERIC BLOCK: the columns that are exactly representable as a fixed
number of decimal places, with any row that breaks one of them dropped. Every
method -- xz, polypress, stridexz, and every image variant -- is then run on
that identical block. The dropped columns and rows are printed; a comparison
against the whole file would be measuring the date column, not the idea.

This mirrors the 2026-07-29 matrix run, which had to drop four rows of
treasury_yields for the same reason (the all-or-nothing numeric gate).

Image sizes include an xz'd JSON sidecar carrying column names, decimal
counts and offsets -- without it the pixels do not reconstruct the text, and
an image that cannot reproduce "7.83" is not a competitor.
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import lzma
import os
import re
import subprocess
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "old"))

import numpy as np                       # noqa: E402

from polypress import dtz, fast          # noqa: E402
from stridexz import codec               # noqa: E402

XZ = dict(format=lzma.FORMAT_XZ,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])

STRIDEXZ_BEST = {"pool": True, "fixed": True, "planes": True,
                 "tune": {"lc": 4, "pb": 0}}

MIN_CONFORM = 0.95      # a column is a numeric candidate at this hit rate
DEC = re.compile(r"^-?\d+(?:\.(\d+))?$")


# ------------------------------------------------------------- numeric block

def render(v: int, d: int) -> str:
    """Integer + decimal count -> the printed text, exactly."""
    if d == 0:
        return str(v)
    a = abs(v)
    s = "%d.%0*d" % (a // 10 ** d, d, a % 10 ** d)
    return "-" + s if v < 0 else s


def parse(cell: str, d: int):
    """-> int, or None if the cell does not render back byte for byte.

    The round-trip check is the whole test. "-0.0" parses to 0 and renders as
    "0.0", so it is refused here exactly as the codec refuses it."""
    m = DEC.match(cell)
    if not m:
        return None
    if len(m.group(1) or "") != d:
        return None
    neg = cell[0] == "-"
    v = int((cell[1:] if neg else cell).replace(".", ""))
    if neg:
        v = -v
    return v if render(v, d) == cell else None


def column_decimals(cells):
    """The decimal count this column is mostly written at, or None."""
    counts = collections.Counter()
    for c in cells:
        m = DEC.match(c)
        if m:
            counts[len(m.group(1) or "")] += 1
    if not counts:
        return None
    d, n = counts.most_common(1)[0]
    return d if n >= MIN_CONFORM * len(cells) else None


def numeric_block(t):
    """-> (Table, matrix, decimals, dropped_cols, dropped_rows)."""
    cand = []
    for i in range(len(t.columns)):
        cells = t.column(i)
        d = column_decimals(cells)
        if d is None:
            continue
        if len(set(cells)) < 2:          # a constant column is not a signal
            continue
        cand.append((i, d))

    if not cand:
        return None, None, None, list(t.columns), 0

    parsed = {}
    for i, d in cand:
        parsed[i] = [parse(c, d) for c in t.column(i)]

    keep = [r for r in range(t.shape[0])
            if all(parsed[i][r] is not None for i, _ in cand)]

    names = [t.columns[i] for i, _ in cand]
    rows = [[t.column(i)[r] for i, _ in cand] for r in keep]
    mat = np.array([[parsed[i][r] for i, _ in cand] for r in keep],
                   dtype=np.int64)
    dropped_cols = [t.columns[i] for i in range(len(t.columns))
                    if i not in {c for c, _ in cand}]
    return (dtz.Table(names, rows), mat, [d for _, d in cand],
            dropped_cols, t.shape[0] - len(keep))


def to_uint16(mat):
    """-> (uint16 array, offsets, how) or (None, None, why-it-cannot-be-imaged).

    An image is fixed-width unsigned samples. 16 bits is the widest any of PNG,
    TIFF or JPEG 2000 will carry per channel, so a table that does not fit is
    not representable as one image -- that is a finding, not a failure."""
    lo, hi = int(mat.min()), int(mat.max())
    if hi - lo < 65536:
        return (mat - lo).astype(np.uint16), [lo] * mat.shape[1], "global"
    off = mat.min(axis=0)
    if int((mat - off).max()) < 65536:
        return (mat - off).astype(np.uint16), [int(v) for v in off], "per-column"
    return None, None, "range needs more than 16 bits per sample"


# -------------------------------------------------------------- PNG filtering
#
# PNG filters reference the *unfiltered* bytes of the current and previous
# scanline, so every row can be computed at once -- there is no sequential
# dependency, despite how the spec reads.

FILTERS = ["none", "sub", "up", "average", "paeth"]


def _neighbours(a, bpp):
    left = np.zeros_like(a)
    left[:, bpp:] = a[:, :-bpp]
    up = np.zeros_like(a)
    up[1:] = a[:-1]
    upleft = np.zeros_like(a)
    upleft[1:, bpp:] = a[:-1, :-bpp]
    return left, up, upleft


def _paeth(left, up, upleft):
    p = left.astype(np.int16) + up.astype(np.int16) - upleft.astype(np.int16)
    pa = np.abs(p - left)
    pb = np.abs(p - up)
    pc = np.abs(p - upleft)
    out = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, up, upleft))
    return out.astype(np.uint8)


def filter_scanlines(a, bpp, which):
    """-> (h, stride+1) uint8, filter byte in column 0."""
    left, up, upleft = _neighbours(a, bpp)
    cand = [
        a,
        (a - left).astype(np.uint8),
        (a - up).astype(np.uint8),
        (a - ((left.astype(np.uint16) + up.astype(np.uint16)) >> 1)
         .astype(np.uint8)).astype(np.uint8),
        (a - _paeth(left, up, upleft)).astype(np.uint8),
    ]
    h, stride = a.shape
    out = np.empty((h, stride + 1), dtype=np.uint8)
    if which == "adaptive":
        # libpng's heuristic: minimise the sum of absolute signed byte values.
        score = np.stack([np.abs(c.view(np.int8).astype(np.int16)).sum(axis=1)
                          for c in cand])
        pick = score.argmin(axis=0)
        out[:, 0] = pick
        stacked = np.stack(cand)
        out[:, 1:] = stacked[pick, np.arange(h)]
    else:
        k = FILTERS.index(which)
        out[:, 0] = k
        out[:, 1:] = cand[k]
    return out


def unfilter_scanlines(out, bpp):
    """Reverse of the above -- this is what makes the probe honest."""
    h, w1 = out.shape
    stride = w1 - 1
    a = np.zeros((h, stride), dtype=np.uint8)
    for r in range(h):
        k = int(out[r, 0])
        row = out[r, 1:].astype(np.int16)
        prev = a[r - 1].astype(np.int16) if r else np.zeros(stride, np.int16)
        cur = np.zeros(stride, np.int16)
        for x in range(stride):
            left = cur[x - bpp] if x >= bpp else 0
            up = prev[x]
            ul = prev[x - bpp] if x >= bpp else 0
            if k == 0:
                p = 0
            elif k == 1:
                p = left
            elif k == 2:
                p = up
            elif k == 3:
                p = (left + up) >> 1
            else:
                pp = left + up - ul
                pa, pb, pc = abs(pp - left), abs(pp - up), abs(pp - ul)
                p = left if (pa <= pb and pa <= pc) else (up if pb <= pc else ul)
            cur[x] = (row[x] + p) & 0xFF
        a[r] = cur.astype(np.uint8)
    return a


def png_file(arr16, which):
    """A real, well-formed PNG: 16-bit greyscale, deflate, chunk overhead and
    all. PIL is made to read it back, which is what pins the writer."""
    h, w = arr16.shape
    lines = np.frombuffer(arr16.astype(">u2").tobytes(),
                          dtype=np.uint8).reshape(h, w * 2)
    idat = zlib.compress(filter_scanlines(lines, 2, which).tobytes(), 9)

    def chunk(tag, data):
        return (len(data).to_bytes(4, "big") + tag + data
                + zlib.crc32(tag + data).to_bytes(4, "big"))

    ihdr = (w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + bytes([16, 0, 0, 0, 0]))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def filtered_xz(arr16, which):
    """The same filtered scanlines, finished with xz instead of deflate."""
    h, w = arr16.shape
    lines = np.frombuffer(arr16.astype(">u2").tobytes(),
                          dtype=np.uint8).reshape(h, w * 2)
    return lzma.compress(filter_scanlines(lines, 2, which).tobytes(), **XZ)


# ------------------------------------------------------------------ measuring

def sidecar(names, decs, offsets, shape, how):
    meta = {"names": names, "decimals": decs, "offsets": offsets,
            "shape": list(shape), "offset_mode": how}
    return len(lzma.compress(json.dumps(meta).encode(), **XZ))


def jpeg2000(arr16):
    """Reversible 5/3 wavelet -- a genuine 2D model, not a scanline filter.

    Round-tripped here rather than trusted: a lossless claim from a codec whose
    default is lossy is exactly the thing to check."""
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(arr16).save(buf, format="JPEG2000", irreversible=False)
        blob = buf.getvalue()
        back = np.array(Image.open(io.BytesIO(blob))).astype(np.uint16)
        if not np.array_equal(back.reshape(arr16.shape), arr16):
            return "LOSSY"
        return len(blob)
    except Exception as exc:                                  # noqa: BLE001
        return "n/a (%s)" % str(exc).split("\n")[0][:40]


def verify_png(arr16, which):
    """Round-trip the pixels, and make PIL agree the file is a PNG."""
    h, w = arr16.shape
    lines = np.frombuffer(arr16.astype(">u2").tobytes(),
                          dtype=np.uint8).reshape(h, w * 2)
    back = unfilter_scanlines(filter_scanlines(lines, 2, which), 2)
    if not np.array_equal(back, lines):
        return False
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png_file(arr16, which)))
        got = np.array(im).reshape(h, w).astype(np.uint16)
        return np.array_equal(got, arr16)
    except Exception:                                         # noqa: BLE001
        return False


def measure(path, args):
    t0 = time.time()
    t = dtz.read_any(path).normalise()
    block, mat, decs, dropped_cols, dropped_rows = numeric_block(t)
    name = os.path.basename(path)

    print("=" * 78)
    print("%s   %d x %d" % (name, t.shape[0], t.shape[1]))
    if block is None:
        print("  no fixed-decimal numeric columns -- not imageable at all")
        return
    print("  numeric block      %d x %d" % (block.shape[0], block.shape[1]))
    print("  columns dropped    %d  %s" % (len(dropped_cols),
                                           ", ".join(dropped_cols[:6])))
    print("  rows dropped       %d" % dropped_rows)

    canon = dtz.canonical_csv(block)
    print("  block as CSV       %s B" % f"{len(canon):,}")

    rows = []
    rows.append(("xz -9e  (baseline)", len(lzma.compress(canon, **XZ))))
    try:
        rows.append(("brotli -q11", len(subprocess.run(
            ["brotli", "-q", "11", "-c"], input=canon,
            stdout=subprocess.PIPE, check=True).stdout)))
    except Exception:                                         # noqa: BLE001
        pass

    rows.append(("polypress (planar)", len(fast.encode(block))))
    rows.append(("stridexz BEST", len(codec.encode(block, **STRIDEXZ_BEST))))

    arr, offsets, how = to_uint16(mat)
    if arr is None:
        print("\n  NOT IMAGEABLE: %s" % how)
        for label, n in rows:
            print("    %-26s %12s" % (label, f"{n:,}"))
        return

    meta = sidecar(list(block.columns), decs, offsets, arr.shape, how)
    print("  pixels             %d x %d, 16-bit, %s offset" %
          (arr.shape[1], arr.shape[0], how))
    print("  sidecar (xz'd)     %s B  [added to every image row]" % f"{meta:,}")

    variants = [("normal", arr), ("transposed", np.ascontiguousarray(arr.T))]
    img = {}
    for orient, a in variants:
        # controls: no 2D model at all
        img[(orient, "raw + xz")] = len(
            lzma.compress(a.astype("<u2").tobytes(), **XZ)) + meta
        planes = a.astype("<u2").tobytes()
        shuffled = b"".join(planes[p::2] for p in range(2))
        img[(orient, "byte-planes + xz")] = len(
            lzma.compress(shuffled, **XZ)) + meta
        for f in FILTERS + ["adaptive"]:
            img[(orient, "PNG %s" % f)] = len(png_file(a, f)) + meta
            img[(orient, "filter %s + xz" % f)] = len(filtered_xz(a, f)) + meta
        j2k = jpeg2000(a)
        img[(orient, "JPEG2000 lossless")] = (
            j2k + meta if isinstance(j2k, int) else j2k)

    base = rows[0][1]
    print()
    print("  %-26s %12s %8s" % ("", "bytes", "vs xz"))
    for label, n in rows:
        print("  %-26s %12s %7.2fx" % (label, f"{n:,}", base / n))

    print()
    print("  %-26s %14s %14s" % ("as an image", "normal", "transposed"))
    keys = ["raw + xz", "byte-planes + xz"]
    keys += ["PNG %s" % f for f in FILTERS + ["adaptive"]]
    keys += ["filter %s + xz" % f for f in FILTERS + ["adaptive"]]
    keys += ["JPEG2000 lossless"]
    for k in keys:
        cells = []
        for orient, _ in variants:
            v = img[(orient, k)]
            cells.append("%s (%.2fx)" % (f"{v:,}", base / v)
                         if isinstance(v, int) else str(v))
        print("  %-26s %14s %14s" % (k, cells[0], cells[1]))

    good = verify_png(arr, "adaptive")
    print("\n  PNG round-trip exact (and PIL reads it): %s" % ("yes" if good else "NO"))
    print("  %.1fs" % (time.time() - t0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args()
    for p in args.paths:
        measure(p, args)


if __name__ == "__main__":
    main()
