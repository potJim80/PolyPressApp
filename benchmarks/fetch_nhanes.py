"""Download real CDC NHANES survey microdata and merge it into one wide table.

    python3 benchmarks/fetch_nhanes.py nhanes_real.csv
    python3 benchmarks/bench.py nhanes_real.csv

Why this exists: every survey number in this repo used to come from either a
file that could not be committed (NHAMCS, 278 MB) or one we generated
ourselves (make_survey.py). Generated data proves nothing on its own -- we
built the skip patterns into it, so of course the codec found them.

This fetches genuinely public, genuinely real microdata from CDC, so anyone
can reproduce the survey claim from scratch with no credentials and no manual
download. NHANES is the right stand-in for NEMSIS/NEDS: coded categorical
questionnaire responses, real skip patterns (answer "no" to a gate question
and the whole downstream block is blank), and real hierarchies.

The 2017-2018 cycle merged across 17 questionnaire files gives roughly
9,254 rows x 421 columns and about 6 MB of CSV -- wider than the NHAMCS file
in the README, and small enough to stay well inside a 1-2 GB memory budget.

Measured 2026-07-27: polypress 516,601 B vs xz -9e 707,784 B (1.37x) and
parquet+brotli 1,003,563 B (1.94x). Parquet did NOT round-trip the exact
printed text on this file, so part of its size is discarded formatting.

Encoding runs at only 1.3 MB/s here. That is the O(columns^2) parent search
showing its cost on a genuinely wide table -- 421 columns is 176,820 ordered
pairs. Worth knowing before pointing this at a 1,000-column extract.
"""

from __future__ import annotations

import csv
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xpt import read_xpt

BASE = "https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/2017/DataFiles"

# demographics + a broad spread of questionnaire sections, chosen for skip
# patterns (each gate question blanks a block) rather than for size
FILES = [
    "DEMO_J",     # demographics -- the spine everything joins to
    "ALQ_J",      # alcohol
    "BPQ_J",      # blood pressure / cholesterol
    "DBQ_J",      # diet
    "DIQ_J",      # diabetes
    "DPQ_J",      # depression screener
    "HIQ_J",      # health insurance
    "HUQ_J",      # hospital utilisation
    "INQ_J",      # income
    "KIQ_U_J",    # kidney
    "MCQ_J",      # medical conditions -- the widest, heavy skip logic
    "OCQ_J",      # occupation
    "PAQ_J",      # physical activity
    "RXQ_RX_J",   # prescription medications
    "SLQ_J",      # sleep
    "SMQ_J",      # smoking
    "WHQ_J",      # weight history
]


def fetch(cache: str) -> list:
    os.makedirs(cache, exist_ok=True)
    paths = []
    for name in FILES:
        dst = os.path.join(cache, name + ".xpt")
        if not os.path.exists(dst) or os.path.getsize(dst) < 4096:
            url = "{}/{}.xpt".format(BASE, name)
            sys.stderr.write("fetching {}\n".format(name))
            with urllib.request.urlopen(url, timeout=120) as r:
                body = r.read()
            if body[:4] == b"<!DO":
                sys.stderr.write("  {} unavailable (CDC returned HTML)\n"
                                 .format(name))
                continue
            with open(dst, "wb") as fh:
                fh.write(body)
        paths.append(dst)
    return paths


def merge(paths: list, dst: str) -> tuple:
    tables = []
    for p in paths:
        cols, rows = read_xpt(p)
        if "SEQN" not in cols:
            continue
        si = cols.index("SEQN")
        # RXQ_RX has several rows per respondent (one per prescription);
        # keep the first so the merge stays one row per person
        by_key = {}
        for r in rows:
            by_key.setdefault(r[si], r)
        tables.append((os.path.basename(p), cols, by_key))

    if not tables:
        raise SystemExit("no usable NHANES files were downloaded")

    # the spine is whichever file covers the most respondents
    spine = max(tables, key=lambda t: len(t[2]))
    keys = sorted(spine[2], key=lambda k: int(k))

    out_cols, seen = [], set()
    for name, cols, _ in tables:
        for j, c in enumerate(cols):
            if c == "SEQN" and "SEQN" in seen:
                continue
            cc = c if c not in seen else "{}_{}".format(c, name[:3])
            seen.add(cc)
            out_cols.append((cc, name, j))

    with open(dst, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow([c for c, _, _ in out_cols])
        lookup = {name: by_key for name, _, by_key in tables}
        for k in keys:
            row = []
            for _cc, name, j in out_cols:
                r = lookup[name].get(k)
                row.append(r[j] if r else "")
            w.writerow(row)
    return len(keys), len(out_cols)


def main(argv) -> int:
    dst = argv[1] if len(argv) > 1 else "nhanes_real.csv"
    cache = argv[2] if len(argv) > 2 else os.path.join(
        os.path.dirname(os.path.abspath(dst)), "nhanes_cache")
    paths = fetch(cache)
    nrows, ncols = merge(paths, dst)
    print("{:,} rows x {} columns   {:,} B   {}".format(
        nrows, ncols, os.path.getsize(dst), dst))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
