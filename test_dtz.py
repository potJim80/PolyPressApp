"""Fidelity tests for dtz. Every case must round-trip the logical table exactly.

These are the cases that break naive table tools: embedded delimiters and
newlines, quotes, unicode, ragged rows, empty values, duplicate headers,
degenerate shapes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Tuple

import dtz

CASES = {
    "simple": dtz.Table(["a", "b"], [["1", "2"], ["3", "4"]]),
    "embedded_comma": dtz.Table(
        ["name", "note"],
        [["Smith, John", "hello, world"], ["Doe, Jane", "a,b,c"]]),
    "embedded_quote": dtz.Table(
        ["q"], [['say "hi"'], ["it's \"fine\""], ['""']]),
    "embedded_newline": dtz.Table(
        ["text"], [["line1\nline2"], ["a\r\nb"], ["trailing\n"]]),
    "unicode": dtz.Table(
        ["city", "emoji"],
        [["Über", "\U0001f600"], ["北京", "☃"],
         ["Kraków", "✓"]]),
    "empty_values": dtz.Table(
        ["a", "b", "c"], [["", "", ""], ["1", "", "3"], ["", "2", ""]]),
    "duplicate_headers": dtz.Table(
        ["x", "x", "y"], [["1", "2", "3"], ["4", "5", "6"]]),
    "single_column": dtz.Table(["only"], [[str(i)] for i in range(50)]),
    "single_row": dtz.Table(["a", "b", "c"], [["1", "2", "3"]]),
    "no_rows": dtz.Table(["a", "b"], []),
    "one_cell": dtz.Table(["a"], [["x"]]),
    "wide_shallow": dtz.Table(
        ["c{}".format(i) for i in range(80)],
        [[str(i * j) for j in range(80)] for i in range(3)]),
    "negatives_decimals": dtz.Table(
        ["v"], [["-0.50"], ["0.00"], ["-12.75"], ["3.10"], ["-0.05"]]),
    "leading_zeros": dtz.Table(
        ["zip"], [["01234"], ["00501"], ["99950"]]),
    "big_ints": dtz.Table(
        ["n"], [[str(10 ** 30 + i)] for i in range(20)]),
    "mixed_types": dtz.Table(
        ["m"], [["1"], ["1.5"], ["abc"], [""], ["-2"], ["1e10"]]),
    "whitespace": dtz.Table(
        ["w"], [["  leading"], ["trailing  "], ["  both  "], ["\ttab"]]),
    "fd_present": dtz.Table(
        ["zip", "city", "state"],
        [["10001", "NYC", "NY"], ["10001", "NYC", "NY"],
         ["90210", "Beverly Hills", "CA"], ["90210", "Beverly Hills", "CA"],
         ["60601", "Chicago", "IL"]] * 20),
}


def check_strategies(name: str, table: dtz.Table) -> list:
    """Every strategy that succeeds must round-trip exactly."""
    failures = []
    for unordered in (False, True):
        attempts, blobs = dtz.try_all(table, unordered=unordered, verify=True)
        succeeded = [a for a in attempts if a.size is not None]
        if not succeeded:
            failures.append("{}: no strategy succeeded".format(name))
            continue
        for a in attempts:
            if a.size is None and "no functional dependencies" not in (a.error or ""):
                failures.append("{}: {} -> {}".format(name, a.name, a.error))
    return failures


def check_files(name: str, table: dtz.Table) -> Tuple[list, list]:
    """Write to each on-disk format, read back, compress, decompress.

    A format that genuinely cannot hold the table (duplicate keys in JSON or
    Parquet, headers for a zero-row jsonl) must raise FormatLimit rather than
    write something subtly wrong. That counts as correct behaviour, not a pass.
    """
    failures, limits = [], []
    exts = [".csv", ".tsv", ".json", ".jsonl"]
    if dtz.pa is not None:
        exts.append(".parquet")
    with tempfile.TemporaryDirectory() as tmp:
        for ext in exts:
            path = os.path.join(tmp, "t" + ext)
            try:
                dtz.write_any(table, path)
                loaded = dtz.read_any(path)
            except dtz.FormatLimit:
                limits.append(ext)
                continue
            except Exception as exc:
                failures.append("{}{}: io {}".format(name, ext, exc))
                continue
            if loaded.columns != table.columns or loaded.rows != table.rows:
                failures.append("{}{}: file io changed the table".format(
                    name, ext))
                continue
            try:
                blob, best, _ = dtz.compress(loaded)
                back = dtz.decompress(blob)
            except Exception as exc:
                failures.append("{}{}: compress {}".format(name, ext, exc))
                continue
            if back.columns != loaded.columns or back.rows != loaded.rows:
                failures.append("{}{}: dtz round trip mismatch".format(
                    name, ext))
    return failures, limits


def main() -> int:
    all_failures = []
    all_limits = []
    print("\ncase                      strategies  file formats  winner")
    print("-" * 68)
    for name, table in CASES.items():
        f1 = check_strategies(name, table)
        f2, limits = check_files(name, table)
        all_failures.extend(f1 + f2)
        if limits:
            all_limits.append((name, limits))
        try:
            _, best, _ = dtz.compress(table)
            winner = best.name
        except Exception as exc:
            winner = "FAILED: {}".format(exc)
        note = "ok" if not f2 else "FAIL"
        if limits and not f2:
            note = "ok ({} n/a)".format(len(limits))
        print("  {:<24} {:^10}  {:^12}  {}".format(
            name, "ok" if not f1 else "FAIL", note, winner))

    print()
    if all_limits:
        print("documented format limits (raised FormatLimit, wrote nothing):")
        for name, exts in all_limits:
            print("  {:<24} {}".format(name, " ".join(exts)))
        print()
    if all_failures:
        print("{} failure(s):".format(len(all_failures)))
        for f in all_failures:
            print("  - {}".format(f))
        return 1
    print("all {} cases round-trip exactly under every strategy, and in every "
          "format that can hold them".format(len(CASES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
