#!/usr/bin/env python3
"""Draw the plugin's icons: a microstrip cross-section (trace, dielectric, plane).

    resources/icon.png                 64 x 64, the PCM package icon (on a badge)
    plugins/icons/rftools-light-24.png toolbar icon for light themes, and 48 px
    plugins/icons/rftools-dark-24.png  toolbar icon for dark themes, and 48 px

Pure Python (zlib + struct), so the output is byte-for-byte reproducible and
needs no imaging library:

    python3 scripts/make_icons.py            # rewrite the PNGs
    python3 scripts/make_icons.py --check    # exit 1 if a committed PNG differs
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPERSAMPLE = 8

BLUE = (0x25, 0x63, 0xEB)  # rftools.io --eng-blue (light)
BLUE_DARK = (0x3B, 0x82, 0xF6)  # --eng-blue (dark)
INK = (0x1F, 0x29, 0x37)
PAPER = (0xE5, 0xE7, 0xEB)
WHITE = (0xFF, 0xFF, 0xFF)

# Shapes in unit coordinates (0..1), drawn in order: (kind, box, rgb, alpha).
# The glyph: a copper trace on a dielectric over a ground plane.
# The badge leaves a margin inside its rounded square; a toolbar glyph fills
# its frame, as KiCad's own toolbar icons do.
BADGE_GLYPH = {
    "trace": (0.36, 0.30, 0.64, 0.44),
    "dielectric": (0.14, 0.44, 0.86, 0.66),
    "plane": (0.14, 0.66, 0.86, 0.76),
}
TOOLBAR_GLYPH = {
    "trace": (0.33, 0.18, 0.67, 0.38),
    "dielectric": (0.04, 0.38, 0.96, 0.70),
    "plane": (0.04, 0.70, 0.96, 0.84),
}


def _glyph(boxes, ink, fill, fill_alpha):
    return [
        ("rect", boxes["dielectric"], fill, fill_alpha),
        ("rect", boxes["plane"], ink, 1.0),
        ("rect", boxes["trace"], ink, 1.0),
    ]


def badge_shapes():
    background = [("rounded", (0.0, 0.0, 1.0, 1.0), BLUE, 1.0)]
    return background + _glyph(BADGE_GLYPH, WHITE, WHITE, 0.38)


def toolbar_shapes(dark: bool):
    if dark:
        return _glyph(TOOLBAR_GLYPH, PAPER, BLUE_DARK, 0.75)
    return _glyph(TOOLBAR_GLYPH, INK, BLUE, 0.65)


def _inside(kind, box, x, y):
    x0, y0, x1, y1 = box
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    if kind == "rect":
        return True
    r = 0.1875 * (x1 - x0)
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(size: int, shapes) -> bytes:
    """RGBA rows, straight alpha, composited with the 'over' operator."""
    rows = bytearray()
    n = SUPERSAMPLE
    for py in range(size):
        rows.append(0)  # PNG filter type 0 for this row
        for px in range(size):
            # Premultiplied accumulation over the sub-pixel samples.
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(n):
                y = (py + (sy + 0.5) / n) / size
                for sx in range(n):
                    x = (px + (sx + 0.5) / n) / size
                    r = g = b = a = 0.0
                    for kind, box, rgb, alpha in shapes:
                        if _inside(kind, box, x, y):
                            r = rgb[0] / 255 * alpha + r * (1 - alpha)
                            g = rgb[1] / 255 * alpha + g * (1 - alpha)
                            b = rgb[2] / 255 * alpha + b * (1 - alpha)
                            a = alpha + a * (1 - alpha)
                    acc[0] += r
                    acc[1] += g
                    acc[2] += b
                    acc[3] += a
            samples = n * n
            alpha = acc[3] / samples
            if alpha > 0:
                rgb = [min(255, round(c / samples / alpha * 255)) for c in acc[:3]]
            else:
                rgb = [0, 0, 0]
            rows.extend(bytes(rgb + [round(alpha * 255)]))
    return bytes(rows)


def png(size: int, rows: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def targets():
    yield ROOT / "resources" / "icon.png", 64, badge_shapes()
    for theme in ("light", "dark"):
        for size in (24, 48):
            path = ROOT / "plugins" / "icons" / f"rftools-{theme}-{size}.png"
            yield path, size, toolbar_shapes(theme == "dark")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    stale = []
    for path, size, shapes in targets():
        data = png(size, render(size, shapes))
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"wrote {path.relative_to(ROOT)} ({size} x {size})")
    if stale:
        print(f"Icons differ from scripts/make_icons.py: {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
