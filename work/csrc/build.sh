#!/bin/sh
# Build polypress -- the whole program: codec, table readers and writers,
# streaming container, command line.
#
#     csrc/build.sh            -> csrc/polypress
#     PREFIX=/usr/local csrc/build.sh install
#
# xz (liblzma) and ReadStat are vendored C, each built once into .build/ and
# again only when its source changes, so the program needs nothing installed
# but a C compiler and links nothing a fresh Mac lacks.

set -e
here=$(cd "$(dirname "$0")" && pwd)
out="$here/polypress"

CFLAGS="-O2 -std=gnu99 -pthread -Wall -Wextra -Wno-unused-parameter"

# The commit this binary comes from, shown by `polypress --version`; "+" when
# the program's source has uncommitted changes. "unknown" outside git.
BUILD=$(git -C "$here" rev-parse --short HEAD 2>/dev/null || echo unknown)
if [ "$BUILD" != unknown ] && ! git -C "$here" diff --quiet HEAD -- . 2>/dev/null; then
    BUILD="$BUILD+"
fi
CFLAGS="$CFLAGS -DPPZ_BUILD=\"$BUILD\""
"$here/xz/build.sh" "$here/.build" -O2
INC="-I$here/xz/liblzma/api"
LIB="$here/.build/liblzma.a"

# iconv (for --encoding) is part of libc on Linux, a separate library on macOS
[ "$(uname)" = "Darwin" ] && LIB="$LIB -liconv"

# ReadStat, for Stata/SPSS/SAS files: vendored C, built once into .build/.
# It needs zlib (SPSS .zsav), which macOS ships and Linux has as zlib1g-dev.
"$here/readstat/build.sh" "$here/.build" -O2
LIB="$here/.build/libreadstat.a $LIB -lz"

echo "cc $CFLAGS $INC ... $LIB"
# shellcheck disable=SC2086
cc $CFLAGS $INC \
    "$here/ppz_util.c" "$here/ppz_io.c" "$here/ppz_thread.c" \
    "$here/ppz_decode.c" "$here/ppz_encode.c" "$here/ppz_stream.c" \
    "$here/ppz_stat.c" "$here/ppz_main.c" \
    -o "$out" $LIB

echo "built $out"
"$out" 2>&1 | head -1 >/dev/null || true

if [ "$1" = "install" ]; then
    prefix="${PREFIX:-/usr/local}"
    mkdir -p "$prefix/bin"
    cp "$out" "$prefix/bin/polypress"
    echo "installed $prefix/bin/polypress"
fi
