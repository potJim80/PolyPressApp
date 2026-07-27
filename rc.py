"""Binary range coder with adaptive bit models (LZMA-style).

This is the piece the project was missing. Rice coding, which codec.py uses,
can only spend a whole number of bits per symbol. A column with three
responses needs about 1.5 bits each; Rice charges 2. The range coder spends
fractional bits, so it can reach the entropy floor.

Everything here is exact and reversible. encode/decode are mirror images and
must be kept that way.
"""

from __future__ import annotations

from typing import List

KBITS = 11
KTOP = 1 << KBITS          # probability denominator
PINIT = KTOP // 2
MOVE = 5                   # adaptation rate
TOP = 1 << 24


class Model:
    """A bank of adaptive binary probabilities.

    Sparse on purpose: cross-column conditioning multiplies context counts
    (parent alphabet x tree nodes) far past what a dense array can hold, but
    real data only ever visits a small corner of that space.
    """

    __slots__ = ("p",)

    def __init__(self, n: int = 1) -> None:
        self.p: dict = {}


class Encoder:
    def __init__(self) -> None:
        self.low = 0
        self.range = 0xFFFFFFFF
        self.cache = 0
        self.cache_size = 1
        self.out = bytearray()

    def _shift_low(self) -> None:
        if self.low < 0xFF000000 or self.low > 0xFFFFFFFF:
            carry = self.low >> 32
            temp = self.cache
            while True:
                self.out.append((temp + carry) & 0xFF)
                temp = 0xFF
                self.cache_size -= 1
                if self.cache_size == 0:
                    break
            self.cache = (self.low >> 24) & 0xFF
        self.cache_size += 1
        self.low = (self.low << 8) & 0xFFFFFFFF

    def bit(self, m: Model, i: int, b: int) -> None:
        p = m.p.get(i, PINIT)
        bound = (self.range >> KBITS) * p
        if b:
            self.low += bound
            self.range -= bound
            m.p[i] = p - (p >> MOVE)
        else:
            self.range = bound
            m.p[i] = p + ((KTOP - p) >> MOVE)
        while self.range < TOP:
            self.range = (self.range << 8) & 0xFFFFFFFF
            self._shift_low()

    def direct(self, value: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            self.range >>= 1
            if (value >> i) & 1:
                self.low += self.range
            while self.range < TOP:
                self.range = (self.range << 8) & 0xFFFFFFFF
                self._shift_low()

    def tree(self, m: Model, nbits: int, symbol: int) -> None:
        """Encode `nbits` bits of `symbol` through a binary tree of contexts."""
        node = 1
        for i in range(nbits - 1, -1, -1):
            b = (symbol >> i) & 1
            self.bit(m, node, b)
            node = (node << 1) | b

    def tree_ctx(self, m: Model, nbits: int, symbol: int, base: int) -> None:
        """As tree(), but the context bank is offset by `base` -- this is how
        a column conditions on its previous value."""
        node = 1
        for i in range(nbits - 1, -1, -1):
            b = (symbol >> i) & 1
            self.bit(m, base + node, b)
            node = (node << 1) | b

    def finish(self) -> bytes:
        for _ in range(5):
            self._shift_low()
        return bytes(self.out)


class Decoder:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 1                      # first byte is the encoder's padding
        self.range = 0xFFFFFFFF
        self.code = 0
        for _ in range(4):
            self.code = (self.code << 8) | self._byte()

    def _byte(self) -> int:
        if self.pos < len(self.data):
            v = self.data[self.pos]
        else:
            v = 0
        self.pos += 1
        return v

    def _norm(self) -> None:
        while self.range < TOP:
            self.range = (self.range << 8) & 0xFFFFFFFF
            self.code = ((self.code << 8) | self._byte()) & 0xFFFFFFFF

    def bit(self, m: Model, i: int) -> int:
        p = m.p.get(i, PINIT)
        bound = (self.range >> KBITS) * p
        if self.code < bound:
            self.range = bound
            m.p[i] = p + ((KTOP - p) >> MOVE)
            b = 0
        else:
            self.code -= bound
            self.range -= bound
            m.p[i] = p - (p >> MOVE)
            b = 1
        self._norm()
        return b

    def direct(self, n: int) -> int:
        result = 0
        for _ in range(n):
            self.range >>= 1
            self.code = (self.code - self.range) & 0xFFFFFFFF
            t = 0 - (self.code >> 31)
            self.code = (self.code + (self.range & t)) & 0xFFFFFFFF
            result = (result << 1) + (t + 1)
            self._norm()
        return result

    def tree(self, m: Model, nbits: int) -> int:
        node = 1
        for _ in range(nbits):
            node = (node << 1) | self.bit(m, node)
        return node - (1 << nbits)

    def tree_ctx(self, m: Model, nbits: int, base: int) -> int:
        node = 1
        for _ in range(nbits):
            node = (node << 1) | self.bit(m, base + node)
        return node - (1 << nbits)


# ------------------------------------------------------------ integer coding

MAXLEN = 40


def zigzag(v: int) -> int:
    return (-v << 1) - 1 if v < 0 else v << 1


def unzigzag(u: int) -> int:
    return -((u + 1) >> 1) if (u & 1) else (u >> 1)


class IntModel:
    """Codes an arbitrary signed integer: an adaptive bit-length prefix, then
    the mantissa as raw bits. Small values cost few bits, and the length model
    adapts to the column's actual spread."""

    def __init__(self) -> None:
        self.length = Model(1 << 7)     # tree over 0..63 bit-lengths

    def encode(self, enc: Encoder, value: int) -> None:
        u = zigzag(value)
        n = u.bit_length()
        enc.tree(self.length, 6, n)
        if n > 1:
            enc.direct(u - (1 << (n - 1)), n - 1)

    def decode(self, dec: Decoder) -> int:
        n = dec.tree(self.length, 6)
        if n == 0:
            return 0
        u = 1 << (n - 1)
        if n > 1:
            u += dec.direct(n - 1)
        return unzigzag(u)
