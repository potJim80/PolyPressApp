#!/bin/sh
# Build the standalone polypress binary.
#
#     csrc/build.sh            -> csrc/polypress
#     PREFIX=/usr/local csrc/build.sh install
#
# liblzma has no header in the macOS SDK even though the library ships there,
# so the include path is discovered rather than assumed: pkg-config first,
# then the usual Homebrew and /usr/local locations. The LIBRARY may still be
# the system one -- that is fine and in fact preferable, and it is checked:
# liblzma 5.4.3 and 5.8.3 were both verified to emit byte-identical output to
# Python's lzma module for this filter chain, which is what makes a
# byte-identical port possible.

set -e
here=$(cd "$(dirname "$0")" && pwd)
out="$here/polypress"

CFLAGS="-O2 -std=c99 -Wall -Wextra -Wno-unused-parameter"
INC=""
LIB="-llzma -lbz2"

if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists liblzma 2>/dev/null; then
    INC="$(pkg-config --cflags liblzma)"
    LIB="$(pkg-config --libs liblzma) -lbz2"
else
    for d in /opt/homebrew /usr/local /opt/local; do
        if [ -f "$d/include/lzma.h" ]; then
            INC="-I$d/include"
            LIB="-L$d/lib -llzma -lbz2"
            break
        fi
    done
fi

if [ -z "$INC" ] && [ ! -f /usr/include/lzma.h ]; then
    echo "build.sh: cannot find lzma.h." >&2
    echo "  macOS:  brew install xz" >&2
    echo "  Debian: apt install liblzma-dev libbz2-dev" >&2
    exit 1
fi

echo "cc $CFLAGS $INC ... $LIB"
# shellcheck disable=SC2086
cc $CFLAGS $INC \
    "$here/ppz_util.c" "$here/ppz_decode.c" "$here/ppz_encode.c" \
    "$here/ppz_main.c" \
    -o "$out" $LIB

echo "built $out"
"$out" 2>&1 | head -1 >/dev/null || true

if [ "$1" = "install" ]; then
    prefix="${PREFIX:-/usr/local}"
    mkdir -p "$prefix/bin"
    cp "$out" "$prefix/bin/polypress"
    echo "installed $prefix/bin/polypress"
fi
