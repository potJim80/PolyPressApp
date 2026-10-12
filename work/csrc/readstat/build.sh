#!/bin/sh
# Compile the vendored ReadStat into a static library.
#
#     readstat/build.sh OUT_DIR [CFLAGS...]   -> OUT_DIR/libreadstat.a
#
# Rebuilt only when a source is newer than the library. Its own warnings are
# silenced (-w): this is someone else's code, pinned, and not ours to tidy.
# Source: github.com/WizardMac/ReadStat, branch dev at 835b88c8 (2026-09-20),
# MIT licence (LICENSE here). Only src/, src/sas, src/spss and src/stata are
# kept -- the command-line tool, the text-schema readers and the tests are not.
set -e
here=$(cd "$(dirname "$0")" && pwd)
out="$1"; shift
mkdir -p "$out"
lib="$out/libreadstat.a"
flags="${*:--O2}"
stamp="$out/libreadstat.flags"
if [ -f "$lib" ] && [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$flags" ] &&
   [ -z "$(find "$here" -name '*.[ch]' -newer "$lib" | head -1)" ]; then
    exit 0
fi
rm -f "$out"/rs_*.o "$lib"
pids=""
for f in "$here"/*.c "$here"/sas/*.c "$here"/spss/*.c "$here"/stata/*.c; do
    o="$out/rs_$(basename "$f" .c).o"
    # shellcheck disable=SC2086
    cc $flags -w -DHAVE_ZLIB=1 -c "$f" -o "$o" &
    pids="$pids $!"
done
fail=0
for p in $pids; do wait "$p" || fail=1; done
[ "$fail" = 0 ] || { echo "readstat/build.sh: compile failed" >&2; exit 1; }
ar rcs "$lib" "$out"/rs_*.o
rm -f "$out"/rs_*.o
echo "$flags" > "$stamp"
