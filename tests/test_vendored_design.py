"""The site's design, vendored into the plugin (kicad-plugin-site-design 5.1).

Beside the monorepo, ui/tokens.css and ui/logo.svg must be what
scripts/vendor_design.py generates from the site today; a changed site token is
named. Standalone, the committed files must still be well formed.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "plugins" / "rftools_kicad" / "ui"
FRONTEND = ROOT.parent / "frontend"


def _vendor():
    path = ROOT / "scripts" / "vendor_design.py"
    spec = importlib.util.spec_from_file_location("vendor_design", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = _vendor()
monorepo = pytest.mark.skipif(not FRONTEND.is_dir(), reason="monorepo not present")


@monorepo
def test_tokens_match_the_site():
    generated = V.render_tokens(V.SITE_CSS.read_text(encoding="utf-8"))
    assert (UI / "tokens.css").read_text(encoding="utf-8") == generated


@monorepo
def test_logo_matches_the_site():
    generated = V.render_logo(
        V.LOGOMARK.read_text(encoding="utf-8"), V.WORDMARK.read_text(encoding="utf-8")
    )
    assert (UI / "logo.svg").read_text(encoding="utf-8") == generated


@monorepo
def test_a_changed_site_token_is_named():
    css = V.SITE_CSS.read_text(encoding="utf-8")
    changed = css.replace("--primary: #2563eb;", "--primary: #ff0000;", 1)
    assert changed != css
    assert V.token_drift(V.render_tokens(css), V.render_tokens(changed)) == ["--primary"]


@monorepo
def test_fonts_are_the_sites_families():
    fonts_ts = (FRONTEND / "src" / "lib" / "design" / "fonts.ts").read_text(encoding="utf-8")
    assert "Instrument_Sans" in fonts_ts and "IBM_Plex_Mono" in fonts_ts


def test_tokens_carry_both_themes():
    css = (UI / "tokens.css").read_text(encoding="utf-8")
    light, dark = V.token_blocks_of(css)
    assert light["--background"] == "#f5f6f8" and dark["--background"] == "#0b0d10"
    assert set(dark) <= set(light)
    assert '[data-theme="dark"]' in css and '[data-theme="auto"]' in css
    assert "prefers-color-scheme: dark" in css


def test_fonts_are_bundled_woff2_with_their_licences():
    fonts = UI / "fonts"
    for name in V.FONT_FILES:
        assert (fonts / name).read_bytes()[:4] == b"wOF2", name
    for licence in ("OFL-InstrumentSans.txt", "OFL-IBMPlexMono.txt"):
        assert "SIL OPEN FONT LICENSE" in (fonts / licence).read_text(encoding="utf-8").upper()


def test_logo_has_the_mark_and_the_wordmark():
    svg = (UI / "logo.svg").read_text(encoding="utf-8")
    assert re.search(r'<symbol id="rf-mark" viewBox="0 0 22 22">', svg)
    assert re.search(r'<symbol id="rf-wordmark" viewBox="0 0 66.45 19.52">', svg)
    assert "http" not in svg
