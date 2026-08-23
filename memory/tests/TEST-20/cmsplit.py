#!/usr/bin/env python3
"""Is kanzi's advantage the TEXT word-dictionary transform, or the context
mixer? Run the same bytes through kanzi with the transform stack switched off.

-t None -e TPAQX  = pure context mixer, no transform at all.
-l 9              = EXE+RLT+TEXT+UTF+DNA & TPAQX (what TEST-14 measured).
"""
from __future__ import annotations
import csv, io, lzma, os, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.getcwd())

SCRATCH = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
           "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/floats/tmp")
KANZI = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
         "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/kanzi-cpp/bin/"
         "kanzi_dynamic")

CFG = [("l9  TEXT+..&TPAQX", ["-l", "9"]),
       ("None&TPAQX", ["-t", "None", "-e", "TPAQX"]),
       ("None&CM", ["-t", "None", "-e", "CM"]),
       ("None&FPAQ", ["-t", "None", "-e", "FPAQ"]),
       ("PACK&TPAQX", ["-t", "PACK", "-e", "TPAQX"]),
       ("BWT+RANK&TPAQX", ["-t", "BWT+RANK", "-e", "TPAQX"])]


def xz(b):
    return len(lzma.compress(b, format=lzma.FORMAT_RAW, filters=[
        {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])) if b else 0


def knz(data, args):
    os.makedirs(SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        src = os.path.join(d, "in"); arc = os.path.join(d, "o.knz")
        open(src, "wb").write(data)
        r = subprocess.run([KANZI, "-c", "-i", src, "-o", arc, "-b", "16m",
                            "-j", "1", "-f"] + args, capture_output=True)
        if r.returncode != 0:
            return -1
        return os.path.getsize(arc)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def canon(rows):
    buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def main(paths, cols):
    from polypress import dtz
    for path in paths:
        t = dtz.read_any(path)
        n = len(t.columns)
        rows = [r + [""] * (n - len(r)) if len(r) < n else r[:n]
                for r in t.rows[:40000]]
        name = os.path.basename(path).replace(".csv", "")
        targets = [("<whole table>", canon(rows))]
        for j, c in enumerate(t.columns):
            if c in cols:
                targets.append((c, ("\n".join(r[j] for r in rows)
                                    + "\n").encode()))
        hdr = f"{name:<24}{'xz -9e':>10}" + "".join(f"{k:>18}" for k, _ in CFG)
        print("\n" + hdr); print("-" * len(hdr))
        for label, data in targets:
            x = xz(data)
            row = f"{label[:23]:<24}{x:>10,}"
            for _, args in CFG:
                s = knz(data, args)
                row += f"{s:>12,}{s/x:>6.2f}" if s > 0 else f"{'err':>18}"
            print(row, flush=True)


if __name__ == "__main__":
    main(["../IN/corpus/usgs_quakes.csv"],
         {"place", "latitude", "time", "magError", "depthError", "id"})
    main(["../IN/corpus/austin_incidents.csv"],
         {"ucr_code", "census_block_group", "occ_time", "occ_date_time",
          "crime_type"})
