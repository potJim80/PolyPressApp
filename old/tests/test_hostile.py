"""Corrupt-archive fuzzing for the readers that are not the single-shot one.

`test_cbin.py` puts the C decoder and `fast.decode` through this; those got
hardened after fuzzing found the decompression wrappers doubling their output
buffer toward a terabyte on input that never decodes at any size. `stream.py`
and `ondemand.py` had never been through the same mill, which is the only
reason this file exists.

Invariant 3: the decoder treats its input as hostile. It reads files other
people made, so corrupt input must be refused -- never crash, never allocate
unbounded. "Refused" means an exception the CLI turns into one line, not a
traceback and not a MemoryError three gigabytes later.

Each case is opened in a **subprocess under a hard RLIMIT_AS**, because the
failure being tested for is precisely the one that takes the machine down with
it if it happens in-process.

    python3 tests/test_hostile.py            # both readers
    python3 tests/test_hostile.py stream     # just one
"""

from __future__ import annotations

import csv
import os
import random
import resource
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MEM_CAP = 512 * 1024 * 1024
SELF = os.path.abspath(__file__)


# --------------------------------------------------------------- the probe

def probe(path: str, kind: str) -> int:
    """Open one archive under a memory cap. Runs as its own process.

    Exit codes: 0 opened, 1 refused cleanly, 2 unexpected exception type,
    3 hit the memory cap.
    """
    resource.setrlimit(resource.RLIMIT_AS, (MEM_CAP, MEM_CAP))
    try:
        if kind == "stream":
            from polypress import stream
            stream.info(path)
            list(stream.iter_blocks(path))
        else:
            from polypress import ondemand
            r = ondemand.Reader(path)
            for c in r.columns:
                r.column(c)
            r.close()
        return 0
    except MemoryError:
        return 3
    except (ValueError, OSError, KeyError, TypeError, IndexError, EOFError,
            UnicodeDecodeError, LookupError, ArithmeticError,
            AttributeError, StopIteration) as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1
    except Exception as exc:                      # noqa: BLE001 -- the point
        print(type(exc).__name__, file=sys.stderr)
        return 2


# ---------------------------------------------------------------- the mill

def sample_table(path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["id", "kind", "val", "note"])
        for i in range(3000):
            w.writerow([i, ["a", "b", "c"][i % 3], "{:.2f}".format(i * 1.5),
                        "note {}".format(i % 9)])


def build(kind: str, src: str, dst: str) -> bytes:
    if kind == "stream":
        from polypress import stream
        stream.compress(src, dst, rows=500)
    else:
        from polypress import dtz, ondemand
        with open(dst, "wb") as fh:
            fh.write(ondemand.pack(dtz.read_any(src)))
    with open(dst, "rb") as fh:
        return fh.read()


def mutations(good: bytes, kind: str):
    """Truncations, bit flips, and aimed shots at the length field.

    The aimed ones matter: a fuzzer finds a bad length eventually, a reader of
    the code finds it immediately. Both readers take an 8-byte length straight
    out of the file and use it for a seek and a read."""
    cases = [("empty", b""), ("magic only", good[:4])]
    step = max(1, len(good) // 30)
    for n in range(0, len(good), step):
        cases.append(("truncate@{}".format(n), good[:n]))
    rnd = random.Random(11)
    for i in range(80):
        b = bytearray(good)
        b[rnd.randrange(len(b))] = rnd.randrange(256)
        cases.append(("bitflip{}".format(i), bytes(b)))
    for name, val in [("len=2^63", (1 << 63).to_bytes(8, "big")),
                      ("len=2^40", (1 << 40).to_bytes(8, "big")),
                      ("len=max", b"\xff" * 8),
                      ("len=0", (0).to_bytes(8, "big"))]:
        b = bytearray(good)
        if kind == "stream":
            b[-8:] = val               # stream keeps its header length last
        else:
            b[4:12] = val              # ondemand keeps it right after MAGIC
        cases.append((name, bytes(b)))
    return cases


def run(kind: str) -> int:
    tmp = tempfile.mkdtemp(prefix="ppz-hostile-")
    try:
        src = os.path.join(tmp, "t.csv")
        sample_table(src)
        good = build(kind, src, os.path.join(tmp, "good.bin"))
        arc = os.path.join(tmp, "case.bin")

        counts, bad = {}, []
        cases = mutations(good, kind)
        for name, data in cases:
            with open(arc, "wb") as fh:
                fh.write(data)
            p = subprocess.run([sys.executable, SELF, "--probe", arc, kind],
                               capture_output=True, timeout=120)
            counts[p.returncode] = counts.get(p.returncode, 0) + 1
            if p.returncode not in (0, 1):
                bad.append((name, p.returncode,
                            p.stderr.decode().strip()[-60:]))

        print("{}: {} corrupt archives".format(kind, len(cases)))
        print("  opened (still valid) : {}".format(counts.get(0, 0)))
        print("  refused cleanly      : {}".format(counts.get(1, 0)))
        print("  unexpected exception : {}".format(counts.get(2, 0)))
        print("  hit the {} MB cap   : {}".format(
            MEM_CAP // (1024 * 1024), counts.get(3, 0)))
        for name, rc, detail in bad[:10]:
            print("   ! {:<16} exit {}  {}".format(name, rc, detail))
        return len(bad)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if "--probe" in sys.argv:
        i = sys.argv.index("--probe")
        return probe(sys.argv[i + 1], sys.argv[i + 2])

    want = [a for a in sys.argv[1:] if not a.startswith("-")]
    kinds = want or ["stream", "ondemand"]
    bad = 0
    for kind in kinds:
        if kind == "ondemand":
            try:
                import polypress.ondemand           # noqa: F401
            except ImportError:
                # ondemand lives on its own branch; skipping is correct on
                # master rather than a failure.
                print("ondemand: not present on this branch -- skipped")
                continue
        bad += run(kind)
    print()
    print("every corrupt archive was refused cleanly" if not bad
          else "{} case(s) did NOT refuse cleanly".format(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
