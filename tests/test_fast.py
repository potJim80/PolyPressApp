"""Fidelity tests for fast.py -- the codec that actually ships.

Reuses the awkward cases from test_dtz (embedded delimiters, newlines,
unicode, ragged shapes) and adds the ones that specifically stress this
codec's machinery: the numeric parser, 2D column grouping, parent-sorted
reordering, and the varint escape path.

    python3 test_fast.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from polypress import dtz, fast, caccel
from test_dtz import CASES as BASE_CASES

EXTRA = {
    # numeric edge cases -- these decide whether a column is parsed as a
    # scaled integer or falls back to text
    "leading_zeros": dtz.Table(["z"], [["007"], ["010"], ["000"]]),
    "negative_zero": dtz.Table(["z"], [["-0.0"], ["0.0"], ["-1.5"]]),
    "ragged_decimals": dtz.Table(["d"], [["1.5"], ["1.50"], ["1.500"]]),
    "big_ints": dtz.Table(
        ["n"], [[str(2 ** 62 - 1)], [str(-(2 ** 62) + 1)], ["0"]]),
    "huge_ints": dtz.Table(["n"], [["9" * 40], ["1"], ["2"]]),

    # The 2^62 acceptance boundary, from both sides and both signs. This is
    # where the C accelerator and the numpy reference used to disagree: the C
    # parser checked for overflow *after* multiplying, and signed overflow
    # wraps negative, so INT64_MIN sailed past a limit of 2^62. The
    # accelerator accepted it as numeric; numpy rejected it. Same file, two
    # different plans, depending only on whether a compiler was available.
    "int64_min": dtz.Table(["n"], [["-9223372036854775808"], ["1"], ["2"]]),
    "int64_max": dtz.Table(["n"], [["9223372036854775807"], ["1"], ["2"]]),
    "limit_edge": dtz.Table(
        ["lo", "hi"],
        [[str(-(2 ** 62) + 1), str(2 ** 62 - 1)],
         [str(2 ** 62), str(-(2 ** 62))],
         ["0", "1"]]),
    "mixed_sign": dtz.Table(
        ["v"], [["-0.01"], ["0.00"], ["0.01"], ["-99999.99"]]),
    "varint_escape": dtz.Table(
        ["v"], [[str(i * 7919)] for i in range(300)]),

    # 2D grouping: commensurable columns should be grouped, and the codec
    # must still round-trip when they are not
    "commensurable_2d": dtz.Table(
        ["a", "b", "c", "d"],
        [["{:.2f}".format(1.0 + i * 0.01 + j * 0.05) for j in range(4)]
         for i in range(200)]),
    "incommensurable": dtz.Table(
        ["year", "tiny", "huge"],
        [[str(2000 + i), "{:.3f}".format(i / 1000.0), str(i * 10 ** 9)]
         for i in range(120)]),

    # parent-sorted reordering: a child column strongly determined by a
    # parent, plus a tie-heavy parent to exercise the stable sort
    "parent_child": dtz.Table(
        ["zip", "city", "noise"],
        [["{:05d}".format(90000 + (i % 40)),
          "city{}".format(i % 40),
          "n{}".format(i % 7)] for i in range(500)]),
    "constant_parent": dtz.Table(
        ["k", "v"], [["same", "v{}".format(i % 3)] for i in range(200)]),
    "all_unique": dtz.Table(
        ["u"], [["value-{}".format(i)] for i in range(400)]),
    "single_distinct": dtz.Table(["s"], [["x"] for _ in range(100)]),

    # Numeric-with-exceptions. A column that is numeric apart from a few cells
    # used to be discarded to the dictionary path entirely, and on the
    # Treasury yield curve four blank cells in 72,048 cost 41% of the archive.
    # These force each way a cell can fail while the column stays numeric.
    "one_blank": dtz.Table(
        ["v"], [["" if i == 137 else "{:.2f}".format(1.0 + i * 0.01)]
                for i in range(400)]),
    "blank_first": dtz.Table(
        ["v"], [["" if i < 3 else str(i * 3)] for i in range(400)]),
    "blank_last": dtz.Table(
        ["v"], [["" if i >= 397 else str(i * 3)] for i in range(400)]),
    "minus_zero_rare": dtz.Table(
        ["v"], [["-0.0" if i == 200 else "{:.1f}".format(i * 0.5 - 100)]
                for i in range(400)]),
    "stray_decimals": dtz.Table(
        ["v"], [["{:.3f}".format(i) if i == 55 else "{:.2f}".format(i)]
                for i in range(400)]),
    "leading_zero_rare": dtz.Table(
        ["v"], [["007" if i == 9 else str(i)] for i in range(400)]),
    # Just past the 5% screen, so the lenient path must decline it and the
    # column must stay a dictionary column.
    "too_many_blanks": dtz.Table(
        ["v"], [["" if i % 10 == 0 else str(i)] for i in range(400)]),
    # Exceptions inside a commensurable group: the group is rebuilt first and
    # the exceptions land afterwards, which is a different code path in both
    # implementations.
    "grouped_with_blanks": dtz.Table(
        ["a", "b", "c"],
        [["" if (i == 100 and j == 1) else "{:.2f}".format(1.0 + i * 0.01
                                                           + j * 0.05)
          for j in range(3)] for i in range(200)]),
    # A value past the 2^62 acceptance limit is an exception, not a refusal of
    # the whole column -- and Python's arbitrary-precision ints must agree
    # with the C parser about that.
    "over_limit_rare": dtz.Table(
        ["v"], [[str(2 ** 63) if i == 42 else str(i)] for i in range(400)]),

    # Front-coding, per group. A monotonic text column shares almost every
    # character with its predecessor, which is where this pays -- one real
    # timestamp column went 9,438 bytes to 434. The reconstruction is a prefix
    # length plus a remainder, so anything that makes the prefix ambiguous or
    # the arithmetic wrong shows up as a round-trip failure here.
    "monotonic_text": dtz.Table(
        ["t"], [["2015-01-{:02d}T{:02d}:{:02d}:00".format(
            1 + i // 1440, (i // 60) % 24, i % 60)] for i in range(3000)]),
    # Prefix longer than 255 bytes, which is where the one-byte cap bites.
    "long_shared_prefix": dtz.Table(
        ["t"], [["Z" * 400 + "{:04d}".format(i)] for i in range(500)]),
    # A non-ASCII shared prefix. Python counts BYTES like C does; counting
    # characters would give a different archive for exactly this table.
    "unicode_prefix": dtz.Table(
        ["t"], [["éü中文-{:05d}".format(i)]
                for i in range(600)]),
    # Front-coding must be refused on a group containing a newline, because the
    # remainders are newline-joined and could not be split back apart.
    "newline_in_sorted_text": dtz.Table(
        ["t"], [["pre-{:04d}\nsuffix".format(i)] for i in range(400)]),
    # Empty and one-character values around the front-coded path.
    "degenerate_text": dtz.Table(
        ["t"], [[""], ["a"], ["a"], ["ab"], [""], ["b"]] * 80),
    # Two monotonic columns, so the single-candidate cap has to choose, and the
    # choice must be the same in both implementations.
    "two_monotonic": dtz.Table(
        ["a", "b"],
        [["2020-01-01T{:02d}:{:02d}".format(i // 60, i % 60),
          "id-{:07d}-tail".format(i * 3)] for i in range(2000)]),
}


def check(name: str, table) -> bool:
    try:
        blob = fast.encode(table)
        back = fast.decode(blob)
    except Exception as exc:
        print("  {:<22} RAISED {}: {}".format(name, type(exc).__name__, exc))
        return False
    if back.columns != table.columns:
        print("  {:<22} COLUMN MISMATCH".format(name))
        return False
    if back.rows != table.rows:
        print("  {:<22} ROW MISMATCH".format(name))
        for i, (a, b) in enumerate(zip(table.rows, back.rows)):
            if a != b:
                print("      row {}: want {!r}".format(i, a))
                print("             got  {!r}".format(b))
                break
        else:
            print("      row count {} vs {}".format(
                len(table.rows), len(back.rows)))
        return False
    return True


def check_fallback() -> list:
    """The fallback container: correctness first, then that it earns its place.

    encode() now returns whichever is smaller of the modelled encoding and the
    whole table under a plain standard codec. That introduces two new archive
    shapes and a new way to be wrong -- picking a fallback that does not
    actually round-trip -- so each is checked directly rather than inferred
    from the size going down.
    """
    import random
    import string
    bad = []
    rnd = random.Random(4)
    al = string.ascii_letters + string.digits

    # unstructured: no parent survives, no column differences usefully, no
    # commensurable group. This is the shape that must reach the fallback.
    noise = dtz.Table(
        ["a", "b"],
        [["".join(rnd.choice(al) for _ in range(24)),
          "".join(rnd.choice(al) for _ in range(24))] for _ in range(3000)])

    blob, fired, _ngroups, _nlax = fast._encode_plan(noise)
    if fired:
        bad.append("noise table fired {} tricks; expected 0".format(fired))
    chosen = fast.encode(noise)
    if len(chosen) > len(blob):
        bad.append("encode() returned {} B, worse than the plan's {} B"
                   .format(len(chosen), len(blob)))
    if fast.decode(chosen).rows != noise.rows:
        bad.append("chosen encoding did not round-trip")

    # both fallback containers must decode, whether or not encode picks them
    for magic, name in ((fast.MAGIC_RAW_XZ, "xz"), (fast.MAGIC_RAW_BZ, "bz2")):
        import bz2 as _bz2
        import lzma as _lzma
        canon = fast._canonical_bytes(noise)
        body = (_lzma.compress(canon, **fast.XZ) if magic == fast.MAGIC_RAW_XZ
                else _bz2.compress(canon, 9))
        back = fast.decode(magic + body)
        if back.rows != noise.rows or back.columns != noise.columns:
            bad.append("{} fallback container did not round-trip".format(name))

    # cells that stress the canonical CSV round trip, since a fallback that
    # corrupts data is worse than losing on size
    nasty = dtz.Table(
        ["t", "u"],
        [['has,comma', 'has"quote'], ['has\nnewline', 'has\ttab'],
         ['', '   '], ['éü中文', 'trailing '], ['\r', 'a\r\nb']])
    if fast._canonical_bytes(nasty) and \
            fast._from_canonical(fast._canonical_bytes(nasty)).rows != nasty.rows:
        # not a failure in itself -- _raw_candidates must simply refuse it
        if fast._raw_candidates(nasty, 1 << 30) is not None:
            bad.append("offered a fallback for cells it cannot round-trip")
    if fast.decode(fast.encode(nasty)).rows != nasty.rows:
        bad.append("nasty-cell table did not round-trip")

    # a structured table must NOT pay for the fallback, and must be unchanged
    structured = dtz.Table(
        ["zip", "city"],
        [[["98101", "98402", "98501"][i % 3],
          ["Seattle", "Tacoma", "Olympia"][i % 3]] for i in range(2000)])
    sblob, sfired, _sngroups, _snlax = fast._encode_plan(structured)
    if not sfired:
        bad.append("structured table fired no tricks")
    if fast.encode(structured) != sblob:
        bad.append("structured table was diverted to a fallback")
    return bad


def main() -> int:
    cases = dict(BASE_CASES)
    cases.update(EXTRA)
    print("C acceleration: {}".format(
        "on" if caccel.HAVE_C else "off (numpy fallback)"))
    print("{} cases\n".format(len(cases)))

    failed = [n for n, t in sorted(cases.items()) if not check(n, t)]

    # The numpy fallback must agree with the C path, or the accelerator is
    # not an accelerator -- it is a second, different codec.
    if caccel.HAVE_C:
        caccel.HAVE_C = False
        try:
            fallback = [n for n, t in sorted(cases.items()) if not check(n, t)]
        finally:
            caccel.HAVE_C = True
        if fallback:
            print("\nnumpy-fallback failures: {}".format(fallback))
            failed += ["fallback:" + n for n in fallback]
        else:
            print("numpy fallback agrees on all cases")

    fb = check_fallback()
    if fb:
        print("\nfallback failures:")
        for f in fb:
            print("  " + f)
        failed += ["fallback-container"]
    else:
        print("fallback container: correct, and only used when it wins")

    if failed:
        print("\nFAILED {} of {}: {}".format(
            len(failed), len(cases) * 2, ", ".join(failed)))
        return 1
    print("\nall cases round-trip exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
