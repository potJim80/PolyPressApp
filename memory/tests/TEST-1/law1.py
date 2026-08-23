#!/usr/bin/env python3
"""
LAW 1 -- one column, n rows.

  A  plain xz -9e on the column as text
  B  number-encode every value, sort ascending, pancake equal values into
     runs, regress, store the differences -- plus whatever it takes to put
     the rows back where they were.

Every B variant is DECODED and compared value by value before its size is
counted.  Where a part of B can be encoded several ways, B is given the
smallest -- an oracle it could not actually achieve without trying them all.
"""
import lzma, math, struct, sys, os, csv, io, random
from collections import Counter

# ---------------------------------------------------------------- primitives

def xz(b):
    return lzma.compress(b, format=lzma.FORMAT_RAW,
                         filters=[{"id": lzma.FILTER_LZMA2,
                                   "preset": 9 | lzma.PRESET_EXTREME}])

def unxz(b):
    return lzma.decompress(b, format=lzma.FORMAT_RAW,
                           filters=[{"id": lzma.FILTER_LZMA2,
                                     "preset": 9 | lzma.PRESET_EXTREME}])

def put_uvarint(out, u):
    assert u >= 0
    while True:
        b = u & 0x7F
        u >>= 7
        out.append(b | (0x80 if u else 0))
        if not u:
            return

def get_uvarint(buf, p):
    u = 0; s = 0
    while True:
        b = buf[p]; p += 1
        u |= (b & 0x7F) << s
        if not (b & 0x80):
            return u, p
        s += 7

def zig(i):   return (i << 1) ^ (i >> 63) if i >= 0 else ((-i) << 1) - 1
def unzig(u): return (u >> 1) ^ -(u & 1)

def uvarints(seq):
    out = bytearray()
    for u in seq:
        put_uvarint(out, u)
    return bytes(out)

def read_uvarints(buf, count, p=0):
    out = []
    for _ in range(count):
        u, p = get_uvarint(buf, p)
        out.append(u)
    return out, p

def bitpack(vals, width):
    if width == 0:
        return b""
    out = bytearray(); acc = 0; nb = 0
    for v in vals:
        acc |= v << nb; nb += width
        while nb >= 8:
            out.append(acc & 0xFF); acc >>= 8; nb -= 8
    if nb:
        out.append(acc & 0xFF)
    return bytes(out)

def bitunpack(buf, count, width):
    if width == 0:
        return [0] * count
    out = []; acc = 0; nb = 0; p = 0
    mask = (1 << width) - 1
    for _ in range(count):
        while nb < width:
            acc |= buf[p] << nb; p += 1; nb += 8
        out.append(acc & mask); acc >>= width; nb -= width
    return out

def section(*parts):
    """length-prefixed concatenation, so every variant is a real self-
    contained container and nothing is measured off the books."""
    out = bytearray()
    put_uvarint(out, len(parts))
    for p in parts:
        put_uvarint(out, len(p))
        out += p
    return bytes(out)

def unsection(buf):
    n, p = get_uvarint(buf, 0)
    parts = []
    for _ in range(n):
        ln, p = get_uvarint(buf, p)
        parts.append(buf[p:p + ln]); p += ln
    return parts

# ------------------------------------------------------------ value ordering

def as_ints(distinct):
    """If every distinct value is a decimal that survives str() round-trip,
    return integers scaled to a common number of decimals.  This is what lets
    the regression actually be a regression."""
    dec = 0
    for v in distinct:
        s = v.strip()
        if not s:
            return None
        try:
            if "." in s:
                head, tail = s.split(".", 1)
                if not tail.isdigit() or not (head.lstrip("-").isdigit()):
                    return None
                dec = max(dec, len(tail))
            else:
                if not s.lstrip("-").isdigit():
                    return None
        except ValueError:
            return None
    scale = 10 ** dec
    out = []
    for v in distinct:
        s = v.strip()
        if "." in s:
            head, tail = s.split(".", 1)
            neg = head.startswith("-")
            iv = int(head.lstrip("-") or "0") * scale + int(tail.ljust(dec, "0"))
            iv = -iv if neg else iv
        else:
            iv = int(s) * scale
        out.append(iv)
    # the text must come back EXACTLY, or this path is a lie
    for iv, v in zip(out, distinct):
        if fmt_int(iv, dec) != v:
            return None
    return out, dec

def fmt_int(iv, dec):
    if dec == 0:
        return str(iv)
    neg = iv < 0
    iv = -iv if neg else iv
    head, tail = divmod(iv, 10 ** dec)
    return ("-" if neg else "") + f"{head}.{tail:0{dec}d}"

def order(values):
    """distinct values in increasing order, plus the numeric view if any."""
    distinct = sorted(set(values))
    num = as_ints(distinct)
    if num is not None:
        ints, dec = num
        pairs = sorted(zip(ints, distinct))
        return [p[1] for p in pairs], [p[0] for p in pairs], dec
    return distinct, None, 0

# ------------------------------------------------------- B: the three parts

