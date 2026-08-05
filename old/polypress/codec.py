"""Lossless table codec built on low-order polynomial extrapolation.

The salvaged form of the idea. Per column:

  1. Read the printed cells as fixed-point integers (scientific tables are
     printed to a fixed number of decimals, so this is exact).
  2. Predict each value by extrapolating a degree-k polynomial fitted to the
     k+1 values before it. On the integer grid that prediction is just a
     finite-difference formula, so the residual is the (k+1)-th finite
     difference -- the discrete analogue of a Taylor remainder.
  3. Entropy-code the residuals with Rice coding.

Nothing is thrown away: the residual stream restores the column exactly. The
saving comes from the residuals being small, which happens exactly when the
column is locally well-approximated by a low-degree polynomial.

Round-trips to the original CSV text byte-for-byte.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

BLOCK = 64          # residuals per Rice-parameter block
MAX_ORDER = 4       # highest polynomial predictor order
RICE_ESCAPE = 48    # unary quotient cap before falling back to raw varint
K_BITS = 6          # width of the per-block Rice-parameter field
MAX_K = (1 << K_BITS) - 1

# pred[i] = sum(c[j] * x[i-1-j]); residual = x[i] - pred[i].
# These are the degree-k polynomial extrapolators on a unit grid.
FIXED_PREDICTORS = {
    0: [],
    1: [1],
    2: [2, -1],
    3: [3, -3, 1],
    4: [4, -6, 4, -1],
}

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


class BitWriter:
    def __init__(self) -> None:
        self.buf = bytearray()
        self._cur = 0
        self._nbits = 0

    def bit(self, b: int) -> None:
        self._cur = (self._cur << 1) | (b & 1)
        self._nbits += 1
        if self._nbits == 8:
            self.buf.append(self._cur)
            self._cur = 0
            self._nbits = 0

    def bits(self, value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            self.bit((value >> shift) & 1)

    def unary(self, q: int) -> None:
        for _ in range(q):
            self.bit(1)
        self.bit(0)

    def uvarint(self, v: int) -> None:
        assert v >= 0
        while True:
            chunk = v & 0x7F
            v >>= 7
            self.bit(1 if v else 0)
            self.bits(chunk, 7)
            if not v:
                return

    def svarint(self, v: int) -> None:
        self.uvarint(zigzag(v))

    def rice(self, u: int, k: int) -> None:
        q = u >> k
        if q >= RICE_ESCAPE:
            self.unary(RICE_ESCAPE)
            self.uvarint(u)
        else:
            self.unary(q)
            if k:
                self.bits(u & ((1 << k) - 1), k)

    def bytes_out(self) -> bytes:
        if self._nbits:
            self.buf.append(self._cur << (8 - self._nbits))
            self._cur = 0
            self._nbits = 0
        return bytes(self.buf)


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def bit(self) -> int:
        byte = self.data[self.pos >> 3]
        b = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return b

    def bits(self, width: int) -> int:
        v = 0
        for _ in range(width):
            v = (v << 1) | self.bit()
        return v

    def unary(self) -> int:
        q = 0
        while self.bit():
            q += 1
        return q

    def uvarint(self) -> int:
        v = 0
        shift = 0
        while True:
            more = self.bit()
            v |= self.bits(7) << shift
            shift += 7
            if not more:
                return v

    def svarint(self) -> int:
        return unzigzag(self.uvarint())

    def rice(self, k: int) -> int:
        q = self.unary()
        if q >= RICE_ESCAPE:
            return self.uvarint()
        return (q << k) | (self.bits(k) if k else 0)


def zigzag(v: int) -> int:
    return 2 * v if v >= 0 else -2 * v - 1


def unzigzag(u: int) -> int:
    return u >> 1 if u % 2 == 0 else -((u + 1) >> 1)


# ---------------------------------------------------------------- fixed point

def cell_to_int(cell: str, decimals: int) -> int:
    neg = cell.startswith("-")
    if neg:
        cell = cell[1:]
    if "." in cell:
        whole, frac = cell.split(".")
    else:
        whole, frac = cell, ""
    frac = (frac + "0" * decimals)[:decimals]
    v = int(whole + frac)
    return -v if neg else v


def int_to_cell(value: int, decimals: int) -> str:
    neg = value < 0
    digits = str(abs(value)).rjust(decimals + 1, "0")
    if decimals:
        digits = digits[:-decimals] + "." + digits[-decimals:]
    return ("-" if neg else "") + digits


def as_numeric_column(cells: Sequence[str]) -> Optional[Tuple[List[int], int]]:
    """Return (integers, decimals) if the column can be stored as fixed point
    and reproduced exactly, else None."""
    decimals = 0
    for cell in cells:
        if not _NUMERIC.match(cell):
            return None
        if "." in cell:
            decimals = max(decimals, len(cell.split(".")[1]))
    if decimals > 63:
        return None
    ints = [cell_to_int(c, decimals) for c in cells]
    for value, original in zip(ints, cells):
        if int_to_cell(value, decimals) != original:
            return None  # leading zeros, "-0.0", ragged decimals, ...
    return ints, decimals


# ------------------------------------------------------------- prediction

def residuals(ints: Sequence[int], order: int) -> List[int]:
    coeffs = FIXED_PREDICTORS[order]
    out = []
    for i in range(order, len(ints)):
        pred = 0
        for j, c in enumerate(coeffs):
            pred += c * ints[i - 1 - j]
        out.append(ints[i] - pred)
    return out


def restore(warmup: Sequence[int], res: Sequence[int], order: int, n: int) -> List[int]:
    coeffs = FIXED_PREDICTORS[order]
    ints = list(warmup)
    for i in range(order, n):
        pred = 0
        for j, c in enumerate(coeffs):
            pred += c * ints[i - 1 - j]
        ints.append(res[i - order] + pred)
    return ints


def _rice_cost(us: Sequence[int], k: int) -> int:
    total = 0
    for u in us:
        q = u >> k
        if q >= RICE_ESCAPE:
            nbytes = max(1, (u.bit_length() + 6) // 7)
            total += RICE_ESCAPE + 1 + 8 * nbytes
        else:
            total += q + 1 + k
    return total


def _best_k(us: Sequence[int]) -> Tuple[int, int]:
    best = (0, _rice_cost(us, 0))
    for k in range(1, MAX_K + 1):
        cost = _rice_cost(us, k)
        if cost < best[1]:
            best = (k, cost)
    return best


def _blocks(res: Sequence[int]) -> List[List[int]]:
    us = [zigzag(r) for r in res]
    return [us[i:i + BLOCK] for i in range(0, len(us), BLOCK)]


def _column_cost(ints: Sequence[int], order: int) -> int:
    if len(ints) <= order:
        return 1 << 60
    cost = 3  # order field
    for value in ints[:order]:
        cost += 8 * max(1, (zigzag(value).bit_length() + 6) // 7)
    for block in _blocks(residuals(ints, order)):
        cost += K_BITS + _best_k(block)[1]
    return cost


def choose_order(ints: Sequence[int]) -> int:
    return min(range(MAX_ORDER + 1), key=lambda o: _column_cost(ints, o))


# ------------------------------------------------------------------ codec

MAGIC = b"PTC1"


def encode(csv_text: str):
    lines = csv_text.rstrip("\n").split("\n")
    header = lines[0].split(",")
    rows = [line.split(",") for line in lines[1:]]
    ncols, nrows = len(header), len(rows)

    bw = BitWriter()
    bw.uvarint(nrows)
    bw.uvarint(ncols)
    for name in header:
        raw = name.encode("utf-8")
        bw.uvarint(len(raw))
        for byte in raw:
            bw.bits(byte, 8)

    report = []
    for col in range(ncols):
        cells = [row[col] for row in rows]
        numeric = as_numeric_column(cells)
        if numeric is None:
            bw.bit(0)
            for cell in cells:
                raw = cell.encode("utf-8")
                bw.uvarint(len(raw))
                for byte in raw:
                    bw.bits(byte, 8)
            report.append((header[col], None, None))
            continue

        ints, decimals = numeric
        order = choose_order(ints)
        bw.bit(1)
        bw.bits(decimals, 6)
        bw.bits(order, 3)
        for value in ints[:order]:
            bw.svarint(value)
        for block in _blocks(residuals(ints, order)):
            k, _ = _best_k(block)
            bw.bits(k, K_BITS)
            for u in block:
                bw.rice(u, k)
        report.append((header[col], order, _column_cost(ints, order) / 8.0))

    return MAGIC + bw.bytes_out(), report


def decode(blob: bytes) -> str:
    assert blob[:4] == MAGIC, "bad magic"
    br = BitReader(blob[4:])
    nrows = br.uvarint()
    ncols = br.uvarint()
    header = []
    for _ in range(ncols):
        n = br.uvarint()
        header.append(bytes(br.bits(8) for _ in range(n)).decode("utf-8"))

    columns = []
    for _ in range(ncols):
        if br.bit() == 0:
            cells = []
            for _ in range(nrows):
                n = br.uvarint()
                cells.append(bytes(br.bits(8) for _ in range(n)).decode("utf-8"))
            columns.append(cells)
            continue

        decimals = br.bits(6)
        order = br.bits(3)
        warmup = [br.svarint() for _ in range(order)]
        res = []
        remaining = nrows - order
        while remaining > 0:
            k = br.bits(K_BITS)
            take = min(BLOCK, remaining)
            for _ in range(take):
                res.append(unzigzag(br.rice(k)))
            remaining -= take
        ints = restore(warmup, res, order, nrows)
        columns.append([int_to_cell(v, decimals) for v in ints])

    out = [",".join(header)]
    for i in range(nrows):
        out.append(",".join(col[i] for col in columns))
    return "\n".join(out) + "\n"
