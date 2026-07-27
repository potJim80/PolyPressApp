"""ctypes binding for tcz.c, with a working pure-Python fallback.

Builds the shared library on first import if it is missing or older than the
source. If anything goes wrong -- no compiler, unusual platform -- `HAVE_C`
stays False and the callers use numpy instead. Nothing here is required for
correctness.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from typing import List, Optional, Tuple

import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_DIR, "tcz.c")
_LIB = os.path.join(_DIR, "libtcz.so")

HAVE_C = False
_lib = None


def _build() -> bool:
    if not os.path.exists(_SRC):
        return False
    if os.path.exists(_LIB) and \
            os.path.getmtime(_LIB) >= os.path.getmtime(_SRC):
        return True
    cmd = ["cc", "-O3", "-shared", "-fPIC", "-o", _LIB, _SRC]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=120)
        return r.returncode == 0
    except Exception:
        return False


def _load() -> None:
    global HAVE_C, _lib
    if not _build():
        return
    try:
        lib = ctypes.CDLL(_LIB)
    except OSError:
        return

    c64 = ctypes.c_int64
    p64 = ctypes.POINTER(ctypes.c_int64)
    pu8 = ctypes.POINTER(ctypes.c_uint8)
    pu64 = ctypes.POINTER(ctypes.c_uint64)

    lib.scan_decimals.restype = ctypes.c_int32
    lib.scan_decimals.argtypes = [ctypes.c_char_p, c64, p64]
    lib.parse_fixed.restype = ctypes.c_int32
    lib.parse_fixed.argtypes = [ctypes.c_char_p, c64, ctypes.c_int32, p64, c64]
    lib.fmt_fixed.restype = c64
    lib.fmt_fixed.argtypes = [p64, c64, ctypes.c_int32, ctypes.c_char_p, c64]
    lib.pack_ints.restype = c64
    lib.pack_ints.argtypes = [p64, c64, pu8, pu64]
    lib.unpack_ints.restype = None
    lib.unpack_ints.argtypes = [pu8, c64, pu64, p64]

    _lib = lib
    HAVE_C = True


_load()


def _ptr(a: np.ndarray, ct):
    return a.ctypes.data_as(ctypes.POINTER(ct))


# --------------------------------------------------------------- numeric

def parse_column(cells: List[str]) -> Optional[Tuple[np.ndarray, int]]:
    """Decimal text -> (scaled int64 array, decimals), or None if the column
    is not exactly representable that way.

    Exactness is checked the only way that cannot be argued with: format the
    parsed integers back and compare the bytes."""
    if not HAVE_C or not cells:
        return None
    blob = "\n".join(cells).encode()
    n = len(cells)
    cnt = ctypes.c_int64(0)
    dec = _lib.scan_decimals(blob, len(blob), ctypes.byref(cnt))
    if dec < 0 or cnt.value != n:
        return None
    out = np.empty(n, dtype=np.int64)
    if _lib.parse_fixed(blob, len(blob), dec, _ptr(out, ctypes.c_int64), n) != 0:
        return None
    if format_column(out, dec) != blob.decode():
        return None                       # leading zeros, "-0.0", ragged
    return out, dec


def format_column(a: np.ndarray, dec: int) -> str:
    """Scaled int64 array -> newline-joined decimal text."""
    a = np.ascontiguousarray(a, dtype=np.int64)
    cap = a.size * 42 + 64
    buf = ctypes.create_string_buffer(cap)
    wrote = _lib.fmt_fixed(_ptr(a, ctypes.c_int64), a.size, dec, buf, cap)
    if wrote < 0:
        raise RuntimeError("fmt_fixed capacity")
    return buf.raw[:wrote].decode()


def cells_from_ints(a: np.ndarray, dec: int) -> List[str]:
    if a.size == 0:
        return []
    return format_column(a, dec).split("\n")


# --------------------------------------------------------------- varints

def pack(res: np.ndarray) -> bytes:
    """Layout: width byte (4 or 8), then one head byte per value, then the
    escaped values at that width.

    The width is per-array. Escapes always fit in 64 bits, but on real tables
    they almost always fit in 32 -- a fixed 8-byte tail cost 5% on a table of
    9-digit vehicle IDs."""
    a = np.ascontiguousarray(res, dtype=np.int64)
    head = np.empty(a.size, dtype=np.uint8)
    tail = np.empty(a.size, dtype=np.uint64)
    nbig = _lib.pack_ints(_ptr(a, ctypes.c_int64), a.size,
                          _ptr(head, ctypes.c_uint8),
                          _ptr(tail, ctypes.c_uint64))
    tail = tail[:nbig]
    if nbig and int(tail.max()) >= (1 << 32):
        return b"\x08" + head.tobytes() + tail.tobytes()
    return b"\x04" + head.tobytes() + tail.astype("<u4").tobytes()


def unpack(buf: bytes, n: int) -> np.ndarray:
    width = buf[0]
    head = np.ascontiguousarray(np.frombuffer(buf[1:1 + n], dtype=np.uint8))
    nbig = int((head == 255).sum())
    at = 1 + n
    if width == 8:
        tail = np.frombuffer(buf[at:at + 8 * nbig], dtype="<u8")
    else:
        tail = np.frombuffer(buf[at:at + 4 * nbig], dtype="<u4").astype(np.uint64)
    tail = np.ascontiguousarray(tail)
    out = np.empty(n, dtype=np.int64)
    _lib.unpack_ints(_ptr(head, ctypes.c_uint8), n,
                     _ptr(tail, ctypes.c_uint64), _ptr(out, ctypes.c_int64))
    return out


if __name__ == "__main__":
    print("C acceleration:", "available" if HAVE_C else "NOT available")
    if HAVE_C:
        cells = ["5.40", "-73.67", "0.05", "0.00", "12345.99"]
        got = parse_column(cells)
        print("parse  ", got[0].tolist(), "dec", got[1])
        print("format ", cells_from_ints(got[0], got[1]))
        arr = np.array([0, 1, -1, 300, -99999], dtype=np.int64)
        print("varint ", unpack(pack(arr), arr.size).tolist())
