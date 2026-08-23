#!/usr/bin/env python3
"""TEST-18 -- which COLUMN carries the gap to kanzi?

TEST-14 measured `kanzi -l9` beating Polypress on 6 of 13 tables, worst
`austin_incidents` at 0.799. LAW 3 says the tables we lose are the ones where
row reordering does not fire. That is a statement about tables. This asks the
sharper question: **which columns?**

Two views, both per column, on the tables we lose:

  isolated   the column ALONE as a one-column table, through polypress, kanzi
             and xz. Shows where each codec is strong with no cross-column
             help at all.
  ablated    the whole table MINUS that column, through polypress and kanzi.
             The column carrying the gap is the one whose removal closes it.

Isolated is the cheap view and can mislead -- a column that is cheap alone may
be the parent that makes five others cheap. Ablated is the honest one, and
costs one encode per column, so it is run only on the named tables.

  python3 ../memory/tests/TEST-18/run_ablate.py austin_incidents usgs_quakes
"""
from __future__ import annotations

import csv
import io
import lzma
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.getcwd())

KANZI = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
         "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/kanzi-cpp/bin/"
         "kanzi_dynamic")
SCRATCH = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
           "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/test18")
MAXROWS = int(os.environ.get("T18_ROWS", "40000"))
XZ = dict(format=lzma.FORMAT_RAW,
          filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])


def xz(b):
    return lzma.compress(b, **XZ)


def kanzi(data: bytes) -> int:
    os.makedirs(SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        src = os.path.join(d, "in.bin")
        arc = os.path.join(d, "out.knz")
        open(src, "wb").write(data)
        r = subprocess.run([KANZI, "-c", "-i", src, "-o", arc, "-l", "9",
                            "-b", "16m", "-j", "1", "-f"],
                           capture_output=True)
        if r.returncode != 0:
            return -1
        return os.path.getsize(arc)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def as_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def main():
    from polypress import dtz, fast
    names = sys.argv[1:] or ["austin_incidents"]

    def pp(rs, cols):
        class T:
            pass
        a = T(); a.columns = cols; a.rows = rs
        a.column = lambda j, _r=rs: [r[j] for r in _r]
        return len(fast.encode(a))

    for name in names:
        t = dtz.read_any(f"../IN/corpus/{name}.csv")
        rows = t.rows[:MAXROWS]
        ncol = len(t.columns)
        rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
                for r in rows]
        full_pp = pp(rows, t.columns)
        full_kz = kanzi(as_csv(rows))
        print(f"\n# TEST-18  {name}: {ncol} columns, {len(rows):,} rows")
        print(f"# whole table -- polypress {full_pp:,}  kanzi {full_kz:,}  "
              f"knz/pp {full_kz/full_pp:.3f}\n")
        hdr = (f"{'column':<26}{'distinct':>9}{'iso pp':>11}{'iso knz':>11}"
               f"{'iso k/p':>8}{'abl pp':>11}{'abl knz':>11}{'gap close':>11}")
        print(hdr)
        print("-" * len(hdr))
        gap = full_kz - full_pp
        rowsout = []
        for j, cname in enumerate(t.columns):
            col = [[r[j]] for r in rows]
            ipp = pp(col, [cname])
            ikz = kanzi(as_csv(col))
            keep = [k for k in range(ncol) if k != j]
            sub = [[r[k] for k in keep] for r in rows]
            app = pp(sub, [t.columns[k] for k in keep])
            akz = kanzi(as_csv(sub))
            # how much of the whole-table gap disappears when this column goes
            close = 1 - (akz - app) / gap if gap else 0.0
            rowsout.append((close, cname, len(set(r[j] for r in rows)),
                            ipp, ikz, app, akz))
            print(f"{cname[:25]:<26}{len(set(r[j] for r in rows)):>9,}"
                  f"{ipp:>11,}{ikz:>11,}{ikz/ipp if ipp else 0:>8.3f}"
                  f"{app:>11,}{akz:>11,}{close:>10.1%}", flush=True)
        print("-" * len(hdr))
        rowsout.sort(reverse=True)
        print("columns closing the most of the gap when removed:")
        for close, cname, nd, ipp, ikz, app, akz in rowsout[:4]:
            print(f"  {cname[:30]:<32} {close:>7.1%}  "
                  f"(alone: pp {ipp:,} vs kanzi {ikz:,})")


if __name__ == "__main__":
    main()
