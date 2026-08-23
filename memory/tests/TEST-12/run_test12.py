#!/usr/bin/env python3
"""Drive TEST-12 -- word-level modelling of the CSV text stream.

  python3 ../memory/tests/TEST-12/run_test12.py --selftest
  python3 ../memory/tests/TEST-12/run_test12.py '../IN/corpus/*.csv'

Single worker by design: the session budget is 2 threads and 3 GB.
"""
import csv
import glob
import io
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import wordmodel as W

MAXROWS = int(os.environ.get("T12_ROWS", "40000"))
SCRATCH = ("/private/tmp/claude-501/-Users-mahdiakbarin-Desktop-Compression/"
           "5f80e916-d35e-47b1-a0ab-9b7261e40056/scratchpad/test12")


def ppmd(data: bytes, order: int = 16) -> int:
    os.makedirs(SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(dir=SCRATCH)
    try:
        src = os.path.join(d, "d.bin")
        open(src, "wb").write(data)
        arc = os.path.join(d, "a.7z")
        subprocess.run(["7zz", "a", "-t7z", f"-m0=PPMd:mem=256m:o={order}",
                        "-mmt=1", "-bso0", "-bsp0", arc, src],
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        size = os.path.getsize(arc)
        out = os.path.join(d, "out")
        os.makedirs(out, exist_ok=True)
        subprocess.run(["7zz", "x", f"-o{out}", "-bso0", "-bsp0", "-y", arc],
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        assert open(os.path.join(out, "d.bin"), "rb").read() == data
        return size
    finally:
        shutil.rmtree(d, ignore_errors=True)


def selftest():
    rnd = random.Random(11)
    cases = [
        b"a,b,c\n1,2,3\n1,2,4\n",
        b"",
        b"x",
        b",\n,\n",
        b"HELLO WORLD,123.456,-7e9\nHELLO WORLD,123.456,-7e9\n",
        bytes(range(256)),
        b"\n" * 50,
        ("naive,cafe,ok\n" * 30).encode(),
    ]
    for _ in range(12):
        n = rnd.randint(1, 400)
        cases.append(bytes(rnd.choice(b"ab,\n019.-XY \x00\xff")
                           for _ in range(n)))
    for i, data in enumerate(cases):
        if not data:
            continue
        for name, enc, dec in (("H0", W.encode_H0, W.decode_H0),
                               ("H1c", W.encode_H1c, W.decode_H1c)):
            got = dec(enc(data))
            assert got == data, f"case {i} {name}: {got[:40]!r} != {data[:40]!r}"
        a, s = W.id_stream(data)
        assert W.id_rebuild(a, s) == data, f"case {i} ID"
    print(f"selftest OK -- {len(cases)} cases, H0 / H1c / ID all exact")


def main():
    selftest()
    if "--selftest" in sys.argv:
        return
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    from polypress import dtz

    paths = []
    for pat in (args or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    names = ["xz raw", "ppmd raw", "H0 word", "H1c col", "ID+xz", "ID+ppmd",
             "n*H0", "n*H1"]
    print(f"# TEST-12  word-level modelling, {MAXROWS:,} rows max")
    print("# H0/H1c/ID decoded and compared; n*H0 and n*H1 are floors, "
          "table cost not charged\n")
    hdr = f"{'table':<22}{'tokens':>10}{'alpha':>9}" + \
        "".join(f"{n:>12}" for n in names)
    print(hdr)
    print("-" * len(hdr))

    tot = {n: 0 for n in names}
    for p in paths:
        try:
            t = dtz.read_any(p)
        except Exception as e:
            print(f"{os.path.basename(p)[:21]:<22} SKIP {type(e).__name__}")
            continue
        rows = t.rows[:MAXROWS]
        if not rows:
            continue
        ncol = len(t.columns)
        rows = [r + [""] * (ncol - len(r)) if len(r) < ncol else r[:ncol]
                for r in rows]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        for r in rows:
            w.writerow(r)
        raw = buf.getvalue().encode()
        # The Huffman coders here are pure Python; a 34 MB table is ~15M
        # tokens and a 116-column H1c builds one code table per column over a
        # 500k alphabet. That ran for an hour and hit the RAM ceiling. The
        # answer is already unambiguous on the tables that do fit, so the two
        # giants are skipped and SAID to be skipped rather than silently
        # dropped.
        if len(raw) > 12_000_000:
            print(f"{os.path.basename(p)[:21]:<22}"
                  f"{'SKIP -- too large for the pure-Python coders':>60}",
                  flush=True)
            continue

        sizes = {}
        sizes["xz raw"] = len(W.xz(raw))
        sizes["ppmd raw"] = ppmd(raw)

        b = W.encode_H0(raw)
        assert W.decode_H0(b) == raw, f"{p}: H0"
        sizes["H0 word"] = len(b)

        b = W.encode_H1c(raw)
        assert W.decode_H1c(b) == raw, f"{p}: H1c"
        sizes["H1c col"] = len(b)

        alpha_raw, stream = W.id_stream(raw)
        assert W.id_rebuild(alpha_raw, stream) == raw, f"{p}: ID"
        sizes["ID+xz"] = len(W.xz(alpha_raw)) + len(W.xz(stream))
        sizes["ID+ppmd"] = ppmd(alpha_raw) + ppmd(stream)

        f0, f1, ntok, nalpha = W.entropy_floor(raw)
        sizes["n*H0"] = int(f0)
        sizes["n*H1"] = int(f1)

        n = os.path.basename(p).replace(".csv", "")[:21]
        print(f"{n:<22}{ntok:>10,}{nalpha:>9,}"
              + "".join(f"{sizes[k]:>12,}" for k in names), flush=True)
        for k in names:
            tot[k] += sizes[k]

    print("-" * len(hdr))
    print(f"{'TOTAL':<22}{'':>10}{'':>9}"
          + "".join(f"{tot[k]:>12,}" for k in names))
    print(f"{'vs xz raw':<22}{'':>10}{'':>9}"
          + "".join(f"{tot[k]/tot['xz raw']:>12.3f}" for k in names))


if __name__ == "__main__":
    main()
