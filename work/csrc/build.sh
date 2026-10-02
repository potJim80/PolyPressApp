#!/bin/sh
# Build polypress -- the whole program: codec, table readers and writers,
# streaming container, command line.
#
#     csrc/build.sh            -> csrc/polypress
#     PREFIX=/usr/local csrc/build.sh install
#
# liblzma has no header in the macOS SDK even though the library ships there,
# so the include path is discovered rather than assumed: pkg-config first,
# then the usual Homebrew and /usr/local locations.

set -e
here=$(cd "$(dirname "$0")" && pwd)
out="$here/polypress"

CFLAGS="-O2 -std=gnu99 -pthread -Wall -Wextra -Wno-unused-parameter"
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

# liblzma is not part of macOS, so a binary linked to Homebrew's copy only
# runs on Macs that have Homebrew's xz. Link the static archive when there is
# one, so the program -- and the app that ships it -- runs anywhere.
# (iconv ships with the OS and stays dynamic.)
for a in $(echo "$LIB" | tr ' ' '\n' | sed -n 's/^-L//p') /opt/homebrew/lib /usr/local/lib; do
    if [ -f "$a/liblzma.a" ]; then
        LIB="$(echo "$LIB" | sed 's/-llzma//') $a/liblzma.a"
        break
    fi
done

# iconv (for --encoding) is part of libc on Linux, a separate library on macOS
[ "$(uname)" = "Darwin" ] && LIB="$LIB -liconv"

echo "cc $CFLAGS $INC ... $LIB"
# shellcheck disable=SC2086
cc $CFLAGS $INC \
    "$here/ppz_util.c" "$here/ppz_io.c" "$here/ppz_thread.c" \
    "$here/ppz_decode.c" "$here/ppz_encode.c" "$here/ppz_stream.c" \
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
