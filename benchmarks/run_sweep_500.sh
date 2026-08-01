#!/bin/bash
# Overnight sweep of the 500-dataset unselected corpus, then the report.
#
#     ./benchmarks/run_sweep_500.sh
#
# Safe to interrupt and rerun: sweep.py skips any dataset already in the JSONL.
#
# Resources. The run must stay inside a 1-2 GB memory ceiling and roughly 20%
# of the CPU, both asked for explicitly.
#
#   * sweep.py already runs ONE dataset per subprocess, sequentially, so peak
#     memory is the largest single table rather than the accumulated total.
#   * Every numeric library is pinned to a single thread. On a 10-core machine
#     one single-threaded process is ~10% of the CPU. It also makes the
#     comparison single-threaded for *every* codec rather than only for ours,
#     which is the fairer measurement anyway -- but it does mean the speed
#     column is not comparable with earlier sweeps, which let pyarrow thread.
#   * --max-mb 16, not the default 28. CLAUDE.md's rule of (input MB x 22) +
#     700 predicted 1,316 MB for a 28 MB input and the July sweep measured
#     1,782 MB, so the rule still under-predicts. Scaling that measured worst
#     case linearly, 16 MB lands near 1,020 MB, which leaves room for the
#     fetcher to still be running alongside (it holds about 550 MB) without
#     the pair crossing the ceiling. --rss-abort is the backstop.
#
# The cap is lower than the 28 MB used for the July 100-dataset sweep, so the
# *tables* are smaller here even where the dataset is the same one. Every codec
# is still handed the identical truncated file, so it changes which table is
# being measured, not who wins on it -- but the two sweeps' absolute byte
# totals are not comparable and should not be put in the same column.
#
# Predict to schedule, measure to believe.
set -u
cd "$(dirname "$0")/.." || exit 1

export OMP_NUM_THREADS=1 ARROW_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 NUMEXPR_NUM_THREADS=1

OUT=results/socrata500.jsonl

# Two passes. The first can start while the fetcher is still downloading; the
# second picks up everything that landed in the meantime. sweep.py skips any
# dataset already in the JSONL, so the second pass costs nothing for the ones
# the first already did.
rc=0
for pass_no in 1 2 3 4 5 6; do
    before=$(wc -l < "$OUT" 2>/dev/null || echo 0)
    fetching=0
    pgrep -f "fetch_socrata100.py corpus500" >/dev/null && fetching=1

    echo "=== sweep pass $pass_no started $(date), fetcher running=$fetching ==="
    nice -n 19 python3 benchmarks/sweep.py corpus500/*.csv \
        --out "$OUT" --max-mb 16 --rss-abort 1400 --timeout 3600
    rc=$?
    after=$(wc -l < "$OUT" 2>/dev/null || echo 0)
    echo "=== sweep pass $pass_no exited $rc at $(date), $before -> $after ==="

    [ "$rc" -ne 0 ] && break
    # Stop only when the fetcher had already exited BEFORE this pass began and
    # the pass still found nothing new. Checking the fetcher afterwards would
    # drop every dataset that landed while the pass was running.
    if [ "$fetching" -eq 0 ] && [ "$after" -le "$before" ]; then
        break
    fi
done

# Report even on a partial run -- a sweep that aborted at dataset 400 is still
# 400 datasets of evidence, and having to rerun the whole night to see them
# would be the same mistake as making the fetch non-resumable.
nice -n 19 python3 benchmarks/report.py "$OUT" \
    --title "Socrata 500 (unselected)" \
    --csv results/socrata500-results.csv \
    > results/socrata500-summary.txt 2>&1
echo "=== report written $(date) ==="
tail -40 results/socrata500-summary.txt
exit $rc
