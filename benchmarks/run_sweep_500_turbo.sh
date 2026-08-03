#!/bin/bash
# The 500-dataset unselected sweep, measuring BOTH encoders against every
# competitor on the same tables.
#
#     ./benchmarks/run_sweep_500_turbo.sh
#
# Same resource discipline as run_sweep_500.sh -- one dataset per subprocess,
# every numeric library pinned to one thread, nice 19, --max-mb 16 so peak RSS
# stays near 1 GB, --rss-abort as the backstop. Safe to interrupt: sweep.py
# skips any dataset already in the JSONL.
#
# Writes a NEW jsonl rather than reusing results/socrata500.jsonl, because that
# file already has 500 records without a turbo row and sweep.py would skip
# every one of them.
set -u
cd "$(dirname "$0")/.." || exit 1

export OMP_NUM_THREADS=1 ARROW_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 NUMEXPR_NUM_THREADS=1

OUT=results/socrata500-turbo.jsonl
rc=0
for pass_no in 1 2 3; do
    before=$(wc -l < "$OUT" 2>/dev/null || echo 0)
    echo "=== pass $pass_no started $(date) ==="
    nice -n 19 python3 benchmarks/sweep.py corpus500/*.csv \
        --out "$OUT" --max-mb 12 --rss-abort 1400 --timeout 3600
    rc=$?
    after=$(wc -l < "$OUT" 2>/dev/null || echo 0)
    echo "=== pass $pass_no exited $rc at $(date), $before -> $after ==="
    [ "$rc" -ne 0 ] && break
    [ "$after" -le "$before" ] && break
done

for who in polypress polypress-turbo; do
    nice -n 19 python3 benchmarks/report.py "$OUT" --ours "$who" \
        --title "Socrata 500 (unselected) -- $who" \
        --csv "results/socrata500-${who}-results.csv" \
        > "results/socrata500-${who}-summary.txt" 2>&1
done
echo "=== reports written $(date) ==="
exit $rc
