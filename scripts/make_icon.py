"""Draw the app icon (a blue CD) and save it as assets/cd_screener.ico.

Pure standard library: each size is rendered as an anti-aliased RGBA PNG and packed into
an .ico container (Windows Vista+ reads PNG-compressed icons).

    python scripts/make_icon.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

BLUE = (42, 120, 214)  # the dashboard's series-1 blue (#2a78d6)
LIGHT = (205, 226, 251)  # a light step of the same ramp, for the label ring
OUT = Path(__file__).resolve().parent.parent / "assets" / "cd_screener.ico"


def _coverage(distance: float, radius: float) -> float:
    """1 inside the circle, 0 outside, with a 1-pixel anti-aliased edge."""
    return max(0.0, min(1.0, radius - distance + 0.5))


def render(size: int) -> bytes:
    c = (size - 1) / 2
    disc, label, hole = size * 0.47, size * 0.21, size * 0.09
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG filter type: none
        for x in range(size):
            d = math.hypot(x - c, y - c)
            outer = _coverage(d, disc)
            inner = _coverage(d, label)
            alpha = outer * (1 - _coverage(d, hole))  # the centre hole is transparent
            r, g, b = (BLUE[i] * (1 - inner) + LIGHT[i] * inner for i in range(3))
            row += bytes((round(r), round(g), round(b), round(255 * alpha)))
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def write_ico(path: Path, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    images = [render(s) for s in sizes]
    offset = 6 + 16 * len(images)
    directory, data = b"", b""
    for size, png in zip(sizes, images):
        dim = 0 if size == 256 else size  # 0 means 256 in the ICO format
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + directory + data)


if __name__ == "__main__":
    write_ico(OUT)
    print(f"Icon written to {OUT}")
