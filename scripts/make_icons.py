#!/usr/bin/env python3
"""Draw the plugin's icons: the rftools.io logo mark.

    resources/icon.png                 64 x 64, the PCM package icon
    plugins/icons/rftools-light-24.png toolbar icon for light themes, and 48 px
    plugins/icons/rftools-dark-24.png  toolbar icon for dark themes, and 48 px

Every icon is the site's mark (frontend/src/components/instrument/icons/
LogoMark.tsx), which is the same on light and dark pages; tests/test_icons.py
compares LOGO with it when the monorepo is present.

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

#: The rftools.io logo mark in the site's own units (LogoMark.tsx, viewBox 22):
#: a blue rounded square, two white rings (the second at 75 % opacity) and a
#: white line across the middle, drawn in that order.
LOGO_VIEWBOX = 22
LOGO = {
    "rect": {"size": 22, "rx": 5, "fill": "#2563eb"},
    "rings": [
        {"cx": 11, "cy": 11, "r": 6.5, "stroke": 1.4, "colour": "#fff", "opacity": 1.0},
        {"cx": 13.2, "cy": 11, "r": 4.3, "stroke": 1.1, "colour": "#fff", "opacity": 0.75},
    ],
    "line": {"x0": 4.5, "x1": 17.5, "y": 11, "stroke": 1.1, "colour": "#fff"},
}


def _rgb(hex_colour: str) -> tuple:
    value = hex_colour.lstrip("#")
    if len(value) == 3:  # "#fff" is "#ffffff"
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def logo_shapes():
    """The mark as shapes in unit coordinates (0..1): (kind, box, rgb, alpha)."""
    u = 1 / LOGO_VIEWBOX
    rect, line = LOGO["rect"], LOGO["line"]
    shapes = [("rounded", (0.0, 0.0, 1.0, 1.0, rect["rx"] * u), _rgb(rect["fill"]), 1.0)]
    for ring in LOGO["rings"]:
        box = (ring["cx"] * u, ring["cy"] * u, ring["r"] * u, ring["stroke"] * u)
        shapes.append(("ring", box, _rgb(ring["colour"]), ring["opacity"]))
    half = line["stroke"] / 2  # an SVG line's butt caps end exactly at x0 and x1
    box = (line["x0"] * u, (line["y"] - half) * u, line["x1"] * u, (line["y"] + half) * u)
    shapes.append(("rect", box, _rgb(line["colour"]), 1.0))
    return shapes


def _inside(kind, box, x, y):
    if kind == "ring":  # box: centre x, centre y, radius, stroke width
        cx, cy, r, stroke = box
        distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        return r - stroke / 2 <= distance <= r + stroke / 2
    x0, y0, x1, y1 = box[:4]
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    if kind == "rect":
        return True
    r = box[4]  # "rounded": box carries its corner radius
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
    yield ROOT / "resources" / "icon.png", 64, logo_shapes()
    for theme in ("light", "dark"):
        for size in (24, 48):
            path = ROOT / "plugins" / "icons" / f"rftools-{theme}-{size}.png"
            yield path, size, logo_shapes()


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
