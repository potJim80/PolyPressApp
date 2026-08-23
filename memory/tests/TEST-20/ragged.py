#!/usr/bin/env python3
"""Ragged-decimal columns: what does the codec do with them, and is there a
representation that keeps the exact printed text and still models the value?

Prints (a) the classify() plan for each table, and (b) for every column that is
numeric-looking, a set of alternative representations, each measured with
xz -9e and with kanzi -l9.

Representations (all exactly invertible to the original strings):
  text      the column as written, one cell per line          <- what we do now
  sig+dec   split each cell into (sign, integer significand, #decimals);
            three streams: signif (delta, bit-packed), dec-count bytes,
            exception text.  This is the "(value, format descriptor)" split.
  sig+dec-t same but the significand kept as ASCII decimal text (dot removed)
  scaled    polypress's own rule: one decimal count for the column, cells that
            do not reproduce become text exceptions (what fast._numeric_lenient
            builds), then delta + bit-pack
  gorilla   IEEE754 doubles, XOR with previous, leading/trailing-zero coded
            (Facebook Gorilla).  NOT text-exact -- shown as a floor only.
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


def xz(b):
    if not b:
        return 0
    return len(lzma.compress(b, format=lzma.FORMAT_RAW, filters=[
        {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}]))


def knz(data, level="9"):
    if not data:
        return 0
    os.makedirs(SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        src = os.path.join(d, "in"); arc = os.path.join(d, "o.knz")
        open(src, "wb").write(data)
        r = subprocess.run([KANZI, "-c", "-i", src, "-o", arc, "-l", level,
                            "-b", "16m", "-j", "1", "-f"], capture_output=True)
        if r.returncode != 0:
            return -1
        return os.path.getsize(arc)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- packing
def packbits(a: np.ndarray) -> bytes:
    """Frame-of-reference + fixed-width bit-pack, little-endian byte-aligned
    widths (bytes, not bits -- byte alignment is deliberate, see notes)."""
    if a.size == 0:
        return b""
    lo = int(a.min())
    d = (a - lo).astype(object)
    hi = int(max(d)) if a.size else 0
    w = max(1, (hi.bit_length() + 7) // 8)
    out = bytearray(struct.pack("<qB", lo, w))
    for v in d:
        out += int(v).to_bytes(w, "little")
    return bytes(out)


def best_delta(a: np.ndarray) -> bytes:
    best = packbits(a)
    for k in (1, 2):
        if a.size > k:
            c = packbits(np.diff(a, n=k))
            if len(c) < len(best):
                best = c
    return best


# ------------------------------------------------------- representations
def split_sig_dec(cells):
    """(significands, dec counts, exception positions+text). Exact inverse:
    sign + str(abs(sig)).zfill(dec+1) with a dot inserted dec from the right."""
    sig, dec, expos, exvals = [], [], [], []
    last = 0
    for i, c in enumerate(cells):
        s = c
        neg = s.startswith("-")
        if neg or s.startswith("+"):
            s = s[1:]
        ip, _, fp = s.partition(".")
        ok = (ip.isdigit() and (fp == "" or fp.isdigit())
              and ("." in c) == (fp != "" or c.endswith("."))
              and not c.endswith(".")
              and (ip == "0" or not ip.startswith("0"))
              and not c.startswith("+")
              and len(ip) + len(fp) <= 18)
        if ok:
            v = int(ip + fp) * (-1 if neg else 1)
            # -0.0 style: sign lost when the value is zero
            if v == 0 and neg:
                ok = False
        if not ok:
            expos.append(i); exvals.append(c)
            sig.append(last); dec.append(0)
            continue
        last = v
        sig.append(v); dec.append(len(fp))
    return (np.array(sig, dtype=np.int64), np.array(dec, dtype=np.int64),
            expos, exvals)


def rep_sizes(cells, comp):
    out = {}
    text = ("\n".join(cells) + "\n").encode()
    out["text"] = comp(text)

    sig, dec, expos, exvals = split_sig_dec(cells)
    exb = ("\n".join(exvals) + "\n").encode() if exvals else b""
    expb = packbits(np.array(expos, dtype=np.int64)) if expos else b""
    decb = bytes(int(d) for d in dec)
    base = comp(decb) + comp(exb) + comp(expb)
    out["sig+dec"] = comp(best_delta(sig)) + base
    sigtext = ("\n".join(str(int(v)) for v in sig) + "\n").encode()
    out["sig+dec-t"] = comp(sigtext) + base
    out["_nex"] = len(expos)
    out["_decdistinct"] = len(set(int(d) for d in dec))
    out["_decbytes"] = comp(decb)
    return out


def gorilla(vals):
    """Facebook Gorilla XOR-with-previous, bit level. Returns byte length."""
    prev = 0
    pl = pt = -1
    nbits = 0
    for v in vals:
        b = struct.unpack("<Q", struct.pack("<d", v))[0]
        x = b ^ prev
        prev = b
        if x == 0:
            nbits += 1
            continue
        lead = 64 - x.bit_length()
        trail = (x & -x).bit_length() - 1
        if pl >= 0 and lead >= pl and trail >= pt:
            nbits += 2 + (64 - pl - pt)
        else:
            lead = min(lead, 31)
            mb = 64 - lead - trail
            nbits += 2 + 5 + 6 + mb
            pl, pt = lead, trail
    return (nbits + 7) // 8


def main(path, only=None):
    from polypress import dtz, fast
    t = dtz.read_any(path)
    n = len(t.columns)
    rows = [r + [""] * (n - len(r)) if len(r) < n else r[:n]
            for r in t.rows[:40000]]

    class T:
        pass
    tt = T(); tt.columns = list(t.columns); tt.rows = rows
    tt.column = lambda j, _r=rows: [r[j] for r in _r]
    plan = fast.classify(tt)
    print(f"\n=== {os.path.basename(path)}  plan")
    for p in plan:
        j = p["j"]; c = t.columns[j]
        extra = ""
        if p["kind"] == "num":
            extra = f" dec={p['dec']} ex={0 if not p['ex'] else len(p['ex'][1])}"
        elif p["kind"] == "dict":
            extra = f" alpha={len(p['alpha'])}"
        print(f"  {j:>3} {c[:26]:<27} {p['kind']:<5}{extra}")

    print(f"\n=== {os.path.basename(path)}  ragged-decimal representations")
    hdr = (f"{'col':<22}{'text/xz':>10}{'sig+dec':>10}{'sig+dec-t':>11}"
           f"{'gain':>8}{'text/knz':>10}{'sd/knz':>9}{'gain':>8}"
           f"{'nex':>7}{'decB':>7}")
    print(hdr); print("-" * len(hdr))
    for j, c in enumerate(t.columns):
        if only and c not in only:
            continue
        cells = [r[j] for r in rows]
        frac = sum(1 for x in cells if "." in x) / max(len(cells), 1)
        if frac < 0.5:
            continue
        x = rep_sizes(cells, xz)
        k = rep_sizes(cells, knz)
        bx = min(x["sig+dec"], x["sig+dec-t"])
        bk = min(k["sig+dec"], k["sig+dec-t"])
        print(f"{c[:21]:<22}{x['text']:>10,}{x['sig+dec']:>10,}"
              f"{x['sig+dec-t']:>11,}{1-bx/x['text']:>8.1%}"
              f"{k['text']:>10,}{bk:>9,}{1-bk/k['text']:>8.1%}"
              f"{x['_nex']:>7}{x['_decbytes']:>7,}", flush=True)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        main(p)
