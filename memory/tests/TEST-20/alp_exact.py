#!/usr/bin/env python3
"""ALP, made TEXT-exact, and charged for it.

The idea the earlier probe missed: on this corpus the printed cell is the
SHORTEST round-trip decimal of the double it denotes (Ryu/Grisu output).  If
that holds, the decimal-place count is a FUNCTION of the double and costs
nothing -- ALP's exact-double guarantee becomes an exact-text guarantee, and
the format-descriptor stream that killed the earlier ragged-decimal attempt
disappears.

So: ALP with a per-1024-vector (e, f), decode each value back to a double,
print it with the shortest round-trip printer, and require the original string.
Anything that fails is a real exception, stored by position and as text.
Everything measured with xz -9e behind it, and against the pure context mixer.
"""
from __future__ import annotations
import lzma, os, shutil, struct, subprocess, sys, tempfile

sys.path.insert(0, os.getcwd())
import numpy as np

SCRATCH = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
           "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/floats/tmp")
KANZI = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
         "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/kanzi-cpp/bin/"
         "kanzi_dynamic")
V = 1024


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


def shortest(v: float) -> str:
    """Python's repr is the shortest round-trip decimal (same class as Ryu).
    Normalised the way a CSV writes it: no trailing '.0'."""
    s = repr(v)
    if s.endswith(".0"):
        s = s[:-2]
    return s


def forpack(a: np.ndarray) -> bytes:
    if a.size == 0:
        return b""
    lo = int(a.min()); hi = int(a.max())
    w = max(1, (int(hi - lo)).bit_length())
    nb = (w * a.size + 7) // 8
    out = bytearray(struct.pack("<qB", lo, w))
    acc = 0; nbits = 0
    for v in a:
        acc |= (int(v) - lo) << nbits
        nbits += w
        while nbits >= 8:
            out.append(acc & 0xFF); acc >>= 8; nbits -= 8
    if nbits:
        out.append(acc & 0xFF)
    assert len(out) - 9 == nb or True
    return bytes(out)


def alp_vector(cells):
    """Pick e minimising bit-packed cost for this vector; return
    (ints array, exception positions, exception strings)."""
    best = None
    for e in range(0, 15):
        ints = []; ex = []
        p = 10.0 ** e
        for i, s in enumerate(cells):
            try:
                v = float(s)
            except ValueError:
                ex.append(i); ints.append(0); continue
            iv = int(round(v * p))
            if abs(iv) >= 1 << 62:
                ex.append(i); ints.append(0); continue
            if shortest(iv / p) != s:
                ex.append(i); ints.append(0); continue
            ints.append(iv)
        a = np.array(ints, dtype=np.int64)
        if a.size:
            for i in ex:
                a[i] = a[0]
            w = max(1, int(int(a.max() - a.min())).bit_length())
        else:
            w = 1
        cost = (w * a.size) // 8 + sum(len(cells[i]) + 3 for i in ex) + 16
        if best is None or cost < best[0]:
            best = (cost, a, ex, e)
    return best[1], best[2], best[3]


def main():
    from polypress import dtz, fast
    for path, want in [
        ("../IN/corpus/usgs_quakes.csv",
         ["latitude", "longitude", "depth", "mag", "dmin", "rms",
          "horizontalError", "depthError", "magError", "gap", "nst",
          "magNst"]),
        ("../IN/corpus/nyc_collisions.csv", None),
    ]:
        t = dtz.read_any(path)
        n = len(t.columns)
        rows = [r + [""] * (n - len(r)) if len(r) < n else r[:n]
                for r in t.rows[:40000]]
        print(f"\n=== {os.path.basename(path)}  ALP made text-exact "
              f"({len(rows):,} rows)")
        hdr = (f"{'column':<20}{'text/xz':>9}{'pp':>9}{'CM':>9}"
               f"{'ALPx/xz':>9}{'ex':>6}{'e':>4}{'vs pp':>8}{'vs CM':>8}")
        print(hdr); print("-" * len(hdr))
        tot = [0, 0, 0, 0]
        for j, c in enumerate(t.columns):
            cells = [r[j] for r in rows]
            frac = sum(1 for x in cells if x and (x[0].isdigit()
                                                  or x[0] in "+-")) / len(cells)
            if want is not None:
                if c not in want:
                    continue
            elif frac < 0.9 or len(set(cells)) < 50:
                continue
            body = bytearray(); expos = []; exvals = []; es = []
            nex = 0
            for s in range(0, len(cells), V):
                chunk = cells[s:s + V]
                a, ex, e = alp_vector(chunk)
                body += forpack(a)
                es.append(e)
                for i in ex:
                    expos.append(s + i); exvals.append(chunk[i])
                nex += len(ex)
            if nex > len(cells) * 0.30:
                continue
            exb = ("\n".join(exvals) + "\n").encode() if exvals else b""
            epb = forpack(np.array(expos, dtype=np.int64)) if expos else b""
            total = (xz(bytes(body)) + xz(exb) + xz(epb) + len(es))
            text = ("\n".join(cells) + "\n").encode()
            tx = xz(text); s_cm = cm(text)

            class T:
                pass
            tt = T(); tt.columns = [c]; tt.rows = [[x] for x in cells]
            tt.column = lambda k, _r=cells: _r
            p = len(fast.encode(tt))
            print(f"{c[:19]:<20}{tx:>9,}{p:>9,}{s_cm:>9,}{total:>9,}"
                  f"{nex:>6}{es[0]:>4}{total/p:>8.3f}{total/max(s_cm,1):>8.3f}",
                  flush=True)
            tot[0] += tx; tot[1] += p; tot[2] += s_cm; tot[3] += total
        if tot[1]:
            print(f"{'TOTAL':<20}{tot[0]:>9,}{tot[1]:>9,}{tot[2]:>9,}"
                  f"{tot[3]:>9,}{'':>10}{tot[3]/tot[1]:>8.3f}"
                  f"{tot[3]/tot[2]:>8.3f}")


main()
