"""Differential test: the C binary must decode what the Python encoder writes.

    python3 tests/test_cbin.py

The C port's whole contract is that it agrees with the Python implementation.
An independent C decoder that is *nearly* right is worse than no C decoder,
because it hands a researcher a table that looks plausible and is wrong. So
this does not test the C code against its own idea of correct -- it takes the
same corpus the Python suite uses, encodes each case with Python, decodes it
with the binary, and compares the logical table cell for cell.

Skipped with a clear message, not a failure, when the binary has not been
built: `csrc/build.sh` needs liblzma headers, and a machine without them is a
normal machine, not a broken one.

The comparison is on the LOGICAL table -- names, order, and every cell as an
exact string -- because that is what the format promises. Byte-identical CSV
is explicitly not promised: quoting and line endings are not canonical.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polypress import dtz, fast
from test_dtz import CASES as BASE_CASES
from test_fast import EXTRA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BINARY = os.path.join(ROOT, "csrc", "polypress")


def read_back(path: str):
    """Read the C binary's CSV output with the Python reader."""
    return dtz.read_any(path)


def compare(name: str, table, got) -> str:
    if got is None:
        return "{}: binary produced no output".format(name)
    if got.columns != table.columns:
        return "{}: columns differ\n    want {}\n    got  {}".format(
            name, table.columns[:6], got.columns[:6])
    if len(got.rows) != len(table.rows):
        return "{}: {} rows, expected {}".format(
            name, len(got.rows), len(table.rows))
    for i, (a, b) in enumerate(zip(table.rows, got.rows)):
        if a != b:
            return "{}: row {} differs\n    want {}\n    got  {}".format(
                name, i, a, b)
    return ""


