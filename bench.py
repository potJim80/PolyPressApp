"""Size AND speed, both directions, against the real binaries.

    python3 bench.py table.csv [more.csv ...]

Compression tools are a ratio/speed trade-off, so a size win means nothing
without the clock next to it. Every tool here is timed the same way: bytes in
from a file, bytes out to a pipe, then the reverse.
"""
import os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dtz, fast

TOOLS = [
    ("gzip -9",       ["gzip", "-9", "-c"],                 ["gzip", "-dc"]),
    ("bzip2 -9",      ["bzip2", "-9", "-c"],                ["bzip2", "-dc"]),
    ("xz -9e",        ["xz", "-9e", "-c"],                  ["xz", "-dc"]),
    ("zstd -3",       ["zstd", "-3", "-c", "-q"],           ["zstd", "-dc", "-q"]),
    ("zstd -19",      ["zstd", "-19", "-c", "-q"],          ["zstd", "-dc", "-q"]),
    ("zstd -22 ultra", ["zstd", "--ultra", "-22", "-c", "-q"], ["zstd", "-dc", "-q"]),
    ("brotli -q 11",  ["brotli", "-q", "11", "-c"],         ["brotli", "-dc"]),
]

REPS = 3


def timed(fn):
    best = None
    for _ in range(REPS):
        t0 = time.time()
        out = fn()
        dt = time.time() - t0
        best = dt if best is None else min(best, dt)
    return out, best


for path in sys.argv[1:]:
    raw = os.path.getsize(path)
    mb = raw / 1e6
    data = open(path, "rb").read()
    print("\n" + "=" * 76)
    print("{}   {:,} B".format(os.path.basename(path), raw))
    print("=" * 76)
    print("  {:<16} {:>10} {:>7} {:>11} {:>11}".format(
        "codec", "bytes", "ratio", "enc MB/s", "dec MB/s"))
    print("  " + "-" * 62)

    rows = []
    for label, ce, cd in TOOLS:
        comp, te = timed(lambda: subprocess.run(
            ce, input=data, stdout=subprocess.PIPE).stdout)
        _, td = timed(lambda: subprocess.run(
            cd, input=comp, stdout=subprocess.PIPE).stdout)
        rows.append((label, len(comp), mb / te, mb / td))

    t = dtz.read_any(path)
    blob, te = timed(lambda: fast.encode(t))
    _, td = timed(lambda: fast.decode(blob))
    rows.append(("*** fast.py", len(blob), mb / te, mb / td))

    for label, n, e, d in sorted(rows, key=lambda r: r[1]):
        print("  {:<16} {:>10,} {:>6.2f}x {:>10.1f} {:>11.1f}".format(
            label, n, raw / n, e, d))
