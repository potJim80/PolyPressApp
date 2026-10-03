"""Re-measure Polypress alone against a sweep's stored competitor rows.

    python3 benchmarks/rerun_ours.py ../OUT/results/socrata100-v2.jsonl ../IN/corpus100/ \
        --out ../OUT/results/socrata100-v3.jsonl
    python3 benchmarks/report.py ../OUT/results/socrata100-v3.jsonl --title "Socrata 100, v3"

A full sweep re-times all 22 competitors on every table, which takes hours
and changes nothing when only the codec changed: gzip, xz, 7z and Arrow are
the same programs they were. So this keeps every competitor row from the
input JSONL and replaces only the rows that depend on this codec --
`polypress`, `polypress+zstd`, `polypress+brotli`, the round-trip flag, rows,
columns and peak memory -- measured exactly as measure_one.py measures them,
on exactly the file the sweep measured: a dataset the sweep truncated is
truncated again with sweep.py's own function, and refused if the result is
not the same number of bytes.

PROTOCOL.md section 7 says a codec change between sweep and report
invalidates the result. This is how that is repaired without re-timing the
rivals, and every record it writes says so: "rerun_of" names the sweep the
competitor rows came from.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import measure_one  # noqa: E402
from sweep import truncate_to  # noqa: E402

OURS = ("polypress", "polypress+zstd", "polypress+brotli")


def remeasure(path: str) -> dict:
    """measure_one.measure() with the competitors switched off."""
    saved = (measure_one.CLI_TOOLS, measure_one.archiver_rows,
             measure_one.ppmd_rows, measure_one.arrow_rows)
    measure_one.CLI_TOOLS = []
    measure_one.archiver_rows = lambda *a, **k: None
    measure_one.ppmd_rows = lambda *a, **k: None
    measure_one.arrow_rows = lambda *a, **k: None
    try:
        return measure_one.measure(path)
    finally:
        (measure_one.CLI_TOOLS, measure_one.archiver_rows,
         measure_one.ppmd_rows, measure_one.arrow_rows) = saved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rerun_ours")
    ap.add_argument("sweep", help="the JSONL whose competitor rows are kept")
    ap.add_argument("corpus", help="directory holding the sweep's datasets")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-mb", type=float, default=28.0,
                    help="the cap the sweep ran with (default 28, sweep.py's; "
                         "socrata100-v2 ran at 16). A wrong cap is caught: the "
                         "truncated file must be the size the sweep measured")
    a = ap.parse_args(argv)

    recs = [json.loads(l) for l in open(a.sweep) if l.strip()]
    # resumable, like sweep.py: tables already in --out are kept
    done = set()
    if os.path.exists(a.out):
        done = {json.loads(l)["file"] for l in open(a.out) if l.strip()}
    tmp = tempfile.mkdtemp(prefix="rerun-")
    bad = 0
    with open(a.out, "a") as out:
        for i, rec in enumerate(recs, 1):
            name = rec["file"]
            if name in done:
                continue
            src = os.path.join(a.corpus, name)
            print("[{:>3}/{}] {:<52}".format(i, len(recs), name[:52]), end=" ", flush=True)
            if "error" in rec or not os.path.exists(src):
                print("skipped ({})".format(rec.get("error", "file missing")))
                out.write(json.dumps(rec) + "\n")
                continue
            path = src
            if rec.get("truncated_from"):
                path = os.path.join(tmp, name)
                truncate_to(src, path, int(a.max_mb * 1e6))
            if os.path.getsize(path) != rec["bytes"]:
                print("REFUSED: {} bytes, the sweep measured {} (wrong --max-mb?)".format(
                    os.path.getsize(path), rec["bytes"]))
                bad += 1
                if path != src:
                    os.remove(path)
                continue
            new = remeasure(path)
            for k in OURS:
                rec["results"].pop(k, None)
            rec["results"].update({k: v for k, v in new["results"].items() if k in OURS})
            for k in ("roundtrip", "rows", "cols", "polypress_peak_mb"):
                if k in new:
                    rec[k] = new[k]
            rec["rerun_of"] = os.path.basename(a.sweep)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            if path != src:
                os.remove(path)
            print("{:>11,} B  {}".format(rec["results"]["polypress"]["bytes"],
                                         "exact" if rec.get("roundtrip") else "ROUND TRIP FAILED"))
            bad += not rec.get("roundtrip")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
