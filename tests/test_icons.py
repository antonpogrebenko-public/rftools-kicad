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


FRONTEND = ROOT.parent / "frontend"


def _attrs(body: str) -> dict:
    return dict(re.findall(r'(\w+)="([^"]*)"', body))


def _same(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).lower() == str(b).lower()


def expected_elements() -> list:
    """LogoMark.tsx's elements as LOGO describes them, every attribute included."""
    rect, line = M.LOGO["rect"], M.LOGO["line"]
    elements = [("rect", {"width": rect["size"], "height": rect["size"], "rx": rect["rx"],
                          "fill": rect["fill"]})]
    for ring in M.LOGO["rings"]:
        attrs = {"cx": ring["cx"], "cy": ring["cy"], "r": ring["r"], "fill": "none",
                 "stroke": ring["colour"], "strokeWidth": ring["stroke"]}
        if ring["opacity"] != 1:
            attrs["opacity"] = ring["opacity"]
        elements.append(("circle", attrs))
    d = "M{:g} {:g}h{:g}".format(line["x0"], line["y"], line["x1"] - line["x0"])
    elements.append(("path", {"d": d, "stroke": line["colour"], "strokeWidth": line["stroke"]}))
    return elements


def logomark_mismatches(source: str) -> list:
    """How LogoMark.tsx's drawing differs from LOGO: element order, attributes, values."""
    svg = re.search(r"<svg[^>]*>(.*?)</svg>", source, re.S)
    if not svg:
        return ["no <svg> element"]
    found = [(tag, _attrs(body)) for tag, body in re.findall(r"<(\w+)\b([^>]*?)/?>", svg.group(1))]
    expected = expected_elements()
    if [tag for tag, _ in found] != [tag for tag, _ in expected]:
        return [f"elements {[t for t, _ in found]} != {[t for t, _ in expected]}"]
    problems = []
    for (tag, have), (_, want) in zip(found, expected):
        if set(have) != set(want):
            problems.append(f"<{tag}> attributes {sorted(have)} != {sorted(want)}")
            continue
        problems += [
            f"<{tag}> {k}={have[k]!r} != {want[k]!r}"
            for k in want if not _same(have[k], want[k])
        ]
    return problems


def _logomark_source() -> str:
    if not FRONTEND.is_dir():
        pytest.skip("monorepo not present")
    assert LOGOMARK.is_file(), f"the site's logo moved: {LOGOMARK} is missing"
    return LOGOMARK.read_text(encoding="utf-8")


def test_geometry_matches_the_sites_logomark():
    assert logomark_mismatches(_logomark_source()) == []


@pytest.mark.parametrize("old, new", [
    ('stroke="#fff" strokeWidth="1.4"', 'stroke="#f59e0b" strokeWidth="1.4"'),  # amber ring
    ('<path d=', '<path strokeLinecap="round" d='),  # round line caps
    ("</svg>", '<line x1="0" y1="0" x2="1" y2="1" /></svg>'),  # an extra element
    ('cy="11" r="4.3" fill="none"', 'cy="11" r="4.3" fill="#fff"'),  # a filled ring
])
def test_a_changed_logomark_is_caught(old, new):
    source = _logomark_source()
    mutated = source.replace(old, new, 1)
    assert mutated != source, f"mutation {old!r} did not apply"
    assert logomark_mismatches(mutated)
