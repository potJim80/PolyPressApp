"""Minimal SAS Transport (XPT v5) reader -- enough to read NHANES public files.

Written instead of pulling in pandas: the format is fully specified and this
is ~80 lines, where pandas is a very large dependency to add to a machine for
one benchmark.

Layout: 80-byte header records, then 140-byte NAMESTR descriptors (padded to a
multiple of 80), then fixed-width observation records. Numerics are IBM 360
base-16 floating point, which is not IEEE and has to be converted by hand.
"""

import struct
import sys

REC = 80
NAMESTR = 140


def ibm_to_float(b: bytes):
    """IBM 360 hex float -> Python float, or None for a SAS missing value."""
    if b == b"\x00" * len(b):
        return 0.0
    # SAS missing: first byte '.', '_' or 'A'-'Z', rest zero
    if b[1:] == b"\x00" * (len(b) - 1) and (
            b[0] in (0x2E, 0x5F) or 0x41 <= b[0] <= 0x5A):
        return None
    sign = -1.0 if b[0] & 0x80 else 1.0
    exp = (b[0] & 0x7F) - 64
    mant = int.from_bytes(b[1:], "big")
    # mantissa is a base-16 fraction with (len-1)*2 hex digits
    return sign * mant * 16.0 ** (exp - 2 * (len(b) - 1))


def read_xpt(path):
    """-> (list of column names, list of rows, each row a list of str)."""
    data = open(path, "rb").read()
    pos = 0
    # walk header records until the NAMESTR header
    nvars = None
    while pos + REC <= len(data):
        rec = data[pos:pos + REC]
        pos += REC
        if rec.startswith(b"HEADER RECORD*******NAMESTR"):
            nvars = int(rec[54:58])
            break
    if nvars is None:
        raise ValueError("no NAMESTR header in {}".format(path))

    fields = []
    for i in range(nvars):
        ns = data[pos + i * NAMESTR: pos + (i + 1) * NAMESTR]
        ntype, _nhfun, nlng, _nvar0 = struct.unpack(">hhhh", ns[0:8])
        name = ns[8:16].decode("ascii", "replace").strip()
        fields.append((name, ntype, nlng))
    total = nvars * NAMESTR
    pos += total + (-total % REC)          # pad to an 80-byte boundary

    rec = data[pos:pos + REC]
    if not rec.startswith(b"HEADER RECORD*******OBS"):
        raise ValueError("no OBS header in {}".format(path))
    pos += REC

    width = sum(f[2] for f in fields)
    rows = []
    while pos + width <= len(data):
        chunk = data[pos:pos + width]
        pos += width
        if chunk.strip(b"\x00") == b"" or chunk.strip() == b"":
            break                          # trailing pad record
        out, at = [], 0
        for _name, ntype, nlng in fields:
            raw = chunk[at:at + nlng]
            at += nlng
            if ntype == 1:
                v = ibm_to_float(raw)
                if v is None:
                    out.append("")
                elif v == int(v) and abs(v) < 1e15:
                    out.append(str(int(v)))
                else:
                    out.append(repr(round(v, 6)))
            else:
                out.append(raw.decode("latin-1").strip())
        rows.append(out)
    return [f[0] for f in fields], rows


if __name__ == "__main__":
    cols, rows = read_xpt(sys.argv[1])
    print(len(cols), "cols", len(rows), "rows")
    print(cols[:12])
    for r in rows[:3]:
        print(r[:12])
