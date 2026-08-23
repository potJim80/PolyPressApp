#!/usr/bin/env python3
"""Round-trip every ANS variant on adversarial inputs before any measurement.

A coder that loses a bit on one input in a million is worthless as evidence,
and the corpus run would not necessarily hit it.
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rans          # noqa: E402
import ansfam as A   # noqa: E402

CODERS = [
    ("rANS-o0",   rans.rans0,    rans.unrans0),
    ("rANS-o1",   rans.rans1,    rans.unrans1),
    ("tANS-o0",   A.tans0,       A.untans0),
    ("tANS-o1",   A.tans1,       A.untans1),
    ("rANSx4-o0", A.rans0x4,     A.unrans0x4),
    ("rABS-o0",   A.rabs0,       A.unrabs0),
    ("rABS-o1",   A.rabs1,       A.unrabs1),
    ("uABS-o0",   A.uabs0,       A.ununabs0),
]


def cases():
    rnd = random.Random(20260805)
    yield "empty", b""
    yield "one byte", b"Q"
    yield "two bytes", b"\x00\xff"
    yield "all one symbol", b"Z" * 5000
    yield "all zero bytes", bytes(5000)
    yield "all 0xFF", b"\xff" * 5000
    yield "uniform random", bytes(rnd.randrange(256) for _ in range(20000))
    yield "two symbols 50/50", bytes(rnd.choice(b"AB") for _ in range(20000))
    yield "skew 4095:1", bytes(65 if rnd.randrange(4096) else 66
                               for _ in range(20000))
    yield "every byte once", bytes(range(256))
    yield "every byte, shuffled x20", bytes(
        rnd.sample(list(range(256)) * 20, 5120))
    yield "csv-ish text", ("\n".join(
        f"{rnd.randrange(1000)},CODE{rnd.randrange(20):02d},"
        f"{rnd.random():.6f}" for _ in range(2000))).encode()
    yield "long runs", b"".join(bytes([rnd.randrange(256)]) * rnd.randrange(1, 400)
                                for _ in range(200))
    yield "alternating", bytes((i & 1) * 255 for i in range(10000))
    # a 255-symbol alphabet forces a frequency of 1 somewhere after
    # normalisation, which is the tANS special case
    yield "255 symbols, 16 each", bytes(
        rnd.sample([s for s in range(1, 256) for _ in range(16)], 255 * 16))
    for n in (1, 2, 3, 7, 8, 9, 15, 16, 17, 100, 4095, 4096, 4097):
        yield f"random n={n}", bytes(rnd.randrange(256) for _ in range(n))


def main():
    ok = True
    for name, data in cases():
        head = f"{name:<26} n={len(data):<6}"
        cells, bad = [], []
        for label, enc, dec in CODERS:
            try:
                blob = enc(data)
                back = dec(blob)
            except Exception as e:
                bad.append(f"{label} RAISED {type(e).__name__}: {e}")
                cells.append(f"{label}=ERR")
                ok = False
                continue
            if back != data:
                bad.append(f"{label} MISMATCH (got {len(back)}B, "
                           f"want {len(data)}B)")
                cells.append(f"{label}=BAD")
                ok = False
                continue
            cells.append(f"{label}={len(blob)}")
        print(head + " ".join(cells))
        for b in bad:
            print(f"    !! {b}")
    print("\nALL ROUND-TRIPPED" if ok else "\nFAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
