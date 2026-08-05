#!/usr/bin/env python3
"""The C and the Python must agree, and each must read the other's archives.

Three things are checked per table:

  1. byte-identity      -- the two encoders produce the same archive. This is
                           the strong form: it means the two implementations
                           made every single decision the same way, not merely
                           that both decoded correctly.
  2. C reads Python     -- and gets every cell back.
  3. Python reads C     -- likewise.

Byte-identity is the guarantee worth having because divergence is otherwise
silent: two implementations can both be "correct" and still disagree about
which encoding to pick, and then an archive written by one is a different file
from the one written by the other. That is the bug class this test exists for.

    python3 stridexz-c/test_cross.py [file.csv ...]

With no arguments it runs a set of constructed cases that between them touch
every column kind, plus the escaping and the row reorder.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)
sys.path.insert(0, WORK)
sys.path.insert(0, os.path.join(os.path.dirname(WORK), "old"))

from polypress import dtz            # noqa: E402  (the archived rival's I/O)
from stridexz import codec           # noqa: E402

BINARY = os.path.join(HERE, "stridexz")

# What the C binary does by default, so the two are comparing like with like.
OPTS = dict(pool=True, fixed=True, planes=True, tune={"lc": 4, "pb": 0},
            **{"dict": True})


CASES = {
    "tiny": ("a,b\n1,x\n2,y\n3,x\n", None),
    "escapes": ('a,b\n"line\none",back\\slash\n"q""uote",plain\n', None),
    "wide_ints": ("i,j\n" + "".join(f"{v},{-v}\n" for v in range(300)), None),
    "leading_zeros": ("z\n007\n008\n009\n" + "010\n" * 70, None),
    "empty_cells": ("a,b,c\n,,\n1,,3\n,2,\n" + ",,\n" * 80, None),
    "one_column": ("only\n" + "".join(f"v{v % 7}\n" for v in range(200)), None),
    "high_card": ("k\n" + "".join(f"row-{v}\n" for v in range(500)), None),
    "unicode": ("nom,ciudad\nJosé,Bogotá\nÑuñoa,Ñuñoa\n" * 40, None),
    "wide_dict": ("a,b,c\n" + "".join(
        f"{v%3},{v%5},{'yes' if v%2 else 'no'}\n" for v in range(400)), None),
}


def run_case(name, path):
    t = dtz.read_any(path).normalise()

    py = codec.encode(t, **OPTS)
    out_c = path + ".sxz"
    r = subprocess.run([BINARY, "compress", path, out_c],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return name, "C compress failed: " + (r.stderr or r.stdout).strip()[:120]
    with open(out_c, "rb") as fh:
        c = fh.read()

    notes = []
    identical = (py == c)
    if not identical:
        notes.append(f"BYTES DIFFER py={len(py)} c={len(c)}")

    # C archive -> Python decoder
    try:
        if codec.decode(c).rows != t.rows:
            notes.append("python could not read the C archive")
    except Exception as exc:                                  # noqa: BLE001
        notes.append(f"python failed on C archive: {type(exc).__name__}")

    # Python archive -> C decoder
    pypath = path + ".py.sxz"
    with open(pypath, "wb") as fh:
        fh.write(py)
    back = path + ".back.csv"
    r = subprocess.run([BINARY, "restore", pypath, back],
                       capture_output=True, text=True)
    if r.returncode != 0:
        notes.append("C could not read the python archive")
    else:
        if dtz.read_any(back).normalise().rows != t.rows:
            notes.append("C read the python archive but got different cells")

    return name, ("ok" if not notes else "; ".join(notes)), identical


def main():
    if not os.path.exists(BINARY):
        print("build it first: ./stridexz-c/build.sh")
        return 1

    paths = []
    tmp = tempfile.mkdtemp(prefix="sxzcross")
    if len(sys.argv) > 1:
        paths = [(os.path.basename(p), p) for p in sys.argv[1:]]
    else:
        for name, (text, _) in CASES.items():
            p = os.path.join(tmp, name + ".csv")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
            paths.append((name, p))

    bad = 0
    ident = 0
    for name, p in paths:
        try:
            n, verdict, same = run_case(name, p)
        except Exception as exc:                              # noqa: BLE001
            n, verdict, same = name, f"EXCEPTION {type(exc).__name__}: {exc}", False
        ident += bool(same)
        flag = "byte-identical" if same else "DIFFERENT BYTES"
        print(f"  {n:<18}{verdict:<52}{flag}")
        if verdict != "ok":
            bad += 1

    print(f"\n{len(paths) - bad}/{len(paths)} correct, "
          f"{ident}/{len(paths)} byte-identical")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
