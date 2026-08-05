"""Write the app icon as a PNG, with no image libraries.

Draws a rounded square with four bars that shorten down the stack -- a table
being squeezed. Pure zlib + struct so it works on a stock Python.
"""

import struct
import sys
import zlib

W = H = 1024


def rounded(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r),
                   (x0 + r, y1 - r), (x1 - r, y1 - r)):
        inside_x = (x < x0 + r) if cx == x0 + r else (x > x1 - r)
        inside_y = (y < y0 + r) if cy == y0 + r else (y > y1 - r)
        if inside_x and inside_y:
            return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    return True


def build() -> bytes:
    px = bytearray(W * H * 4)
    bars = [(0.16, 0.66), (0.34, 0.52), (0.52, 0.38), (0.70, 0.24)]
    for y in range(H):
        t = y / (H - 1)
        # background gradient, deep indigo -> teal
        br = int(38 + 18 * t)
        bg = int(70 + 110 * t)
        bb = int(140 + 60 * (1 - t))
        for x in range(W):
            o = (y * W + x) * 4
            if not rounded(x, y, 60, 60, W - 60, H - 60, 200):
                continue
            r, g, b = br, bg, bb
            for top, frac in bars:
                y0 = int(top * H)
                y1 = y0 + int(0.115 * H)
                x0 = int(0.17 * W)
                x1 = x0 + int(frac * W * 0.78)
                if y0 <= y < y1 and x0 <= x < x1:
                    r = g = b = 255
            px[o:o + 4] = bytes((r, g, b, 255))

    raw = b"".join(b"\x00" + bytes(px[y * W * 4:(y + 1) * W * 4])
                   for y in range(H))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


if __name__ == "__main__":
    open(sys.argv[1] if len(sys.argv) > 1 else "icon.png", "wb").write(build())
