#!/bin/sh
# Build and run the Polypress test suite. Exits non-zero if anything fails.
#
#     csrc/tests/run.sh                 everything (about a minute and a half)
#     csrc/tests/run.sh codec io        just those programs
#     FUZZ=1000 SEED=7 csrc/tests/run.sh codec
#     SANITIZE=1 csrc/tests/run.sh      AddressSanitizer + UBSan build
#     PPZ_KEEP_TMP=1 ...                keep each program's temp directory
#
# Each test program links the codec sources directly (never ppz_main.c); the
# command line is tested by running a polypress binary built here from the
# same sources, so this never touches csrc/polypress. Everything built and
# written goes under one mktemp directory in $TMPDIR, removed at the end.
#
# The real-data smoke test reads ../IN/suite (xs_ and s_ tables only), and
# the Stata/SPSS/SAS test ../IN/formats; each skips itself when its directory
# is absent.

set -u
here=$(cd "$(dirname "$0")" && pwd)
src=$(cd "$here/.." && pwd)
suite_dir=$(cd "$src/../../IN/suite" 2>/dev/null && pwd || echo "")
formats_dir=$(cd "$src/../../IN/formats" 2>/dev/null && pwd || echo "")

CFLAGS="-O2 -g -std=gnu99 -pthread -Wall -Wextra -Wno-unused-parameter"
if [ "${SANITIZE:-0}" = "1" ]; then
    CFLAGS="-O1 -g -std=gnu99 -pthread -fsanitize=address,undefined -fno-omit-frame-pointer"
fi
INC=""
LIB="-llzma"
# the same lzma.h discovery as csrc/build.sh
if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists liblzma 2>/dev/null; then
    INC="$(pkg-config --cflags liblzma)"
    LIB="$(pkg-config --libs liblzma)"
else
    for d in /opt/homebrew /usr/local /opt/local; do
        if [ -f "$d/include/lzma.h" ]; then
            INC="-I$d/include"
            LIB="-L$d/lib -llzma"
            break
        fi
    done
fi
if [ -z "$INC" ] && [ ! -f /usr/include/lzma.h ]; then
    echo "run.sh: cannot find lzma.h (macOS: brew install xz)" >&2
    exit 1
fi
[ "$(uname)" = "Darwin" ] && LIB="$LIB -liconv"

work=$(mktemp -d "${TMPDIR:-/tmp}/ppz-tests-XXXXXX") || exit 1
cleanup() { rm -rf "$work"; }
trap cleanup EXIT INT TERM

CODEC="ppz_util.c ppz_io.c ppz_thread.c ppz_decode.c ppz_encode.c ppz_stream.c ppz_stat.c"

echo "building in $work"
# ReadStat (Stata/SPSS/SAS): cached in csrc/.build, keyed on the flags, so a
# sanitizer run builds its own copy
# shellcheck disable=SC2086
"$src/readstat/build.sh" "$src/.build/tests-$( echo "$CFLAGS" | cksum | cut -d' ' -f1)" $CFLAGS \
    || { echo "BUILD FAILED: readstat"; exit 1; }
LIB="$src/.build/tests-$( echo "$CFLAGS" | cksum | cut -d' ' -f1)/libreadstat.a $LIB -lz"
objs=""
for f in $CODEC; do
    o="$work/${f%.c}.o"
    # shellcheck disable=SC2086
    cc $CFLAGS $INC -c "$src/$f" -o "$o" || { echo "BUILD FAILED: $f"; exit 1; }
    objs="$objs $o"
done
# shellcheck disable=SC2086
cc $CFLAGS $INC "$src/ppz_main.c" $objs -o "$work/polypress" $LIB \
    || { echo "BUILD FAILED: polypress"; exit 1; }

all="codec io stream hostile cli stat suite"
want="${*:-$all}"
for t in $want; do
    # shellcheck disable=SC2086
    cc $CFLAGS $INC -I"$here" "$here/t_$t.c" $objs -o "$work/t_$t" $LIB \
        || { echo "BUILD FAILED: t_$t.c"; exit 1; }
done

total_pass=0
total_fail=0
failed=""
for t in $want; do
    echo
    echo "==== $t"
    case "$t" in
        codec) set -- "${FUZZ:-1000}" "${SEED:-1}" ;;
        cli)   set -- "$work/polypress" ;;
        stat)  set -- "$work/polypress" "$formats_dir" ;;
        suite) set -- "$suite_dir" ;;
        *)     set -- ;;
    esac
    start=$(date +%s)
    "$work/t_$t" "$@" > "$work/$t.log" 2>&1
    rc=$?
    cat "$work/$t.log"
    line=$(grep '^RESULT ' "$work/$t.log" | tail -1)
    if [ -z "$line" ]; then
        echo "t_$t did not finish (exit $rc) -- counted as one failure"
        total_fail=$((total_fail + 1))
        failed="$failed $t"
        continue
    fi
    p=$(echo "$line" | awk '{print $3}')
    f=$(echo "$line" | awk '{print $4}')
    total_pass=$((total_pass + p))
    total_fail=$((total_fail + f))
    [ "$f" -ne 0 ] || [ "$rc" -ne 0 ] && failed="$failed $t"
    echo "==== $t: $p passed, $f failed ($(( $(date +%s) - start )) s)"
done

echo
echo "================================================================"
echo "TOTAL: $total_pass passed, $total_fail failed"
if [ -n "$failed" ]; then
    echo "FAILED:$failed"
    exit 1
fi
echo "all passed"
exit 0
