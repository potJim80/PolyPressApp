#!/bin/sh
# Compile the vendored liblzma into a static library.
#
#     xz/build.sh OUT_DIR [CFLAGS...]   -> OUT_DIR/liblzma.a
#
# Rebuilt only when a source is newer than the library. Its own warnings are
# silenced (-w): this is someone else's code, pinned, and not ours to tidy.
# Source: XZ Utils 5.8.3 release tarball, sha256 3d3a1b97...46b974a0, its
# signature checked against Lasse Collin's key (3690C240...69184620) on
# 2026-10-03. 0BSD licence (LICENSE here). Only the 18 .c files raw LZMA2
# needs, and the headers they include, are kept -- no build system, no
# tests, no .xz container, threads, checks or other filters. Output is
# byte-identical to Homebrew's liblzma 5.8.3 (checked by csrc/tests).
set -e
here=$(cd "$(dirname "$0")" && pwd)
out="$1"; shift
mkdir -p "$out"
lib="$out/liblzma.a"
flags="${*:--O2}"
stamp="$out/liblzma.flags"
if [ -f "$lib" ] && [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$flags" ] &&
   [ -z "$(find "$here" \( -name '*.[ch]' -o -name build.sh \) -newer "$lib" | head -1)" ]; then
    exit 0
fi
L="$here/liblzma"
inc="-DHAVE_CONFIG_H -I$here -I$here/common -I$L/api -I$L/common -I$L/check -I$L/lz -I$L/rangecoder -I$L/lzma -I$L/simple -I$L/delta"
rm -f "$out"/xz_*.o "$lib"
pids=""
for f in "$L"/common/*.c "$L"/lz/*.c "$L"/lzma/*.c "$L"/rangecoder/*.c "$L"/check/*.c; do
    # shellcheck disable=SC2086
    cc $flags -std=gnu99 -w $inc -c "$f" -o "$out/xz_$(basename "$f" .c).o" &
    pids="$pids $!"
done
fail=0
for p in $pids; do wait "$p" || fail=1; done
[ "$fail" = 0 ] || { echo "xz/build.sh: compile failed" >&2; exit 1; }
ar rcs "$lib" "$out"/xz_*.o
rm -f "$out"/xz_*.o
echo "$flags" > "$stamp"
