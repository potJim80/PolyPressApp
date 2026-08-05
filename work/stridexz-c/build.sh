#!/bin/sh
# Build the standalone stridexz binary. No Python, no numpy, no dependencies
# beyond liblzma.
#
#     stridexz-c/build.sh              -> stridexz-c/stridexz
#     PREFIX=/usr/local stridexz-c/build.sh install
#
# liblzma has no header in the macOS SDK even though the library ships there,
# so the include path is discovered rather than assumed.

set -e
here=$(cd "$(dirname "$0")" && pwd)
out="$here/stridexz"

CFLAGS="-O2 -std=c99 -Wall -Wextra -Wno-unused-parameter"
INC=""
LIB="-llzma"

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
    echo "build.sh: cannot find lzma.h." >&2
    echo "  macOS:  brew install xz" >&2
    echo "  Debian: apt install liblzma-dev" >&2
    exit 1
fi

echo "cc $CFLAGS $INC ... $LIB"
# shellcheck disable=SC2086
cc $CFLAGS $INC \
    "$here/sxz_util.c" "$here/sxz_encode.c" "$here/sxz_decode.c" \
    "$here/sxz_main.c" \
    -o "$out" $LIB

echo "built $out"

if [ "$1" = "install" ]; then
    prefix="${PREFIX:-/usr/local}"
    mkdir -p "$prefix/bin"
    cp "$out" "$prefix/bin/stridexz"
    echo "installed $prefix/bin/stridexz"
fi
