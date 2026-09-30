#!/bin/sh
# One command for the standard suite: build it if needed, sweep it, report it.
# See benchmarks/PROTOCOL.md. Run from work/.
#
#   ./benchmarks/run_suite.sh                  # -> OUT/results/suite-v1.*
#   ./benchmarks/run_suite.sh suite-v2         # a named run
#
# The sweep skips datasets already in the .jsonl, so this is safe to re-run
# after an interrupt. To force a full re-measure, delete the .jsonl first.
set -e

TAG="${1:-suite-v1}"
SUITE=../IN/suite
OUT=../OUT/results

# Threads change timings and, for multi-threaded zstd and 7z, change SIZES.
# Pin everything to one. PYTHONHASHSEED because set iteration order has
# reached the output bytes before.
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 ARROW_NUM_THREADS=1
export PYTHONHASHSEED=0
# The polypress encoder's threads never change its bytes (tests/ pins that),
# but they do change its time and memory, and every competitor here runs on
# one thread -- so it runs on one too.
export PPZ_THREADS=1

./csrc/build.sh >/dev/null
python3 benchmarks/make_suite.py "$SUITE"

# --max-mb 30 is above the largest file in the suite on purpose: the suite
# fixes its own sizes, and a sweep-side truncation would silently change the
# corpus out from under the manifest.
nice -n 5 python3 benchmarks/sweep.py "$SUITE"/*.csv \
    --out "$OUT/$TAG.jsonl" --max-mb 30 --rss-abort 2600 \
    2>&1 | tee "$OUT/$TAG.log"

python3 benchmarks/report.py "$OUT/$TAG.jsonl" \
    --manifest "$SUITE/MANIFEST.json" \
    --title "Polypress benchmark suite ($TAG)" \
    --csv "$OUT/$TAG-results.csv" | tee "$OUT/$TAG-summary.txt"

echo
echo "results:  $OUT/$TAG.jsonl"
echo "summary:  $OUT/$TAG-summary.txt"
echo "per-row:  $OUT/$TAG-results.csv"
