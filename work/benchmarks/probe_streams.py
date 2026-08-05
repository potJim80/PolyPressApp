#!/usr/bin/env python3
"""Where does the single xz stream stop paying?

The codec finishes the whole table in ONE xz stream, so xz can copy between
adjacent columns -- which is the entire mechanism behind `pool` (-25% on
cdc_nndss). But the dictionary at preset 9e reaches back 64 MB. Once a single
column is larger than that, no two columns are ever in the window together and
the cross-column reach is gone whether or not the stream is split.

That predicts a crossover: below some table size the single stream wins, above
it the split costs nothing and per-stream entropy tuning is free money.

Reaching that regime by growing the table needs a column over 64 MB -- a
gigabyte-scale file for anything but the narrowest table, which this machine
should not be asked for. What actually decides whether xz can reach across a
column boundary is the RATIO of column size to window, so the sweep shrinks
the window instead and leaves the table small. Same ratio, same mechanism, a
thousandth of the machine time. PROBE_DICTS is in MB; 64 is what preset 9e
asks for, and 0.25 on a 0.5 MB column is the 50 GB table in miniature.

    PROBE_DICTS=64,4,1,0.25 python3 benchmarks/probe_streams.py f.csv

Every variant is decoded and compared cell by cell before it is reported. One
that does not round-trip prints BROKEN and its number is not used.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "old"))

from polypress import dtz            # noqa: E402
from stridexz import codec           # noqa: E402

# v7c BEST, the configuration everything else was measured against.
BASE = {"pool": True, "fixed": True, "planes": True, "tune": {"lc": 4, "pb": 0}}

VARIANTS = [
    ("one",         {}),
    ("percol",      {"streams": "percol"}),
    ("percol+tune", {"streams": "percol", "stune": True}),
    ("bykind",      {"streams": "bykind"}),
    ("bykind+tune", {"streams": "bykind", "stune": True}),
]


def load(path, max_mb):
    cap = int(max_mb * 1024 * 1024)
    if os.path.getsize(path) <= cap:
        return dtz.read_any(path).normalise(), False
    with open(path, "rb") as fh:
        head = fh.read(cap)
    head = head[:head.rfind(b"\n") + 1]
    tmp = path + ".__probe_streams_cut"
    with open(tmp, "wb") as fh:
        fh.write(head)
    try:
        return dtz.read_any(tmp).normalise(), True
    finally:
        os.unlink(tmp)


def main():
    cap_mb = float(os.environ.get("PROBE_MAX_MB", "8"))
    dicts = [float(d) for d in
             os.environ.get("PROBE_DICTS", "64,4,1,0.25").split(",")]
    for path in sys.argv[1:]:
        name = os.path.basename(path)
        t, cut = load(path, cap_mb)
        rows, cols = t.shape
        seen = min(os.path.getsize(path), cap_mb * 1024 * 1024)
        # The mechanism is column size against the window, so report the
        # average column, not the table.
        percol_mb = (seen / max(cols, 1)) / 1e6
        print(f"\n{name}  {rows:,} rows x {cols} cols, {seen / 1e6:.1f} MB read"
              f"{' (truncated)' if cut else ''}"
              f"  ~{percol_mb:.3f} MB/column")
        print(f"   {'dict':>7}  {'col/dict':>8}  {'variant':<14}"
              f"{'bytes':>12}{'secs':>7}   vs one")
        sys.stdout.flush()

        for dmb in dicts:
            base_size = None
            ratio = percol_mb / dmb
            for label, extra in VARIANTS:
                o = dict(BASE)
                o.update(extra)
                o["dictmb"] = dmb
                t0 = time.perf_counter()
                try:
                    blob = codec.encode(t, **o)
                    ok = codec.decode(blob).rows == t.rows
                except Exception as exc:               # noqa: BLE001
                    print(f"   {dmb:>6g}M  {ratio:>8.2f}  {label:<14}"
                          f" ERROR {type(exc).__name__}: {exc}")
                    continue
                secs = time.perf_counter() - t0
                if label == "one":
                    base_size = len(blob)
                delta = ""
                if base_size and label != "one":
                    pct = (len(blob) - base_size) / base_size * 100
                    delta = f"{pct:+7.2f}%"
                flag = "" if ok else "   <-- BROKEN"
                print(f"   {dmb:>6g}M  {ratio:>8.2f}  {label:<14}"
                      f"{len(blob):>12,}{secs:>6.1f}s  {delta}{flag}")
                sys.stdout.flush()
        del t


if __name__ == "__main__":
    main()
