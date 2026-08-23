#!/usr/bin/env python3
"""Does a SPATIAL row order (Hilbert / Morton on lat,lon) pay for itself?

This codec already pays for one permutation shared by every column, so the
marginal cost of choosing a spatial key instead of some other key is zero --
the question is only whether the key is a better one. Measured here as:

    encode(rows in order X) + xz(inverse permutation)   for several X

The permutation is charged in full at its bit-packed-then-xz size, which is
the honest floor (LAW 1: sorting is conservation).
"""
from __future__ import annotations
import csv, io, lzma, os, shutil, struct, subprocess, sys, tempfile, time

sys.path.insert(0, os.getcwd())
import numpy as np

SCRATCH = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
           "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/floats/tmp")
KANZI = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
         "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/kanzi-cpp/bin/"
         "kanzi_dynamic")


def xz(b):
    return len(lzma.compress(b, format=lzma.FORMAT_RAW, filters=[
        {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])) if b else 0


def cm(data):
    if not data:
        return 0
    os.makedirs(SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        src = os.path.join(d, "in"); arc = os.path.join(d, "o.knz")
        open(src, "wb").write(data)
        r = subprocess.run([KANZI, "-c", "-i", src, "-o", arc, "-b", "16m",
                            "-j", "1", "-f", "-t", "None", "-e", "TPAQX"],
                           capture_output=True)
        return os.path.getsize(arc) if r.returncode == 0 else -1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def canon(rows):
    buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def packperm(perm):
    n = len(perm)
    w = max(1, ((n - 1).bit_length() + 7) // 8)
    out = bytearray()
    for v in perm:
        out += int(v).to_bytes(w, "little")
    return bytes(out)


def hilbert_d(x, y, order):
    rx = ry = 0
    d = 0
    s = 1 << (order - 1)
    while s > 0:
        rx = 1 if (x & s) > 0 else 0
        ry = 1 if (y & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        # rotate
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        s >>= 1
    return d


def morton(x, y, bits):
    d = 0
    for i in range(bits):
        d |= ((x >> i) & 1) << (2 * i)
        d |= ((y >> i) & 1) << (2 * i + 1)
    return d


def pp(cols, rows):
    from polypress import fast

    class T:
        pass
    t = T(); t.columns = list(cols); t.rows = rows
    t.column = lambda j, _r=rows: [r[j] for r in _r]
    return len(fast.encode(t))


def main(path, latc, lonc, keycols):
    from polypress import dtz
    t = dtz.read_any(path)
    n = len(t.columns)
    rows = [r + [""] * (n - len(r)) if len(r) < n else r[:n]
            for r in t.rows[:40000]]
    cols = list(t.columns)
    jl, jo = cols.index(latc), cols.index(lonc)

    def f(v, d):
        try:
            return float(v)
        except ValueError:
            return d
    lat = [f(r[jl], 0.0) for r in rows]
    lon = [f(r[jo], 0.0) for r in rows]
    BITS = 22
    qx = [min((1 << BITS) - 1, max(0, int((a + 90) * 1e4))) for a in lat]
    qy = [min((1 << BITS) - 1, max(0, int((b + 180) * 1e4))) for b in lon]

    orders = {"original": list(range(len(rows)))}
    orders["sort lat"] = sorted(range(len(rows)), key=lambda i: qx[i])
    orders["sort lat,lon"] = sorted(range(len(rows)),
                                    key=lambda i: (qx[i], qy[i]))
    orders["morton(lat,lon)"] = sorted(range(len(rows)),
                                       key=lambda i: morton(qx[i], qy[i], BITS))
    orders["hilbert(lat,lon)"] = sorted(
        range(len(rows)), key=lambda i: hilbert_d(qx[i], qy[i], BITS))
    for kc in keycols:
        j = cols.index(kc)
        orders[f"sort {kc}"] = sorted(range(len(rows)), key=lambda i: rows[i][j])

    print(f"\n=== {os.path.basename(path)}  row-order experiment "
          f"({len(rows):,} rows)")
    hdr = (f"{'order':<20}{'xz':>10}{'perm':>9}{'xz+perm':>10}"
           f"{'CM':>10}{'CM+perm':>10}{'pp':>10}{'pp+perm':>10}{'vs orig':>9}")
    print(hdr); print("-" * len(hdr))
    base = None
    for name, perm in orders.items():
        rr = [rows[i] for i in perm]
        raw = canon(rr)
        pcost = 0 if name == "original" else xz(packperm(perm))
        x, c = xz(raw), cm(raw)
        p = pp(cols, rr)
        tot = p + pcost
        if base is None:
            base = tot
        print(f"{name:<20}{x:>10,}{pcost:>9,}{x+pcost:>10,}{c:>10,}"
              f"{c+pcost:>10,}{p:>10,}{tot:>10,}{tot/base:>9.3f}", flush=True)

    # lat/lon as a pair vs two independent columns, in the best spatial order
    print(f"\n-- lat/lon coding, per row order --")
    print(f"{'order':<20}{'lat xz':>9}{'lon xz':>9}{'sum':>9}"
          f"{'interleaved':>13}{'latCM':>9}{'lonCM':>9}{'sumCM':>9}")
    for name in ("original", "sort lat", "hilbert(lat,lon)",
                 "morton(lat,lon)"):
        perm = orders[name]
        la = ("\n".join(rows[i][jl] for i in perm) + "\n").encode()
        lo = ("\n".join(rows[i][jo] for i in perm) + "\n").encode()
        il = ("\n".join(rows[i][jl] + "," + rows[i][jo] for i in perm)
              + "\n").encode()
        print(f"{name:<20}{xz(la):>9,}{xz(lo):>9,}{xz(la)+xz(lo):>9,}"
              f"{xz(il):>13,}{cm(la):>9,}{cm(lo):>9,}{cm(la)+cm(lo):>9,}",
              flush=True)


main("../IN/corpus/usgs_quakes.csv", "latitude", "longitude",
     ["place", "time"])