def enc_dict_text(distinct):
    out = bytearray()
    for v in distinct:
        b = v.encode("utf-8")
        put_uvarint(out, len(b)); out += b
    return xz(bytes(out))

def dec_dict_text(blob, k):
    buf = unxz(blob); p = 0; out = []
    for _ in range(k):
        ln, p = get_uvarint(buf, p)
        out.append(buf[p:p + ln].decode("utf-8")); p += ln
    return out

def linreg(ys):
    n = len(ys)
    if n < 2:
        return 0.0, float(ys[0]) if ys else 0.0
    sx = n * (n - 1) / 2.0
    sxx = sum(float(i) * i for i in range(n))
    sy = float(sum(ys))
    sxy = sum(float(i) * y for i, y in enumerate(ys))
    den = n * sxx - sx * sx
    if den == 0:
        return 0.0, sy / n
    a = (n * sxy - sx * sy) / den
    b = (sy - a * sx) / n
    return a, b

def enc_dict_numeric(ints, dec):
    """the proposal's step 4, literally: regress the sorted values, keep the
    differences."""
    a, b = linreg(ints)
    resid = [v - int(math.floor(a * i + b + 0.5)) for i, v in enumerate(ints)]
    head = struct.pack("<dd", a, b) + uvarints([dec])
    return xz(head + uvarints(zig(r) for r in resid)), (a, b)

def dec_dict_numeric(blob, k):
    buf = unxz(blob)
    a, b = struct.unpack("<dd", buf[:16])
    dec, p = get_uvarint(buf, 16)
    us, _ = read_uvarints(buf, k, p)
    ints = [unzig(u) + int(math.floor(a * i + b + 0.5)) for i, u in enumerate(us)]
    return [fmt_int(v, dec) for v in ints]

def enc_dict_delta(ints, dec):
    ds = [ints[0]] + [ints[i] - ints[i - 1] for i in range(1, len(ints))]
    return xz(uvarints([dec]) + uvarints(zig(d) for d in ds))

def dec_dict_delta(blob, k):
    buf = unxz(blob)
    dec, p = get_uvarint(buf, 0)
    us, _ = read_uvarints(buf, k, p)
    cur = 0; ints = []
    for u in us:
        cur += unzig(u); ints.append(cur)
    return [fmt_int(v, dec) for v in ints]

def enc_counts(counts):
    """the pancake.  3A 2B C -> 3,2,1."""
    plain = xz(uvarints(counts))
    a, b = linreg(counts)
    resid = [c - int(math.floor(a * i + b + 0.5)) for i, c in enumerate(counts)]
    regr = xz(struct.pack("<dd", a, b) + uvarints(zig(r) for r in resid))
    return (b"\x00" + plain) if len(plain) <= len(regr) else (b"\x01" + regr)

def dec_counts(blob, k):
    tag, body = blob[0], blob[1:]
    buf = unxz(body)
    if tag == 0:
        cs, _ = read_uvarints(buf, k, 0)
        return cs
    a, b = struct.unpack("<dd", buf[:16])
    us, _ = read_uvarints(buf, k, 16)
    return [unzig(u) + int(math.floor(a * i + b + 0.5)) for i, u in enumerate(us)]

# ------------------------------------------------------------ B: variants

def build(values):
    distinct, ints, dec = order(values)
    k = len(distinct)
    idx = {v: i for i, v in enumerate(distinct)}
    codes = [idx[v] for v in values]           # number-encoded, original order
    counts = [0] * k
    for c in codes:
        counts[c] += 1
    return distinct, ints, dec, k, codes, counts

def best_dict(distinct, ints, dec):
    cands = [("text", enc_dict_text(distinct))]
    if ints is not None:
        cands.append(("regress", enc_dict_numeric(ints, dec)[0]))
        cands.append(("delta", enc_dict_delta(ints, dec)))
    name, blob = min(cands, key=lambda t: len(t[1]))
    return name, blob

def dec_dict(name, blob, k):
    return {"text": dec_dict_text, "regress": dec_dict_numeric,
            "delta": dec_dict_delta}[name](blob, k)


def variant_raw_perm(values):
    """The literal reading: give every cell a row number, sort, keep the row
    numbers so you can undo it."""
    distinct, ints, dec, k, codes, counts = build(values)
    n = len(values)
    dname, dblob = best_dict(distinct, ints, dec)
    cblob = enc_counts(counts)
    perm = sorted(range(n), key=lambda i: codes[i])   # stable
    w = 4 if n < 2**32 else 8
    pblob = xz(struct.pack(f"<{len(perm)}{'I' if w==4 else 'Q'}", *perm))
    blob = section(dname.encode(), uvarints([n, k]), dblob, cblob, pblob)

    def decode(blob):
        nb, hdr, db, cb, pb = unsection(blob)
        (n_, k_), _ = read_uvarints(hdr, 2, 0)
        dis = dec_dict(nb.decode(), db, k_)
        cs = dec_counts(cb, k_)
        raw = unxz(pb)
        w_ = 4 if n_ < 2**32 else 8
        perm_ = struct.unpack(f"<{n_}{'I' if w_==4 else 'Q'}", raw)
        sortedvals = []
        for i, c in enumerate(cs):
            sortedvals.extend([dis[i]] * c)
        out = [None] * n_
        for pos, row in enumerate(perm_):
            out[row] = sortedvals[pos]
        return out
    return blob, decode


