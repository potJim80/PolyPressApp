#!/usr/bin/env python3
"""Drive TEST-13 -- does sorting the alphabet pay once the body is charged?

  python3 ../memory/tests/TEST-13/run_test13.py --selftest
  python3 ../memory/tests/TEST-13/run_test13.py '../IN/corpus/*.csv'

Single worker: the session budget is 2 threads and 3 GB.
"""
import csv
import glob
import io
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "TEST-8"))
sys.path.insert(0, os.getcwd())
import inlinedict as I
import sortalpha as S

MAXROWS = int(os.environ.get("T13_ROWS", "40000"))


def selftest():
    rnd = random.Random(3)
    nasty = ["", "0", "42", "abc", "a,b", "x\x01y", "p\x02p", "l\nl", "\x03",
             "é", "  ", "AAA", "AAB", "AAC", "aab", "-1.5"]
    tables = [
        [[v, "1", v] for v in nasty] * 2,
        [["1", "a"], ["2", "b"], ["3", "a"], ["4", "c"]] * 30,
        [[""]],
        [[f"u{i}", "k", str(i % 3)] for i in range(300)],
    ]
    for _ in range(12):
        ncol = rnd.randint(1, 4)
        nrow = rnd.randint(1, 150)
        tables.append([[rnd.choice(nasty) for _ in range(ncol)]
                       for _ in range(nrow)])
    for ti, rows in enumerate(tables):
        for name, enc in (("B", S.encode_B), ("Bs", S.encode_Bs),
                          ("Bsf", S.encode_Bsf)):
            got = S.decode(enc(rows))
            assert got == rows, f"table {ti} {name}"
    # front coder alone, on the values most likely to break it
    for _ in range(200):
        n = rnd.randint(1, 40)
        strs = sorted({"".join(rnd.choice("ab\x00é ") for _ in
                               range(rnd.randint(0, 6))) for _ in range(n)})
        assert S.un_front(S.front_code(strs))[0] == strs
    print(f"selftest OK -- {len(tables)} tables x 3 variants + 200 front-code "
          f"cases, all exact")


def main():
    selftest()
    if "--selftest" in sys.argv:
        return
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    from polypress import dtz

    paths = []
    for pat in (args or ["../IN/corpus/*.csv"]):
        paths.extend(sorted(glob.glob(pat)))

    names = ["xz raw", "B appear", "Bs sorted", "Bsf front", "C inline"]
    print(f"\n# TEST-13  alphabet order, {MAXROWS:,} rows max")
    print("# every variant decoded and compared cell by cell\n")
    hdr = (f"{'table':<22}" + "".join(f"{n:>12}" for n in names)
           + f"{'Bsf/B':>8}{'Bsf/xz':>8}" + f"{'alpha app':>11}"
           + f"{'alpha srt':>11}{'alpha frt':>11}")
    print(hdr)
    print("-" * len(hdr))

    tot = {n: 0 for n in names}
    ta = {"appearance": 0, "sorted": 0, "sorted+front": 0}
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

        sizes = {"xz raw": len(I.xz(raw))}
        for key, enc, dec in (("B appear", S.encode_B, S.decode),
                              ("Bs sorted", S.encode_Bs, S.decode),
                              ("Bsf front", S.encode_Bsf, S.decode)):
            b = enc(rows)
            assert dec(b) == rows, f"{p}: {key}"
            sizes[key] = len(b)
        b = I.encode_C(rows)
        assert I.decode_C(b) == rows, f"{p}: C"
        sizes["C inline"] = len(b)

        a = S.alphabet_only(rows)
        for k in ta:
            ta[k] += a[k]

        n = os.path.basename(p).replace(".csv", "")[:21]
        print(f"{n:<22}" + "".join(f"{sizes[k]:>12,}" for k in names)
              + f"{sizes['Bsf front']/sizes['B appear']:>8.3f}"
              + f"{sizes['Bsf front']/sizes['xz raw']:>8.3f}"
              + f"{a['appearance']:>11,}{a['sorted']:>11,}"
              + f"{a['sorted+front']:>11,}", flush=True)
        for k in names:
            tot[k] += sizes[k]

    print("-" * len(hdr))
    print(f"{'TOTAL':<22}" + "".join(f"{tot[k]:>12,}" for k in names)
          + f"{tot['Bsf front']/tot['B appear']:>8.3f}"
          + f"{tot['Bsf front']/tot['xz raw']:>8.3f}"
          + f"{ta['appearance']:>11,}{ta['sorted']:>11,}"
          + f"{ta['sorted+front']:>11,}")
    print(f"{'vs xz raw':<22}"
          + "".join(f"{tot[k]/tot['xz raw']:>12.3f}" for k in names))
    print(f"\nalphabet block alone: appearance {ta['appearance']:,}  "
          f"sorted {ta['sorted']:,} ({ta['sorted']/ta['appearance']:.3f})  "
          f"sorted+front {ta['sorted+front']:,} "
          f"({ta['sorted+front']/ta['appearance']:.3f})")


if __name__ == "__main__":
    main()
