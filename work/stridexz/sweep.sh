#!/bin/bash
# One subprocess per table, so peak memory is the largest single table rather
# than the accumulated total -- the same rule benchmarks/sweep.py follows.
# Resumable: a table already in the .jsonl is skipped.
#
#   ./xzlab/sweep.sh out.jsonl file1.csv file2.csv ...
set -u
OUT="$1"; shift
export PROBE_MAX_MB="${PROBE_MAX_MB:-16}"
export XZLAB_JSONL="$OUT"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
touch "$OUT"
n=0
for f in "$@"; do
    n=$((n + 1))
    base=$(basename "$f")
    if grep -qF "\"file\": \"$base\"" "$OUT" 2>/dev/null; then
        echo "[$n/$#] skip $base (already done)"
        continue
    fi
    echo "[$n/$#] $base"
    nice -n 10 python3 xzlab/bench.py "$f" 2>&1 | sed 's/^/    /'
done
echo "done -> $OUT"
