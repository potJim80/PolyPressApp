"""Head-to-head harness for the turbo fork against master's encoder.

    python3 benchmarks/bench_turbo.py <benchdir> --enc fast turbo

Every encoder is measured the same way on the same in-memory table, so the CSV
parse is never counted. Each archive is decoded and compared cell for cell
before its size is allowed to count -- a fast encoder that loses data is not a
result, and this repo has been bitten by verification that sat downstream of
the damage.

Reports MB/s against the ORIGINAL file size, so the numbers are comparable with
everything else in results/.
"""

from __future__ import annotations

import argparse
import glob
import importlib
import json
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from polypress import dtz  # noqa: E402


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 << 20)


def run(mod, table, mb):
    """(bytes, encode_seconds, decode_seconds, exact) for one encoder."""
    t0 = time.time()
    blob = mod.encode(table)
    t_enc = time.time() - t0

    t0 = time.time()
    back = mod.decode(blob)
    t_dec = time.time() - t0

    exact = (list(back.columns) == list(table.columns)
             and back.rows == table.rows)
    return len(blob), t_enc, t_dec, exact


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench_turbo")
    ap.add_argument("benchdir")
    ap.add_argument("--enc", nargs="+", default=["fast", "turbo"],
                    help="module names under polypress/")
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", default=None, help="substring filter")
    a = ap.parse_args(argv)

    mods = {}
    for name in a.enc:
        try:
            mods[name] = importlib.import_module(f"polypress.{name}")
        except Exception as exc:
            print(f"cannot import polypress.{name}: {exc}")
            return 1

    paths = sorted(glob.glob(os.path.join(a.benchdir, "*.csv")))
    if a.only:
        paths = [p for p in paths if a.only in p]
    if not paths:
        print("no datasets")
        return 1

    hdr = "{:<18} {:>7}".format("dataset", "MB")
    for name in a.enc:
        hdr += " | {:>10} {:>7} {:>7}".format(name[:10], "MB/s", "ok")
    print(hdr)
    print("-" * len(hdr))

    totals = {n: dict(bytes=0, enc=0.0, dec=0.0, bad=0) for n in a.enc}
    total_mb = 0.0
    rows = []
    for p in paths:
        name = os.path.basename(p).replace(".csv", "")
        mb = os.path.getsize(p) / 1e6
        total_mb += mb
        table = dtz.read_any(p)
        rec = {"dataset": name, "mb": mb}
        line = "{:<18} {:>7.1f}".format(name[:18], mb)
        for enc in a.enc:
            try:
                nb, te, td, ok = run(mods[enc], table, mb)
            except Exception as exc:
                line += " | {:>10} {:>7} {:>7}".format("ERR", "-", str(exc)[:6])
                rec[enc] = {"error": str(exc)}
                totals[enc]["bad"] += 1
                continue
            totals[enc]["bytes"] += nb
            totals[enc]["enc"] += te
            totals[enc]["dec"] += td
            if not ok:
                totals[enc]["bad"] += 1
            rec[enc] = dict(bytes=nb, enc=te, dec=td, exact=ok)
            line += " | {:>10,} {:>7.2f} {:>7}".format(
                nb, mb / te if te else 0, "yes" if ok else "**NO**")
        del table
        rows.append(rec)
        print(line, flush=True)

    print("-" * len(hdr))
    base = a.enc[0]
    print("\n{:<12} {:>14} {:>10} {:>9} {:>9} {:>8}".format(
        "encoder", "total bytes", "vs " + base[:7], "enc s", "MB/s", "exact"))
    for enc in a.enc:
        t = totals[enc]
        d = 100.0 * (t["bytes"] - totals[base]["bytes"]) / totals[base]["bytes"]
        print("{:<12} {:>14,} {:>+9.2f}% {:>9.1f} {:>9.2f} {:>8}".format(
            enc, t["bytes"], d, t["enc"], total_mb / t["enc"] if t["enc"] else 0,
            "ALL" if not t["bad"] else f"{t['bad']} BAD"))
    print(f"\n{total_mb:.1f} MB across {len(rows)} datasets; "
          f"peak RSS {rss_mb():.0f} MB")
    for enc in a.enc:
        if a.enc[0] != enc and totals[enc]["enc"]:
            print(f"  {enc} is {totals[base]['enc']/totals[enc]['enc']:.2f}x "
                  f"the speed of {base}")

    if a.out:
        json.dump(rows, open(a.out, "w"), indent=1)
        print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
