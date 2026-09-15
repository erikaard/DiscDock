"""Draws the DiscDock logo (public/favicon.svg) into assets/discdock.ico and assets/discdock.png.

The icon is used for DiscDock.exe, its Setup and the notification area; the PNG suits places such as a GitHub
social preview. Only the Python standard library is needed: the logo is a few circles on a rounded square,
drawn with 16 to 64 samples per pixel. Run it again after changing the logo: python scripts/make-icon.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

TEAL = (0x40, 0xDD, 0xC6)
INK = (0x07, 0x10, 0x13)
LIGHT = (0xEE, 0xF7, 0xF5)
# 20 and 40 pixels are the notification area at 125% and 250% display scaling.
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
HIGHLIGHT_END = (32 + 20 * math.cos(math.radians(-30)), 32 + 20 * math.sin(math.radians(-30)))


def inside_rounded_square(x: float, y: float, radius: float) -> bool:
    if not (0 <= x <= 64 and 0 <= y <= 64):
        return False
    nearest_x = min(max(x, radius), 64 - radius)
    nearest_y = min(max(y, radius), 64 - radius)
    return (x - nearest_x) ** 2 + (y - nearest_y) ** 2 <= radius**2


def on_highlight(x: float, y: float, distance: float) -> bool:
    """The light arc along the top right of the disc, from 12 to 2 o'clock, 2 wide with round ends."""
    angle = math.degrees(math.atan2(y - 32, x - 32))
    if -90 <= angle <= -30 and abs(distance - 20) <= 1:
        return True
    return math.hypot(x - 32, y - 12) <= 1 or math.hypot(x - HIGHLIGHT_END[0], y - HIGHLIGHT_END[1]) <= 1


def layers(x: float, y: float, detailed: bool) -> list[tuple[tuple[int, int, int], float]]:
    """The colours covering one point of the 64 x 64 logo, bottom first. Small icons leave out the fine lines."""
    found = []
    distance = math.hypot(x - 32, y - 32)
    if inside_rounded_square(x, y, 16 if detailed else 14):
        found.append((TEAL, 1.0))
    if distance <= (20 if detailed else 22):
        found.append((INK, 1.0))
    if detailed and 11 <= distance <= 13:
        found.append((TEAL, 0.35))
    if distance <= (5 if detailed else 7):
        found.append((TEAL, 1.0))
    if detailed and on_highlight(x, y, distance):
        found.append((LIGHT, 0.55))
    return found


def render(size: int) -> bytes:
    """One size of the logo as a PNG with transparent corners."""
    detailed = size >= 32
    samples = 4 if size >= 64 else 6 if size >= 32 else 8
    scale = 64 / size
    count = samples * samples
    raw = bytearray()
    for row in range(size):
        raw.append(0)
        for column in range(size):
            red = green = blue = alpha = 0.0
            for sample_y in range(samples):
                for sample_x in range(samples):
                    x = (column + (sample_x + 0.5) / samples) * scale
                    y = (row + (sample_y + 0.5) / samples) * scale
                    r = g = b = a = 0.0
                    for (layer_r, layer_g, layer_b), opacity in layers(x, y, detailed):
                        r = layer_r * opacity + r * (1 - opacity)
                        g = layer_g * opacity + g * (1 - opacity)
                        b = layer_b * opacity + b * (1 - opacity)
                        a = opacity + a * (1 - opacity)
                    red, green, blue, alpha = red + r, green + g, blue + b, alpha + a
            alpha /= count
            if alpha <= 0:
                raw += b"\0\0\0\0"
                continue
            raw += bytes(min(255, round(value / count / alpha)) for value in (red, green, blue))
            raw.append(round(alpha * 255))
    return png(size, bytes(raw))


def png(size: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico(images: list[tuple[int, bytes]]) -> bytes:
    """An icon file holding every size as PNG, which Windows reads since Vista."""
    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = data = b""
    for size, image in images:
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(image), offset + len(data))
        data += image
    return directory + entries + data


def main() -> None:
    assets = Path(__file__).resolve().parents[1] / "assets"
    assets.mkdir(exist_ok=True)
    images = [(size, render(size)) for size in SIZES]
    (assets / "discdock.ico").write_bytes(ico(images))
    (assets / "discdock.png").write_bytes(dict(images)[256])
    print(f"Wrote assets/discdock.ico ({len(images)} sizes) and assets/discdock.png")


if __name__ == "__main__":
    main()
