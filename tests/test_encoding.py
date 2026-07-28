"""Text-encoding fidelity: read it right, or refuse -- never silently mangle.

The readers used to open every file with `errors="replace"`, which turns any
byte the codec does not recognise into U+FFFD. That is silent data destruction
in a compressor whose entire promise is that it does not alter data, and it
was invisible: the round-trip check compares the decoded table against the
table that was read, so a file corrupted *on the way in* verifies perfectly
and reports success. Measured before the fix, on six files whose true contents
were known:

    plain UTF-8                     OK
    UTF-8 with BOM (Excel)          CORRUPTED -- BOM became part of column 1's
                                    name: 'city' -> '﻿city'
    UTF-16 (Excel 'Unicode text')   refused, but as "line contains NUL"
    latin-1 / ISO-8859-1            CORRUPTED -- every accent lost, silently
    cp1252 (Windows)                CORRUPTED -- ditto
    UTF-8 + one illegal byte        CORRUPTED -- swallowed, silently

Three of those six destroyed data and said nothing. This suite pins the fixed
behaviour: a byte-order mark is honoured, unmarked files are decoded as strict
UTF-8, and anything else is REFUSED with a message naming the offending byte
and the flag that overrides it. Guessing an encoding is what corrupts files,
so nothing is guessed -- `--encoding` is how the user states what we decline
to assume.

    python3 tests/test_encoding.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polypress import dtz, fast, stream         # noqa: E402

TZIP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "tzip.py")

# Accented text is the whole point: it is exactly what a non-UTF-8 encoding
# gets wrong, and exactly what errors="replace" used to throw away.
COLS = ["city", "note"]
ROWS = [["Zürich", "café"],
        ["Malmö", "naïve"],
        ["München", "résumé"]]

_fails = []


def check(name: str, got, want) -> None:
    ok = got == want
    print("  {:52s} {}".format(name, "ok" if ok else "FAIL"))
    if not ok:
        _fails.append(name)
        print("      got  {!r}".format(got))
        print("      want {!r}".format(want))


def write(path: str, encoding: str, bom: bytes = b"") -> str:
    text = ",".join(COLS) + "\n" + "\n".join(",".join(r) for r in ROWS) + "\n"
    with open(path, "wb") as fh:
        fh.write(bom + text.encode(encoding))
    return path


# ------------------------------------------------- files we must read right

def test_readable(tmp: str) -> None:
    print("\nencodings that must be READ CORRECTLY")
    cases = [
        ("plain UTF-8", "utf8.csv", "utf-8", b""),
        # Excel's "CSV UTF-8" writes this. The BOM used to end up inside the
        # first column's NAME, which then silently mismatches every schema.
        ("UTF-8 with BOM", "utf8bom.csv", "utf-8", b"\xef\xbb\xbf"),
        # Excel's "Unicode text (*.txt)" export.
        ("UTF-16 (BOM, little endian)", "utf16le.csv", "utf-16-le",
         b"\xff\xfe"),
        ("UTF-16 (BOM, big endian)", "utf16be.csv", "utf-16-be", b"\xfe\xff"),
        ("UTF-32 (BOM, little endian)", "utf32le.csv", "utf-32-le",
         b"\xff\xfe\x00\x00"),
    ]
    for label, name, enc, bom in cases:
        path = write(os.path.join(tmp, name), enc, bom)
        t = dtz.read_any(path)
        check(label, (t.columns, t.rows), (COLS, ROWS))


# ----------------------------------------------------- files we must refuse

def test_refused(tmp: str) -> None:
    print("\nencodings that must be REFUSED, not guessed at")

    latin = write(os.path.join(tmp, "latin1.csv"), "latin-1")
    cp = write(os.path.join(tmp, "cp1252.csv"), "cp1252")

    # Valid UTF-8 except for one byte that is illegal in UTF-8 anywhere.
    stray = os.path.join(tmp, "stray.csv")
    text = ",".join(COLS) + "\n" + "\n".join(",".join(r) for r in ROWS) + "\n"
    raw = bytearray(text.encode("utf-8"))
    raw.insert(len(raw) // 2, 0xFF)
    with open(stray, "wb") as fh:
        fh.write(bytes(raw))

    for label, path in (("latin-1", latin), ("cp1252", cp),
                        ("UTF-8 with one illegal byte", stray)):
        try:
            dtz.read_any(path)
            check(label + " refused", "read without complaint", "refused")
        except dtz.EncodingRefused as exc:
            msg = str(exc)
            # The message has to carry the two things the user can act on.
            check(label + " refused, message names the byte",
                  ("0x" in msg, "--encoding" in msg), (True, True))
        except Exception as exc:                       # noqa: BLE001
            check(label + " refused with the right error",
                  type(exc).__name__, "EncodingRefused")


# ------------------------------------------- the override, and it must work

def test_override(tmp: str) -> None:
    print("\n--encoding names what we decline to guess")
    for label, enc in (("latin-1", "latin-1"), ("cp1252", "cp1252")):
        path = write(os.path.join(tmp, "ovr_" + enc + ".csv"), enc)
        t = dtz.read_any(path, enc)
        check(label + " read correctly when named", (t.columns, t.rows),
              (COLS, ROWS))
        # and it must survive the actual codec, not just the reader
        back = fast.decode(fast.encode(t))
        check(label + " round-trips through the codec",
              (back.columns, back.rows), (COLS, ROWS))


# ------------------------------------------------------ the other two paths

def test_json_paths(tmp: str) -> None:
    print("\nJSON and JSON Lines get the same treatment")
    import json
    recs = [dict(zip(COLS, r)) for r in ROWS]

    # ensure_ascii=False throughout, or json escapes every accent to ü
    # and the file comes out pure ASCII -- which is valid in every encoding
    # and tests nothing. (This bit the first version of this suite.)
    def dumps(obj):
        return json.dumps(obj, ensure_ascii=False)

    jp = os.path.join(tmp, "bom.json")
    with open(jp, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + dumps(recs).encode("utf-8"))
    t = dtz.read_any(jp)
    check("json with a BOM", (t.columns, t.rows), (COLS, ROWS))

    lp = os.path.join(tmp, "bom.jsonl")
    with open(lp, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + "\n".join(
            dumps(r) for r in recs).encode("utf-8"))
    t = dtz.read_any(lp)
    check("jsonl with a BOM", (t.columns, t.rows), (COLS, ROWS))

    bad = os.path.join(tmp, "bad.json")
    with open(bad, "wb") as fh:
        fh.write(dumps(recs).encode("latin-1"))
    try:
        dtz.read_any(bad)
        check("json in latin-1 refused", "read it", "refused")
    except dtz.EncodingRefused:
        check("json in latin-1 refused", "refused", "refused")


def test_stream_path(tmp: str) -> None:
    """stream.py reads block by block, so a bad byte surfaces mid-file."""
    print("\nthe block-at-a-time reader behaves identically")

    big = os.path.join(tmp, "big_latin1.csv")
    with open(big, "wb") as fh:
        fh.write((",".join(COLS) + "\n").encode("latin-1"))
        for i in range(4000):
            fh.write((",".join(ROWS[i % len(ROWS)]) + "\n").encode("latin-1"))

    dst = os.path.join(tmp, "big.ppz")
    try:
        stream.compress(big, dst, rows=500, verify=True)
        check("latin-1 refused by the streaming reader", "compressed it",
              "refused")
    except dtz.EncodingRefused:
        check("latin-1 refused by the streaming reader", "refused", "refused")

    stream.compress(big, dst, rows=500, verify=True, encoding="latin-1")
    out = os.path.join(tmp, "big_back.csv")
    stream.restore(dst, out)
    t = dtz.read_any(out)
    check("streamed latin-1 round-trips when named",
          (t.columns, len(t.rows), t.rows[0]), (COLS, 4000, ROWS[0]))


# --------------------------------------------------------------- at the CLI

def cli(*args) -> tuple:
    p = subprocess.run([sys.executable, TZIP] + list(args),
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def test_cli(tmp: str) -> None:
    print("\nat the command line: one clear line, never a traceback")
    path = write(os.path.join(tmp, "cli_latin1.csv"), "latin-1")

    code, out = cli("compress", path, "-o", os.path.join(tmp, "x.ppz"))
    check("refusal exits 1", code, 1)
    check("refusal is not a traceback", "Traceback" in out, False)
    check("refusal says what to do", "--encoding" in out, True)

    code, out = cli("compress", path, "--encoding", "latin-1",
                    "-o", os.path.join(tmp, "x.ppz"))
    check("--encoding at the CLI succeeds", code, 0)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        test_readable(tmp)
        test_refused(tmp)
        test_override(tmp)
        test_json_paths(tmp)
        test_stream_path(tmp)
        test_cli(tmp)
    print()
    if _fails:
        print("{} FAILED: {}".format(len(_fails), ", ".join(_fails)))
        return 1
    print("every encoding is read correctly or refused -- none is guessed at")
    return 0


if __name__ == "__main__":
    sys.exit(main())