def variant_gap_perm(values):
    """The best the row numbers can be coded: a stable sort leaves them
    ascending inside a run, so store gaps."""
    distinct, ints, dec, k, codes, counts = build(values)
    n = len(values)
    dname, dblob = best_dict(distinct, ints, dec)
    cblob = enc_counts(counts)
    perm = sorted(range(n), key=lambda i: codes[i])
    gaps = []; prev_run = -1; off = 0
    for c in counts:
        prev = -1
        for j in range(off, off + c):
            gaps.append(perm[j] - prev - 1); prev = perm[j]
        off += c
    pblob = xz(uvarints(gaps))
    blob = section(dname.encode(), uvarints([n, k]), dblob, cblob, pblob)

    def decode(blob):
        nb, hdr, db, cb, pb = unsection(blob)
        (n_, k_), _ = read_uvarints(hdr, 2, 0)
        dis = dec_dict(nb.decode(), db, k_)
        cs = dec_counts(cb, k_)
        gs, _ = read_uvarints(unxz(pb), n_, 0)
        out = [None] * n_
        g = 0
        for i, c in enumerate(cs):
            prev = -1
            for _ in range(c):
                row = prev + 1 + gs[g]; g += 1
                out[row] = dis[i]; prev = row
        return out
    return blob, decode


def variant_min_perm(values):
    """The floor: equal values are interchangeable, so the permutation only
    has to say WHICH value each row holds, not which copy.  Note this makes
    the counts derivable, so B is not charged for them here."""
    distinct, ints, dec, k, codes, counts = build(values)
    n = len(values)
    dname, dblob = best_dict(distinct, ints, dec)
    width = max(1, (k - 1).bit_length()) if k > 1 else 0
    cands = [(b"\x00", xz(bitpack(codes, width))),
             (b"\x01", xz(uvarints(codes)))]
    tag, pblob = min(cands, key=lambda t: len(t[1]))
    blob = section(dname.encode(), uvarints([n, k]), dblob, tag + pblob)

    def decode(blob):
        nb, hdr, db, pb = unsection(blob)
        (n_, k_), _ = read_uvarints(hdr, 2, 0)
        dis = dec_dict(nb.decode(), db, k_)
        tag_, body = pb[0], pb[1:]
        if tag_ == 0:
            w = max(1, (k_ - 1).bit_length()) if k_ > 1 else 0
            cs = bitunpack(unxz(body), n_, w)
        else:
            cs, _ = read_uvarints(unxz(body), n_, 0)
        return [dis[c] for c in cs]
    return blob, decode


def variant_bag(values):
    """No permutation at all -- the multiset only.  Not lossless for a table;
    measured to show what the sort and the pancake actually buy."""
    distinct, ints, dec, k, codes, counts = build(values)
    dname, dblob = best_dict(distinct, ints, dec)
    return section(dname.encode(), uvarints([len(values), k]), dblob,
                   enc_counts(counts)), None

# ------------------------------------------------------------------ measure

def entropy_floor_bytes(counts, n):
    bits = sum(c * math.log2(n / c) for c in counts if c)
    return math.ceil(bits / 8)

def measure(name, values, verbose=True):
    n = len(values)
    text = ("\n".join(values) + "\n").encode("utf-8")
    A = len(xz(text))
    row = {"name": name, "n": n, "k": len(set(values)), "raw": len(text), "A": A}
    for label, fn in (("B_raw", variant_raw_perm),
                      ("B_gap", variant_gap_perm),
                      ("B_min", variant_min_perm),
                      ("B_bag", variant_bag)):
        blob, dec = fn(values)
        if dec is not None:
            got = dec(blob)
            if got != values:
                raise AssertionError(f"{name}/{label} did not round-trip")
        row[label] = len(blob)
    _, _, _, _, _, counts = build(values)
    row["floor"] = entropy_floor_bytes(counts, n)
    row["Bbest"] = min(row["B_raw"], row["B_gap"], row["B_min"])
    return row

HDR = (f"{'dataset / column':<44}{'n':>8}{'k':>7}"
       f"{'xz A':>10}{'B raw':>10}{'B gap':>10}{'B min':>10}"
       f"{'B best':>10}{'B/A':>7}{'nH':>10}")

def line(r):
    return (f"{r['name'][:43]:<44}{r['n']:>8}{r['k']:>7}"
            f"{r['A']:>10,}{r['B_raw']:>10,}{r['B_gap']:>10,}{r['B_min']:>10,}"
            f"{r['Bbest']:>10,}{r['Bbest']/r['A']:>7.2f}{r['floor']:>10,}")
