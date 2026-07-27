"""dtz -- a table compressor that picks the best available strategy.

Reads a table in whatever form you have it, tries every encoding validated in
this project plus the standard general-purpose compressors, keeps whichever
produced the smallest output, and writes a self-describing container.

The point is not a new compression breakthrough. It is that the standard tools
are among the candidates, so the result is never worse than the best of them,
and is sometimes meaningfully better.

Fidelity contract
-----------------
dtz preserves the LOGICAL table exactly: column names, column order, row
order, and every cell as an exact string. It does not promise to reproduce an
input file byte-for-byte, because CSV quoting and line endings are not
canonical. Every compress verifies the round trip in memory and refuses to
write output if it does not match.

Row order
---------
Preserved by default. Sorting the rows compresses substantially better, but it
destroys information, so it is only attempted with --unordered, which is you
declaring that the table is a set of records rather than a sequence.

Usage
-----
    python3 dtz.py compress   data.csv -o data.dtz [--unordered]
    python3 dtz.py decompress data.dtz -o out.csv
    python3 dtz.py inspect    data.dtz
    python3 dtz.py bench      data.csv [--unordered]

Input formats:  .csv .tsv .psv .txt .json .jsonl .ndjson .parquet
Output formats: .csv .tsv .json .jsonl .parquet
"""

from __future__ import annotations

import argparse
import bz2
import csv
import datetime
import decimal
import io
import json
import lzma
import os
import struct
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import codec  # the polynomial-predictor bit codec from this project

MAGIC = b"DTZ1"
XZ_FILTERS = None
XZ_PRESET = 9 | lzma.PRESET_EXTREME

try:
    import zstandard as zstd
except ImportError:
    zstd = None

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = None
    pq = None


# ============================================================== the table

@dataclass
class Table:
    """A table as exact cell strings. Everything round-trips through this."""
    columns: List[str]
    rows: List[List[str]]

    @property
    def shape(self) -> Tuple[int, int]:
        return len(self.rows), len(self.columns)

    def column(self, index: int) -> List[str]:
        return [row[index] for row in self.rows]

    def normalise(self) -> "Table":
        """Pad or trim ragged rows so every row matches the header width."""
        width = len(self.columns)
        fixed = []
        for row in self.rows:
            if len(row) < width:
                row = row + [""] * (width - len(row))
            elif len(row) > width:
                row = row[:width]
            fixed.append([("" if c is None else str(c)) for c in row])
        return Table(list(self.columns), fixed)


# ================================================================ readers

EXT_DELIMITER = {".csv": ",", ".tsv": "\t", ".psv": "|"}


def _sniff_delimiter(sample: str) -> str:
    """Only used for extensions that do not name their delimiter.

    Sniffs the header line alone: a tab or semicolon inside a data value must
    not be mistaken for the delimiter.
    """
    header = sample.split("\n", 1)[0]
    try:
        return csv.Sniffer().sniff(header, delimiters=",\t;|").delimiter
    except csv.Error:
        counts = {d: header.count(d) for d in ",\t;|"}
        return max(counts, key=counts.get) if max(counts.values()) else ","


def read_delimited(path: str) -> Table:
    ext = os.path.splitext(path)[1].lower()
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        delimiter = EXT_DELIMITER.get(ext)
        if delimiter is None:
            delimiter = _sniff_delimiter(fh.read(65536))
            fh.seek(0)
        rows = list(csv.reader(fh, delimiter=delimiter))
    if not rows:
        return Table([], [])
    return Table(rows[0], rows[1:]).normalise()


def _from_records(records: Sequence[dict]) -> Table:
    columns: List[str] = []
    seen = set()
    for rec in records:
        for key in rec:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    rows = []
    for rec in records:
        rows.append(["" if rec.get(c) is None else _scalar(rec.get(c))
                     for c in columns])
    return Table(columns, rows).normalise()


