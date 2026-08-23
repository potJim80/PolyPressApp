#!/usr/bin/env python3
"""Timestamp columns: does parsing to an integer and delta-coding beat leaving
the text alone? Measured against xz -9e, against polypress's own handling, and
against the pure context mixer (kanzi -t None -e TPAQX).

Every representation here is exactly invertible: the format string is fixed and
checked by re-rendering and comparing to the original cell. Anything that does
not re-render is an exception, stored as text.
"""
from __future__ import annotations
import datetime as dt
import lzma, os, re, shutil, struct, subprocess, sys, tempfile

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


def packw(a, w=None):
    """Frame-of-reference, byte-aligned fixed width."""
    if len(a) == 0:
        return b""
    lo = int(min(a)); hi = int(max(a))
    width = max(1, ((hi - lo).bit_length() + 7) // 8) if w is None else w
    out = bytearray(struct.pack("<qB", lo, width))
    for v in a:
        out += int(v - lo).to_bytes(width, "little")
    return bytes(out)


def zig(v):
    return (v << 1) ^ (v >> 63)


def varint(a):
    out = bytearray()
    for v in a:
        u = zig(int(v))
        while True:
            b = u & 0x7F
            u >>= 7
            if u:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
    return bytes(out)


def transpose(cells):
    """Byte-column transpose of a fixed-width text column: all first chars,
    then all second chars, ... This is the 'stream split' idea."""
    w = max(len(c) for c in cells)
    pad = [c.ljust(w, "\x00") for c in cells]
    out = bytearray()
    for k in range(w):
        for c in pad:
            out.append(ord(c[k]))
    return bytes(out)


PATS = [
    ("iso_ms", re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
                          r"\.(\d{3})Z$"),
     lambda m: (dt.datetime(*(int(m.group(i)) for i in range(1, 7)),
                            tzinfo=dt.timezone.utc).timestamp() * 1000
                + int(m.group(7)))),
    ("iso_ms_naive", re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):"
                                r"(\d{2})\.(\d{3})$"),
     lambda m: (dt.datetime(*(int(m.group(i)) for i in range(1, 7)),
                            tzinfo=dt.timezone.utc).timestamp() * 1000
                + int(m.group(7)))),
    ("us_dt", re.compile(r"^(\d{2})/(\d{2})/(\d{4})  (\d{2}):(\d{2})$"),
     lambda m: (dt.datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)),
                            int(m.group(4)), int(m.group(5)),
                            tzinfo=dt.timezone.utc).timestamp() / 60)),
]


def to_ints(cells):
    for name, rx, fn in PATS:
        vals = []
        ok = True
        for c in cells:
            m = rx.match(c)
            if not m:
                ok = False
                break
            vals.append(int(fn(m)))
        if ok:
            return name, np.array(vals, dtype=np.int64)
    return None, None


def report(label, cells):
    text = ("\n".join(cells) + "\n").encode()
    tx, tc = xz(text), cm(text)
    name, a = to_ints(cells)
    print(f"\n{label}  n={len(cells)}  pattern={name}")
    print(f"  {'representation':<30}{'xz -9e':>10}{'CM':>10}{'best':>10}")
    print(f"  {'text (what we store now)':<30}{tx:>10,}{tc:>10,}"
          f"{min(tx,tc):>10,}")
    tr = transpose(cells)
    print(f"  {'text, byte-transposed':<30}{xz(tr):>10,}{cm(tr):>10,}"
          f"{min(xz(tr),cm(tr)):>10,}")
    if a is None:
        print("  (no exact integer parse)")
        return
    for tag, blob in [
            ("int64 FOR-packed", packw(a)),
            ("delta, FOR-packed", packw(np.diff(a, prepend=a[:1]))),
            ("delta, varint", varint(np.diff(a, prepend=a[:1]))),
            ("delta-of-delta, FOR-pack",
             packw(np.diff(np.diff(a, prepend=a[:1]), prepend=np.int64(0)))),
            ("delta-of-delta, varint",
             varint(np.diff(np.diff(a, prepend=a[:1]), prepend=np.int64(0)))),
            ("delta as ASCII text",
             ("\n".join(str(int(v)) for v in np.diff(a, prepend=a[:1]))
              + "\n").encode()),
    ]:
        print(f"  {tag:<30}{xz(blob):>10,}{cm(blob):>10,}"
              f"{min(xz(blob),cm(blob)):>10,}")


def main():
    from polypress import dtz
    for path, want in [("../IN/corpus/usgs_quakes.csv", {"time", "updated"}),
                       ("../IN/corpus/austin_incidents.csv",
                        {"occ_date_time", "rep_date_time", "occ_date",
                         "occ_time"})]:
        t = dtz.read_any(path)
        n = len(t.columns)
        rows = [r + [""] * (n - len(r)) if len(r) < n else r[:n]
                for r in t.rows[:40000]]
        for j, c in enumerate(t.columns):
            if c in want:
                report(f"{os.path.basename(path)}::{c}", [r[j] for r in rows])


main()
