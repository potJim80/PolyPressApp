"""Fidelity tests for stream.py -- the bounded-memory block codec.

stream.py had no tests at all, and the three bugs it shipped with were exactly
the kind a round-trip suite catches:

  * a trailing block was emitted unconditionally, so a row count that divided
    evenly by the block size paid for an extra empty block
  * restore ignored the output extension and always wrote CSV, so
    `-o out.parquet` produced a CSV that pyarrow refuses to open
  * the module docstring documented an invocation that ImportErrors

So the checks here are: block *count* is exactly what the arithmetic says,
every output format is really that format when read back by its own reader,
and the table survives regardless of where the block boundaries fall.

Block boundaries are the interesting axis. Every case is run at several
rows-per-block, including 1 and a value larger than the table, because the
cross-column reordering is per-block and boundaries are where a block codec
breaks.

    python3 tests/test_stream.py
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from polypress import dtz, stream

try:
    import pyarrow.parquet as pq
except ImportError:
    pq = None


# ------------------------------------------------------------------- cases

def _rows(n, fn):
    return [fn(i) for i in range(n)]


CASES = {
    # the shape the reordering is built for: a child column determined by a
    # parent, which only collapses if both land in the same block
    "parent_child": dtz.Table(
        ["zip", "city", "n"],
        _rows(600, lambda i: [["98101", "98402", "98501"][i % 3],
                              ["Seattle", "Tacoma", "Olympia"][i % 3],
                              str(i)])),

    # commensurable numeric columns -> 2D grouping inside each block
    "numeric_2d": dtz.Table(
        ["a", "b", "c"],
        _rows(400, lambda i: ["{:.2f}".format(1.0 + i * 0.01 + j * 0.05)
                              for j in range(3)])),

    # awkward cells: embedded delimiters, quotes, newlines, unicode
    "awkward": dtz.Table(
        ["text", "n"],
        _rows(150, lambda i: ['a,b"c\nd éü中文 {}'.format(i),
                              str(i)])),

    # empty strings and whitespace, which the numeric parser must reject
    "empties": dtz.Table(
        ["a", "b"],
        _rows(120, lambda i: ["" if i % 3 == 0 else "  ", str(i)])),

    # single row, and a table narrower than any grouping rule
    "single_row": dtz.Table(["x", "y"], [["1", "2"]]),

    # header but no data rows -- the one case that legitimately needs a block
    "no_rows": dtz.Table(["a", "b"], []),
}

# rows-per-block values; 600 exercises "block size divides row count exactly",
# which is the case that produced the spurious empty block
BLOCKS = [1, 7, 100, 600, 10_000]


def _write_csv(table, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(table.columns)
        w.writerows(table.rows)


def _read_back(path, fmt):
    """Read an output file using that format's own reader, not ours."""
    if fmt in (".csv", ".tsv"):
        delim = "\t" if fmt == ".tsv" else ","
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh, delimiter=delim))
        return dtz.Table(rows[0], rows[1:]) if rows else dtz.Table([], [])
    if fmt == ".jsonl":
        recs = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        cols = list(recs[0].keys())
        return dtz.Table(cols, [[r[c] for c in cols] for r in recs])
    if fmt == ".json":
        d = json.load(open(path, encoding="utf-8"))
        if isinstance(d, dict):
            return dtz.Table(list(d.keys()), [])
        cols = list(d[0].keys())
        return dtz.Table(cols, [[r[c] for c in cols] for r in d])
    if fmt == ".parquet":
        t = pq.read_table(path)
        cols = list(t.column_names)
        d = t.to_pydict()
        return dtz.Table(cols, [list(r) for r in zip(*[d[c] for c in cols])])
    raise AssertionError(fmt)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ppz-stream-")
    failures = []
    checked = 0
    try:
        for name, table in sorted(CASES.items()):
            src = os.path.join(tmp, name + ".csv")
            _write_csv(table, src)

            for nrows in BLOCKS:
                arc = os.path.join(tmp, "{}-{}.ppz".format(name, nrows))
                stream.compress(src, arc, rows=nrows, verify=True)

                # block count must be exactly ceil(rows/nrows), with a floor of
                # 1 so a header-only table still records its columns
                want = max(1, -(-len(table.rows) // nrows))
                got = len(stream.info(arc)["blocks"])
                checked += 1
                if got != want:
                    failures.append(
                        "{} @{}: {} blocks, expected {}".format(
                            name, nrows, got, want))

                for fmt in (".csv", ".tsv", ".jsonl", ".json", ".parquet"):
                    if fmt == ".parquet" and pq is None:
                        continue
                    dst = os.path.join(tmp, "{}-{}{}".format(name, nrows, fmt))
                    try:
                        stream.restore(arc, dst)
                    except dtz.FormatLimit:
                        # documented limit; it must not leave a file behind
                        checked += 1
                        if os.path.exists(dst):
                            failures.append(
                                "{} @{} {}: FormatLimit but wrote a file"
                                .format(name, nrows, fmt))
                        continue
                    back = _read_back(dst, fmt)
                    checked += 1
                    if back.columns != table.columns or back.rows != table.rows:
                        failures.append("{} @{} {}: round trip differs"
                                        .format(name, nrows, fmt))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("{} checks across {} cases x {} block sizes".format(
        checked, len(CASES), len(BLOCKS)))
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  " + f)
        return 1
    print("\nblock counts exact, and every output format round-trips "
          "under its own reader")
    return 0


if __name__ == "__main__":
    sys.exit(main())
