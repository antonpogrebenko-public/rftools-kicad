"""The plugin's icons are the rftools.io logo mark (kicad-plugin-site-design 1.1)."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOGOMARK = ROOT.parent / "frontend" / "src" / "components" / "instrument" / "icons" / "LogoMark.tsx"


def _make_icons():
    spec = importlib.util.spec_from_file_location("make_icons", ROOT / "scripts" / "make_icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _make_icons()


def pixel(size: int, x: float, y: float) -> tuple:
    """RGBA of the pixel at unit coordinates (x, y) of the logo drawn at *size*."""
    rows = M.render(size, M.logo_shapes())
    stride = 1 + size * 4  # each row starts with its PNG filter byte
    i = int(y * size) * stride + 1 + int(x * size) * 4
    return tuple(rows[i:i + 4])


def test_corner_is_transparent():
    assert pixel(64, 0, 0)[3] == 0


def test_field_is_logo_blue():
    assert pixel(64, 0.5, 0.1) == (0x25, 0x63, 0xEB, 255)


def test_centre_line_is_white():
    assert pixel(64, 0.5, 0.5) == (255, 255, 255, 255)


def test_every_icon_is_the_logo():
    for path, _size, shapes in M.targets():
        assert shapes == M.logo_shapes(), path


def _attrs(tag: str) -> dict:
    return dict(re.findall(r'(\w+)="([^"]*)"', tag))


@pytest.mark.skipif(not LOGOMARK.is_file(), reason="monorepo not present")
def test_geometry_matches_the_sites_logomark():
    source = LOGOMARK.read_text(encoding="utf-8")
    rect = _attrs(re.search(r"<rect [^>]*>", source).group(0))
    assert float(rect["width"]) == float(rect["height"]) == M.LOGO["rect"]["size"]
    assert float(rect["rx"]) == M.LOGO["rect"]["rx"]
    assert rect["fill"].lower() == M.LOGO["rect"]["fill"]

    circles = [_attrs(tag) for tag in re.findall(r"<circle [^>]*>", source)]
    assert [
        {
            "cx": float(c["cx"]), "cy": float(c["cy"]), "r": float(c["r"]),
            "stroke": float(c["strokeWidth"]), "opacity": float(c.get("opacity", "1")),
        }
        for c in circles
    ] == M.LOGO["rings"]

    path = _attrs(re.search(r"<path [^>]*>", source).group(0))
    x0, y, dx = (float(v) for v in re.fullmatch(r"M([\d.]+) ([\d.]+)h([\d.]+)", path["d"]).groups())
    line = M.LOGO["line"]
    assert (x0, x0 + dx, y, float(path["strokeWidth"])) == (
        line["x0"], line["x1"], line["y"], line["stroke"]
    )