def check_corrupt(tmp: str) -> list:
    """A decoder reads files other people made, so it must survive bad ones.

    Truncations and bit-flips, checked for two things: it must exit cleanly
    with an error rather than crashing, and it must not sit there allocating.
    An earlier version grew its output buffer and retried on ANY decompression
    failure -- but corrupt data never decodes at any size, so it doubled its
    way toward a terabyte. Fuzzing hit that on 132 of 199 mutated inputs; this
    keeps it from coming back.
    """
    import random
    bad = []
    rnd = random.Random(3)
    table = dtz.Table(
        ["a", "b", "c"],
        [[str(i), ["x", "y", "z"][i % 3], "{:.2f}".format(i * 1.5)]
         for i in range(800)])
    good = fast.encode(table)
    arc = os.path.join(tmp, "fuzz.ppz")
    out = os.path.join(tmp, "fuzz.out")

    cases = []
    step = max(1, len(good) // 25)
    for n in range(0, len(good), step):
        cases.append(good[:n])
    for _ in range(60):
        b = bytearray(good)
        b[rnd.randrange(len(b))] = rnd.randrange(256)
        cases.append(bytes(b))
    cases.append(b"")
    cases.append(b"PPZ1")

    for i, data in enumerate(cases):
        with open(arc, "wb") as fh:
            fh.write(data)
        try:
            proc = subprocess.run([BINARY, "restore", arc, "-o", out],
                                  capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            bad.append("corrupt input {} hung (>60s)".format(i))
            continue
        # 0 = it happened to still be valid, 1 = rejected cleanly.
        # Anything else is a crash or an unhandled signal.
        if proc.returncode not in (0, 1):
            bad.append("corrupt input {} exited {} (crash?)".format(
                i, proc.returncode))
    return bad


def check_canonical(tmp: str) -> list:
    """The quoting rules of the CSV the plain fallback is built from.

    The fallback compresses the table re-serialised as canonical CSV, so the C
    encoder has to emit byte-for-byte what Python's
    csv.writer(lineterminator="\\n") emits or the archives differ on exactly
    the tables where the fallback fires. Three rules are easy to get wrong and
    were all found by testing the Python writer rather than reading it:

      - a bare '\\r' is NOT quoted, because csv.writer quotes on characters in
        the *lineterminator* and that is "\\n" alone. Python's own CSV is
        therefore lossy for such a cell, both sides notice when they re-parse,
        and both must refuse the fallback and write the modelled container.
      - an empty field IS quoted when it is the only field in its row.
      - a NUL byte forces quoting.

    These are driven from hand-written CSV text rather than through the usual
    Python writer, because the point is to control the input quoting exactly.
    """
    bad = []
    src = os.path.join(tmp, "canon.csv")
    arc = os.path.join(tmp, "canon.ppz")
    checks = [
        # (name, exact file bytes, container both sides must choose)
        ("bare CR",        b'h\n"a\rb"\n',            b"PPZ1"),
        ("CR mid-table",   b'a,b\nx,"p\rq"\ny,z\n',   b"PPZ1"),
        ("empty 1 col",    b'h\n""\nx\n""\n',         b"PPZX"),
        ("empty 2 col",    b'a,b\n,\n,\n',            b"PPZX"),
        ("embedded LF",    b'a,b\n"p\nq",z\n',        b"PPZX"),
        ("embedded comma", b'a,b\n"p,q",z\n',         b"PPZX"),
        ("embedded quote", b'a,b\n"p""q",z\n',        b"PPZX"),
        ("CRLF row ends",  b'a,b\r\nx,y\r\n',         b"PPZX"),
        # A cell holding "\r\n" contains a '\n', so it IS quoted and does
        # survive the round trip -- unlike the bare '\r' above. The fallback is
        # legitimately available here. This case is in the list because the
        # first version of it asserted PPZ1 on the assumption that anything
        # carrying a '\r' was lossy, and that assumption was wrong.
        ("quoted CRLF",    b'a,b\n"p\r\nq",z\n',      b"PPZX"),
    ]
    for name, data, want_magic in checks:
        with open(src, "wb") as fh:
            fh.write(data)
        try:
            table = dtz.read_any(src)
        except Exception as exc:
            bad.append("canonical/{}: Python could not read it: {}".format(
                name, exc))
            continue
        want = fast.encode(table)
        proc = subprocess.run([BINARY, "compress", src, "-o", arc],
                              capture_output=True)
        if proc.returncode != 0:
            bad.append("canonical/{}: C compress exit {} -- {}".format(
                name, proc.returncode,
                proc.stderr.decode(errors="replace").strip()))
            continue
        with open(arc, "rb") as fh:
            have = fh.read()
        if have != want:
            bad.append(
                "canonical/{}: C wrote {} B {} , Python {} B {}".format(
                    name, len(have), have[:4].decode("ascii", "replace"),
                    len(want), want[:4].decode("ascii", "replace")))
        elif want[:4] != want_magic:
            bad.append(
                "canonical/{}: expected the {} container, both chose {}".format(
                    name, want_magic.decode(),
                    want[:4].decode("ascii", "replace")))
    if not bad:
        print("canonical CSV: {} quoting edge cases, both sides identical "
              "and both refuse the lossy ones".format(len(checks)))
    return bad


def main() -> int:
    if not os.path.exists(BINARY):
        print("csrc/polypress not built -- skipping.")
        print("build it with: ./csrc/build.sh")
        return 0

    cases = dict(BASE_CASES)
    cases.update(EXTRA)

    # The shared corpus is mostly tiny tables, and a tiny table now takes the
    # fallback container -- so on its own it exercises the binary's easiest
    # path and leaves the hard one nearly untested. These force the modelled
    # container and, individually, each piece of machinery inside it: the
    # parent permutation, the 2D group reconstruction, and multi-order
    # undiff with its warm-start values.
    import random
    import string
    rnd = random.Random(9)
    al = string.ascii_letters + string.digits

    cases["_fallback_noise"] = dtz.Table(
        ["a", "b"],
        [["".join(rnd.choice(al) for _ in range(20)),
          "".join(rnd.choice(al) for _ in range(20))] for _ in range(2000)])

    # parent-sorted dictionary columns: the stable-argsort inverse
    cases["_modelled_parents"] = dtz.Table(
        ["zip", "city", "state", "note"],
        [[["98101", "98402", "98501", "97201"][i % 4],
          ["Seattle", "Tacoma", "Olympia", "Portland"][i % 4],
          ["WA", "WA", "WA", "OR"][i % 4],
          "n{}".format(i % 7)] for i in range(3000)])

    # commensurable numeric columns: the 2D group cumsum reconstruction
    cases["_modelled_2d"] = dtz.Table(
        ["a", "b", "c", "d"],
        [["{:.2f}".format(10.0 + i * 0.01 + j * 0.03) for j in range(4)]
         for i in range(1500)])

    # smooth numeric columns: differencing at order > 0, so undiff has to
    # rebuild leading terms from the warm-start values
    cases["_modelled_undiff"] = dtz.Table(
        ["lin", "quad", "cube"],
        [[str(5 + 3 * i), str(7 + i * i), str(i * i * i - 2 * i)]
         for i in range(1200)])

    # values past the one-byte varint escape and past 2^32, forcing the
    # 8-byte tail width
    cases["_modelled_bigvarint"] = dtz.Table(
        ["v", "w"],
        [[str((i * 2654435761) % (1 << 40)), str(i * 7919)]
         for i in range(1500)])

    # a wide text column alongside dictionaries, so both string layouts and
    # the text path are hit at scale
    cases["_modelled_text"] = dtz.Table(
        ["k", "free"],
        [[["a", "b", "c"][i % 3],
          "free text {} with, comma and \"quote\"".format(i)]
         for i in range(1500)])

    # Column NAMES are the only strings that reach the JSON metadata, and the
    # metadata is compared byte for byte -- so the escaping has to match
    # json.dumps exactly. Cell contents go in the text blob and never exercise
    # this. Covers BMP escapes, a non-BMP codepoint (surrogate pair), and the
    # literal escapes.
    cases["_json_escapes"] = dtz.Table(
        ["éü中文", "emoji_🎉_col", "quote\"col", "back\\slash", "tab\tcol", "p"],
        [[str(i % 7), "x{}".format(i % 3), "y", "z", "w", str(i)]
         for i in range(60)])

    tmp = tempfile.mkdtemp(prefix="ppz-cbin-")
    failures = []
    kinds = {}
    enc_kinds = {}
    identical = 0
    encodable = 0
    try:
        for name, table in sorted(cases.items()):
            arc = os.path.join(tmp, name + ".ppz")
            out = os.path.join(tmp, name + ".out.csv")
            blob = fast.encode(table)
            with open(arc, "wb") as fh:
                fh.write(blob)
            kinds[blob[:4]] = kinds.get(blob[:4], 0) + 1

            proc = subprocess.run([BINARY, "restore", arc, "-o", out],
                                  capture_output=True)
            if proc.returncode != 0:
                failures.append("{}: exit {} -- {}".format(
                    name, proc.returncode,
                    proc.stderr.decode(errors="replace").strip()))
                continue
            try:
                got = read_back(out)
            except Exception as exc:
                failures.append("{}: output unreadable: {}".format(name, exc))
                continue
            msg = compare(name, table, got)
            if msg:
                failures.append(msg)

            # --- the encoder: byte-identity, not just equivalence ---
            # Compared against encode, not _encode_plan. This used to be
            # scoped to the modelled path because the C encoder had no plain
            # fallbacks and so wrote a larger file than Python on any table
            # where no trick fired -- never wrong, but bigger, and on 28 of
            # these 39 cases. Now both sides make the same choice, so the
            # comparison covers the container choice as well as the bytes.
            src = os.path.join(tmp, name + ".src.csv")
            with open(src, "w", newline="", encoding="utf-8") as fh:
                import csv as _csv
                w = _csv.writer(fh, lineterminator="\n")
                w.writerow(table.columns)
                w.writerows(table.rows)

            # The C binary reads its own CSV, so only compare where the CSV
            # round-trips to the same logical table -- otherwise a difference
            # is the reader's, not the encoder's.
            if dtz.read_any(src).rows != table.rows:
                continue
            encodable += 1
            carc = os.path.join(tmp, name + ".c.ppz")
            proc = subprocess.run([BINARY, "compress", src, "-o", carc],
                                  capture_output=True)
            if proc.returncode != 0:
                failures.append("{}: C compress exit {} -- {}".format(
                    name, proc.returncode,
                    proc.stderr.decode(errors="replace").strip()))
                continue
            want = fast.encode(table)
            with open(carc, "rb") as fh:
                have = fh.read()
            if have == want:
                identical += 1
                enc_kinds[have[:4]] = enc_kinds.get(have[:4], 0) + 1
            else:
                failures.append(
                    "{}: C encoder differs -- {} B {} vs Python's {} B {}".format(
                        name, len(have), have[:4].decode("ascii", "replace"),
                        len(want), want[:4].decode("ascii", "replace")))
        failures.extend(check_canonical(tmp))
        corrupt_bad = check_corrupt(tmp)
        failures.extend(corrupt_bad)
        if not corrupt_bad:
            print("corrupt archives: rejected cleanly, no crash, no runaway "
                  "allocation")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("{} cases through the C binary".format(len(cases)))
    print("containers exercised: {}".format(", ".join(
        "{} x{}".format(k.decode("ascii", "replace"), v)
        for k, v in sorted(kinds.items()))))
    print("encoder: {}/{} byte-identical to Python, container choice included"
          .format(identical, encodable))
    print("  the C encoder chose: {}".format(", ".join(
        "{} x{}".format(k.decode("ascii", "replace"), v)
        for k, v in sorted(enc_kinds.items()))))
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  " + f)
        return 1
    print("\nthe C binary agrees with the Python decoder on every case")
    return 0


if __name__ == "__main__":
    sys.exit(main())