def _scalar(value) -> str:
    """Render any scalar a reader might hand us as an exact string.

    Parquet columns can be timestamps, dates, decimals or binary, none of
    which json.dumps will touch, so they are handled before the fallback.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    try:
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    except TypeError:
        return str(value)


def read_json(path: str) -> Table:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        # column-oriented {name: [values]}
        columns = list(data.keys())
        n = max((len(v) for v in data.values()), default=0)
        rows = [[_scalar(data[c][i]) if i < len(data[c]) else ""
                 for c in columns] for i in range(n)]
        return Table(columns, rows).normalise()
    return _from_records(data)


def read_jsonl(path: str) -> Table:
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return _from_records(records)


def read_parquet(path: str) -> Table:
    if pq is None:
        raise RuntimeError("reading parquet needs pyarrow installed")
    table = pq.read_table(path)
    columns = list(table.column_names)
    data = {c: table.column(c).to_pylist() for c in columns}
    n = table.num_rows
    rows = [["" if data[c][i] is None else _scalar(data[c][i])
             for c in columns] for i in range(n)]
    return Table(columns, rows).normalise()


READERS = {
    ".csv": read_delimited, ".tsv": read_delimited, ".psv": read_delimited,
    ".txt": read_delimited, ".dat": read_delimited,
    ".json": read_json,
    ".jsonl": read_jsonl, ".ndjson": read_jsonl,
    ".parquet": read_parquet, ".pq": read_parquet,
}


def read_any(path: str) -> Table:
    ext = os.path.splitext(path)[1].lower()
    reader = READERS.get(ext)
    if reader is None:
        # unknown extension: try delimited, it covers most real files
        reader = read_delimited
    return reader(path)


# ================================================================ writers

def write_delimited(table: Table, path: str, delimiter: str = ",") -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=delimiter, lineterminator="\n")
        writer.writerow(table.columns)
        writer.writerows(table.rows)


class FormatLimit(Exception):
    """The requested OUTPUT format cannot represent this table.

    Raised instead of writing something subtly wrong. Neither the .dtz
    container nor the delimited formats have these limits, so there is always
    a lossless option available.
    """


def _require_unique_columns(table: Table, fmt: str) -> None:
    seen = set()
    for name in table.columns:
        if name in seen:
            raise FormatLimit(
                "{} cannot represent the duplicate column name {!r}; "
                "write .csv or .tsv instead".format(fmt, name))
        seen.add(name)


def write_json(table: Table, path: str) -> None:
    _require_unique_columns(table, "json")
    with open(path, "w", encoding="utf-8") as fh:
        if not table.rows:
            # a list of records cannot carry headers; use the column form,
            # which read_json also understands
            json.dump({c: [] for c in table.columns}, fh, ensure_ascii=False)
            return
        records = [dict(zip(table.columns, row)) for row in table.rows]
        json.dump(records, fh, ensure_ascii=False, indent=1)


def write_jsonl(table: Table, path: str) -> None:
    _require_unique_columns(table, "jsonl")
    if not table.rows and table.columns:
        raise FormatLimit(
            "jsonl cannot carry column names for a zero-row table; "
            "write .csv, .json or .parquet instead")
    with open(path, "w", encoding="utf-8") as fh:
        for row in table.rows:
            fh.write(json.dumps(dict(zip(table.columns, row)),
                                ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")


def write_parquet_out(table: Table, path: str) -> None:
    if pq is None:
        raise RuntimeError("writing parquet needs pyarrow installed")
    _require_unique_columns(table, "parquet")
    arrays = {c: pa.array(table.column(i))
              for i, c in enumerate(table.columns)}
    pq.write_table(pa.table(arrays), path, compression="zstd")


def write_any(table: Table, path: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".tsv",):
        write_delimited(table, path, "\t")
    elif ext == ".json":
        write_json(table, path)
    elif ext in (".jsonl", ".ndjson"):
        write_jsonl(table, path)
    elif ext in (".parquet", ".pq"):
        write_parquet_out(table, path)
    else:
        write_delimited(table, path, ",")


# ================================================== canonical serialisation

def canonical_csv(table: Table) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(table.columns)
    writer.writerows(table.rows)
    return buf.getvalue().encode("utf-8")


def parse_canonical_csv(data: bytes) -> Table:
    text = data.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return Table([], [])
    return Table(rows[0], rows[1:])


def _xz(data: bytes) -> bytes:
    return lzma.compress(data, preset=XZ_PRESET)


def _unxz(data: bytes) -> bytes:
    return lzma.decompress(data)


def _zstd(data: bytes) -> bytes:
    if zstd is None:
        return _xz(data)
    return zstd.ZstdCompressor(level=22, write_content_size=True).compress(data)


def _unzstd(data: bytes) -> bytes:
    if zstd is None:
        return _unxz(data)
    return zstd.ZstdDecompressor().decompress(data)


# ============================================== typed column serialisation

def _pack_blobs(blobs: Sequence[bytes]) -> bytes:
    out = [struct.pack("<I", len(blobs))]
    for b in blobs:
        out.append(struct.pack("<Q", len(b)))
        out.append(b)
    return b"".join(out)


def _unpack_blobs(data: bytes) -> List[bytes]:
    (count,), pos = struct.unpack_from("<I", data, 0), 4
    blobs = []
    for _ in range(count):
        (length,) = struct.unpack_from("<Q", data, pos)
        pos += 8
        blobs.append(data[pos:pos + length])
        pos += length
    return blobs


INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1


def _arrow_columns(table: Table) -> Tuple[Dict[str, list], Dict[str, int]]:
    """Represent each column as ints+scale when that round-trips exactly,
    else as strings. Gives Parquet integer columns to work with while keeping
    the fidelity contract.

    Values outside int64 stay strings: arrow has no arbitrary-precision int,
    and silently widening would lose digits.
    """
    arrays: Dict[str, list] = {}
    scales: Dict[str, int] = {}
    for i, name in enumerate(table.columns):
        cells = table.column(i)
        numeric = codec.as_numeric_column(cells) if cells else None
        key = "c{}".format(i)
        if numeric is not None:
            ints, decimals = numeric
            if all(INT64_MIN <= v <= INT64_MAX for v in ints):
                arrays[key] = ints
                scales[key] = decimals
                continue
        arrays[key] = cells
    return arrays, scales


def _restore_columns(arrays: Dict[str, list], scales: Dict[str, int],
                     names: Sequence[str]) -> Table:
    cols = []
    for i in range(len(names)):
        key = "c{}".format(i)
        values = arrays[key]
        if key in scales:
            decimals = scales[key]
            cols.append([codec.int_to_cell(int(v), decimals) for v in values])
        else:
            cols.append(["" if v is None else v for v in values])
    n = len(cols[0]) if cols else 0
    rows = [[cols[j][i] for j in range(len(cols))] for i in range(n)]
    return Table(list(names), rows)


def _write_parquet_bytes(arrays: Dict[str, list], level: int = 22) -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.table({k: pa.array(v) for k, v in arrays.items()}), buf,
                   compression="zstd", compression_level=level,
                   use_dictionary=True)
    return buf.getvalue()


def _read_parquet_bytes(data: bytes) -> Dict[str, list]:
    table = pq.read_table(io.BytesIO(data))
    return {c: table.column(c).to_pylist() for c in table.column_names}


# ============================================================= strategies
#
# Each strategy is (encode, decode). encode returns (payload, meta).
# `ordered` False means the strategy does not preserve row order and is only
# offered when the user passes --unordered.

@dataclass
class Strategy:
    name: str
    encode: Callable[[Table], Tuple[bytes, dict]]
    decode: Callable[[bytes, dict], Table]
    ordered: bool = True
    needs_arrow: bool = False


# ---- 1. canonical CSV through a general compressor ----------------------

def _enc_csv_xz(t): return _xz(canonical_csv(t)), {}
def _dec_csv_xz(p, m): return parse_canonical_csv(_unxz(p))
def _enc_csv_zstd(t): return _zstd(canonical_csv(t)), {}
def _dec_csv_zstd(p, m): return parse_canonical_csv(_unzstd(p))
def _enc_csv_bz2(t): return bz2.compress(canonical_csv(t), 9), {}
def _dec_csv_bz2(p, m): return parse_canonical_csv(bz2.decompress(p))


# ---- 2. column-major text ----------------------------------------------

SEP = "\x1f"
ROW = "\x1e"


def _enc_colmajor(table: Table):
    parts = [SEP.join(table.columns)]
    for i in range(len(table.columns)):
        parts.append(SEP.join(table.column(i)))
    return _xz(ROW.join(parts).encode("utf-8")), {"names": table.columns}


def _dec_colmajor(payload: bytes, meta: dict) -> Table:
    parts = _unxz(payload).decode("utf-8").split(ROW)
    names = parts[0].split(SEP) if parts[0] else []
    cols = [p.split(SEP) if p else [] for p in parts[1:]]
    n = len(cols[0]) if cols else 0
    rows = [[cols[j][i] for j in range(len(cols))] for i in range(n)]
    return Table(names, rows)


# ---- 3. polynomial predictor codec (this project's codec.py) -----------

def _enc_poly(table: Table):
    bw = codec.BitWriter()
    bw.uvarint(len(table.rows))
    bw.uvarint(len(table.columns))
    for i in range(len(table.columns)):
        cells = table.column(i)
        numeric = codec.as_numeric_column(cells) if cells else None
        if numeric is None:
            bw.bit(0)
            for cell in cells:
                raw = cell.encode("utf-8")
                bw.uvarint(len(raw))
                for byte in raw:
                    bw.bits(byte, 8)
        else:
            ints, decimals = numeric
            order = codec.choose_order(ints)
            bw.bit(1)
            bw.bits(decimals, 6)
            bw.bits(order, 3)
            for value in ints[:order]:
                bw.svarint(value)
            for block in codec._blocks(codec.residuals(ints, order)):
                k, _ = codec._best_k(block)
                bw.bits(k, codec.K_BITS)
                for u in block:
                    bw.rice(u, k)
    return bw.bytes_out(), {"names": table.columns}


def _dec_poly(payload: bytes, meta: dict) -> Table:
    br = codec.BitReader(payload)
    nrows = br.uvarint()
    ncols = br.uvarint()
    cols = []
    for _ in range(ncols):
        if br.bit() == 0:
            cells = []
            for _ in range(nrows):
                n = br.uvarint()
                cells.append(bytes(br.bits(8) for _ in range(n)).decode("utf-8"))
            cols.append(cells)
        else:
            decimals = br.bits(6)
            order = br.bits(3)
            warmup = [br.svarint() for _ in range(order)]
            res, remaining = [], nrows - order
            while remaining > 0:
                k = br.bits(codec.K_BITS)
                take = min(codec.BLOCK, remaining)
                for _ in range(take):
                    res.append(codec.unzigzag(br.rice(k)))
                remaining -= take
            ints = codec.restore(warmup, res, order, nrows)
            cols.append([codec.int_to_cell(v, decimals) for v in ints])
    rows = [[cols[j][i] for j in range(ncols)] for i in range(nrows)]
    return Table(list(meta["names"]), rows)


def _enc_poly_xz(table: Table):
    payload, meta = _enc_poly(table)
    return _xz(payload), meta


def _dec_poly_xz(payload: bytes, meta: dict) -> Table:
    return _dec_poly(_unxz(payload), meta)


# ---- 4. typed Parquet + zstd ------------------------------------------

def _enc_parquet(table: Table):
    arrays, scales = _arrow_columns(table)
    return _write_parquet_bytes(arrays), {"names": table.columns,
                                          "scales": scales}


def _dec_parquet(payload: bytes, meta: dict) -> Table:
    arrays = _read_parquet_bytes(payload)
    return _restore_columns(arrays, {k: int(v) for k, v
                                     in meta["scales"].items()},
                            meta["names"])


# ---- 5. functional-dependency normalisation ---------------------------

def _find_dependencies(table: Table) -> Dict[int, int]:
    """Map dependent column -> determinant column, for exact FDs only.

    Picks the lowest-cardinality determinant, never chains onto a column that
    has itself been moved, and breaks 1:1 ties deterministically.
    """
    n_cols = len(table.columns)
    if not table.rows or n_cols < 2:
        return {}
    cols = [table.column(i) for i in range(n_cols)]
    cards = [len(set(c)) for c in cols]
    order = sorted(range(n_cols), key=lambda i: cards[i])

    owner: Dict[int, int] = {}
    for b in order:
        if cards[b] <= 1:
            continue                      # constant: general codecs handle it
        for a in order:
            if a == b or a in owner:
                continue
            if cards[a] > cards[b]:
                continue
            if cards[a] == cards[b] and a > b:
                continue
            if cards[a] == len(table.rows):
                continue                  # a unique key determines everything
            mapping, ok = {}, True
            for x, y in zip(cols[a], cols[b]):
                prev = mapping.get(x, y)
                if prev != y:
                    ok = False
                    break
                mapping[x] = y
            if ok:
                owner[b] = a
                break
    return owner


def _enc_fd(table: Table):
    owner = _find_dependencies(table)
    if not owner:
        raise ValueError("no functional dependencies found")

    kept = [i for i in range(len(table.columns)) if i not in owner]
    main = Table([table.columns[i] for i in kept],
                 [[row[i] for i in kept] for row in table.rows])
    main_arrays, main_scales = _arrow_columns(main)
    blobs = [_write_parquet_bytes(main_arrays)]

    groups: Dict[int, List[int]] = {}
    for dep, det in owner.items():
        groups.setdefault(det, []).append(dep)

    dims = []
    for det, deps in sorted(groups.items()):
        seen: Dict[str, List[str]] = {}
        det_col = table.column(det)
        dep_cols = [table.column(d) for d in deps]
        for i, keyval in enumerate(det_col):
            if keyval not in seen:
                seen[keyval] = [c[i] for c in dep_cols]
        dim = Table(["k"] + ["v{}".format(d) for d in deps],
                    [[k] + v for k, v in seen.items()])
        dim_arrays, dim_scales = _arrow_columns(dim)
        blobs.append(_write_parquet_bytes(dim_arrays))
        dims.append({"det": det, "deps": deps, "scales": dim_scales,
                     "names": dim.columns})

    meta = {"names": table.columns, "kept": kept, "main_scales": main_scales,
            "dims": dims}
    return _pack_blobs(blobs), meta


def _dec_fd(payload: bytes, meta: dict) -> Table:
    blobs = _unpack_blobs(payload)
    kept = list(meta["kept"])
    main = _restore_columns(_read_parquet_bytes(blobs[0]),
                            {k: int(v) for k, v in meta["main_scales"].items()},
                            [meta["names"][i] for i in kept])
    n_rows = len(main.rows)
    total = len(meta["names"])
    out: List[List[Optional[str]]] = [[None] * total for _ in range(n_rows)]
    for pos, col in enumerate(kept):
        for r in range(n_rows):
            out[r][col] = main.rows[r][pos]

    for blob, spec in zip(blobs[1:], meta["dims"]):
        dim = _restore_columns(_read_parquet_bytes(blob),
                               {k: int(v) for k, v in spec["scales"].items()},
                               spec["names"])
        lookup = {row[0]: row[1:] for row in dim.rows}
        det = spec["det"]
        deps = spec["deps"]
        for r in range(n_rows):
            values = lookup[out[r][det]]
            for j, d in enumerate(deps):
                out[r][d] = values[j]

    return Table(list(meta["names"]), [[c for c in row] for row in out])


# ---- 6. order-free variants (only with --unordered) -------------------

def _enc_sorted_parquet(table: Table):
    """Sort rows to expose structure. Discards the original row order."""
    rows = sorted(table.rows)
    payload, meta = _enc_parquet(Table(table.columns, rows))
    meta["sorted"] = True
    return payload, meta


def _enc_sorted_fd(table: Table):
    rows = sorted(table.rows)
    payload, meta = _enc_fd(Table(table.columns, rows))
    meta["sorted"] = True
    return payload, meta


def build_strategies() -> List[Strategy]:
    have_arrow = pa is not None
    strategies = [
        Strategy("csv.xz", _enc_csv_xz, _dec_csv_xz),
        Strategy("csv.zstd", _enc_csv_zstd, _dec_csv_zstd),
        Strategy("csv.bz2", _enc_csv_bz2, _dec_csv_bz2),
        Strategy("colmajor.xz", _enc_colmajor, _dec_colmajor),
        Strategy("poly", _enc_poly, _dec_poly),
        Strategy("poly.xz", _enc_poly_xz, _dec_poly_xz),
    ]
    if have_arrow:
        strategies += [
            Strategy("parquet.zstd", _enc_parquet, _dec_parquet,
                     needs_arrow=True),
            Strategy("fd.parquet.zstd", _enc_fd, _dec_fd, needs_arrow=True),
            Strategy("sorted.parquet.zstd", _enc_sorted_parquet, _dec_parquet,
                     ordered=False, needs_arrow=True),
            Strategy("sorted.fd.parquet.zstd", _enc_sorted_fd, _dec_fd,
                     ordered=False, needs_arrow=True),
        ]
    return strategies


# ============================================================== container
#
# DTZ1 | u16 name length | name | u32 meta length | xz(json meta) | payload

def pack(name: str, meta: dict, payload: bytes) -> bytes:
    name_b = name.encode("utf-8")
    meta_b = _xz(json.dumps(meta, separators=(",", ":")).encode("utf-8"))
    return b"".join([MAGIC,
                     struct.pack("<H", len(name_b)), name_b,
                     struct.pack("<I", len(meta_b)), meta_b,
                     payload])


def unpack(blob: bytes) -> Tuple[str, dict, bytes]:
    if blob[:4] != MAGIC:
        raise ValueError("not a dtz file (bad magic)")
    pos = 4
    (name_len,) = struct.unpack_from("<H", blob, pos)
    pos += 2
    name = blob[pos:pos + name_len].decode("utf-8")
    pos += name_len
    (meta_len,) = struct.unpack_from("<I", blob, pos)
    pos += 4
    meta = json.loads(_unxz(blob[pos:pos + meta_len]).decode("utf-8"))
    pos += meta_len
    return name, meta, blob[pos:]


# ============================================================ top level

@dataclass
class Attempt:
    name: str
    size: Optional[int] = None
    error: Optional[str] = None
    ordered: bool = True


def try_all(table: Table, unordered: bool = False,
            verify: bool = True) -> Tuple[List[Attempt], Dict[str, bytes]]:
    attempts, blobs = [], {}
    for strat in build_strategies():
        if not strat.ordered and not unordered:
            continue
        try:
            payload, meta = strat.encode(table)
            blob = pack(strat.name, meta, payload)
            if verify:
                name, m, p = unpack(blob)
                restored = strat.decode(p, m)
                if strat.ordered:
                    if restored.columns != table.columns or \
                            restored.rows != table.rows:
                        raise AssertionError("round trip mismatch")
                else:
                    if restored.columns != table.columns or \
                            sorted(restored.rows) != sorted(table.rows):
                        raise AssertionError("round trip mismatch (unordered)")
            attempts.append(Attempt(strat.name, len(blob), None, strat.ordered))
            blobs[strat.name] = blob
        except Exception as exc:                      # a failed candidate is
            attempts.append(Attempt(strat.name, None,                # not fatal
                                    "{}: {}".format(type(exc).__name__, exc),
                                    strat.ordered))
    return attempts, blobs


def compress(table: Table, unordered: bool = False):
    attempts, blobs = try_all(table, unordered=unordered, verify=True)
    winners = [a for a in attempts if a.size is not None]
    if not winners:
        raise RuntimeError("every strategy failed; see bench for details")
    best = min(winners, key=lambda a: a.size)
    return blobs[best.name], best, attempts


def decompress(blob: bytes) -> Table:
    name, meta, payload = unpack(blob)
    for strat in build_strategies():
        if strat.name == name:
            return strat.decode(payload, meta)
    raise ValueError("unknown strategy in container: {}".format(name))


# ==================================================================== CLI

def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "{:.1f} {}".format(n, unit) if unit != "B" else "{} B".format(n)
        n /= 1024.0


def cmd_compress(args) -> int:
    table = read_any(args.input)
    raw = os.path.getsize(args.input)
    blob, best, attempts = compress(table, unordered=args.unordered)
    out = args.output or (args.input + ".dtz")
    with open(out, "wb") as fh:
        fh.write(blob)
    rows, cols = table.shape
    print("{}  ->  {}".format(args.input, out))
    print("  table        {:,} rows x {} columns".format(rows, cols))
    print("  input        {}".format(_human(raw)))
    print("  output       {}   ({:.2f}x smaller)".format(
        _human(len(blob)), raw / len(blob) if len(blob) else 0))
    print("  strategy     {}{}".format(
        best.name, "" if best.ordered else "   [row order NOT preserved]"))
    runner = sorted((a for a in attempts if a.size and a.name != best.name),
                    key=lambda a: a.size)
    if runner:
        print("  runner-up    {} at {} ({:+.1%})".format(
            runner[0].name, _human(runner[0].size),
            runner[0].size / best.size - 1))
    print("  round trip   verified")
    return 0


def cmd_decompress(args) -> int:
    with open(args.input, "rb") as fh:
        blob = fh.read()
    name, meta, _ = unpack(blob)
    table = decompress(blob)
    out = args.output or os.path.splitext(args.input)[0]
    if not os.path.splitext(out)[1]:
        out += ".csv"
    write_any(table, out)
    rows, cols = table.shape
    print("{}  ->  {}".format(args.input, out))
    print("  strategy     {}".format(name))
    print("  table        {:,} rows x {} columns".format(rows, cols))
    return 0


def cmd_inspect(args) -> int:
    with open(args.input, "rb") as fh:
        blob = fh.read()
    name, meta, payload = unpack(blob)
    print("dtz container: {}".format(args.input))
    print("  strategy     {}".format(name))
    print("  container    {}".format(_human(len(blob))))
    print("  payload      {}".format(_human(len(payload))))
    print("  columns      {}".format(len(meta.get("names", [])) or "n/a"))
    if meta.get("sorted"):
        print("  note         rows were sorted; original order not preserved")
    if meta.get("dims"):
        print("  dependencies {} group(s):".format(len(meta["dims"])))
        names = meta["names"]
        for spec in meta["dims"]:
            deps = ", ".join(names[d] for d in spec["deps"])
            print("    {} -> {}".format(names[spec["det"]], deps))
    return 0


def cmd_bench(args) -> int:
    table = read_any(args.input)
    raw = os.path.getsize(args.input)
    attempts, _ = try_all(table, unordered=args.unordered, verify=True)
    rows, cols = table.shape
    print("\n{}   {:,} rows x {} columns   input {}\n".format(
        args.input, rows, cols, _human(raw)))
    ok = sorted((a for a in attempts if a.size), key=lambda a: a.size)
    width = max((len(a.name) for a in attempts), default=10)
    for i, a in enumerate(ok):
        flag = "" if a.ordered else "  [unordered]"
        mark = "  <-- best" if i == 0 else ""
        print("  {:<{w}}  {:>10}   {:>6.2f}x{}{}".format(
            a.name, _human(a.size), raw / a.size, flag, mark, w=width))
    for a in attempts:
        if a.size is None:
            print("  {:<{w}}  {:>10}   {}".format(
                a.name, "failed", a.error, w=width))
    if not args.unordered:
        print("\n  pass --unordered to also try row-sorting strategies")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dtz", description="compress a data table with the best "
                                "available strategy")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("compress", help="compress a table to .dtz")
    p.add_argument("input")
    p.add_argument("-o", "--output")
    p.add_argument("--unordered", action="store_true",
                   help="allow row reordering (declares row order meaningless)")
    p.set_defaults(func=cmd_compress)

    p = sub.add_parser("decompress", help="restore a table from .dtz")
    p.add_argument("input")
    p.add_argument("-o", "--output", help="extension picks the output format")
    p.set_defaults(func=cmd_decompress)

    p = sub.add_parser("inspect", help="show what is inside a .dtz")
    p.add_argument("input")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("bench", help="show every strategy's size")
    p.add_argument("input")
    p.add_argument("--unordered", action="store_true")
    p.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
