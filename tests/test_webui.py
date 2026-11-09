"""webui renders the dialog state in rftools.io's design (kicad-plugin-site-design 5.2)."""
from __future__ import annotations

import html
import re
import time
from html.parser import HTMLParser

from rftools_kicad import webui as U
from rftools_kicad.dialog_logic import DialogState
from rftools_kicad.mapping import NetClassValues
from rftools_kicad.stackup import layer_model
from tests.support import KEY, FakeServices, four_layer

CLASSES = [
    NetClassValues("HighSpeed", track_width_nm=200000, diff_pair_width_nm=130000,
                   diff_pair_gap_nm=250000, clearance_nm=200000, via_diameter_nm=600000,
                   via_drill_nm=300000),
    NetClassValues("Power", track_width_nm=500000, clearance_nm=250000),
]

#: Elements a page needs to change anything, or to run code.
CONTROLS = ("<script", "<button", "<input", "<select", "<textarea", "data-action")


def state_for(tmp_path, classes=CLASSES, **kwargs) -> DialogState:
    return DialogState(layer_model(four_layer()), classes, FakeServices(tmp_path, **kwargs))


def ran(tmp_path) -> DialogState:
    """A run with a forward impedance, a 50 Ω target and a current."""
    state = state_for(tmp_path)
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.set_form("Power", selected=True, single_ended=False, current="2")
    state.run()
    assert state.results
    return state


def page_text(page: str) -> str:
    class Text(HTMLParser):
        parts: list = []

        def handle_data(self, data):
            self.parts.append(data)

    parser = Text()
    parser.parts = []
    parser.feed(page)
    return " ".join(parser.parts)


def test_every_native_value_appears(tmp_path):
    state = ran(tmp_path)
    session = U.Session(state, step="results")
    text = page_text(U.page(session, theme="dark"))
    for row in state.results:
        for cell in row.cells() + (row.note,):
            if not cell:
                continue
            # The readout shows a value apart from its method: "0.49 mm" over the method.
            parts = [p for p in U.split_value(cell) if p] if cell in (
                row.suggested, row.achieved) else [cell]
            for part in parts:
                assert part in text, (row.label, part)


def test_a_value_is_shown_apart_from_its_method():
    assert U.split_value("0.4907 mm, rftools thermal model (board-specific)") == (
        "0.4907 mm", "rftools thermal model (board-specific)")
    assert U.split_value("50.00 Ω") == ("50.00 Ω", "")
    assert U.split_value("not reached, nearest 0.3 mm") == ("not reached, nearest 0.3 mm", "")


def test_every_step_renders_its_section(tmp_path):
    state = state_for(tmp_path)
    layers = U.render_regions(U.Session(state, step="layers"))["main"]
    classes = U.render_regions(U.Session(state, step="classes"))["main"]
    assert "Layer model" in layers and "F.Cu" in layers and "<svg" in layers
    assert "Net classes" in classes and "HighSpeed" in classes and 'data-field="current"' in classes
    assert set(U.render_regions(U.Session(state))) == set(U.REGIONS)


def test_no_remote_urls(tmp_path):
    page = U.page(U.Session(ran(tmp_path), step="results"), theme="light")
    loaded = re.findall(r'(?:src|href)="([^"]*)"|url\(([^)]*)\)', page)
    remote = [a or b for a, b in loaded if (a or b).strip("'\" ").startswith("http")]
    assert remote == []
    assert "data-href=" in U.render_regions(U.Session(state_for(tmp_path), panel="key"))["panel"]


def test_key_never_appears(tmp_path):
    state = state_for(tmp_path)
    for panel in (None, "key"):
        page = U.page(U.Session(state, panel=panel), theme="dark")
        assert KEY not in page and KEY[:13] not in page
    # The key panel shows the public identifier and where the key comes from.
    panel = U.render_regions(U.Session(state, panel="key"))["panel"]
    assert state.services.settings.key_id in panel


def test_names_are_escaped(tmp_path):
    name = '<HS>&"diff"'
    state = state_for(tmp_path, classes=[NetClassValues(name, track_width_nm=200000)])
    main = U.render_regions(U.Session(state, step="classes"))["main"]
    assert name not in main
    assert html.escape(name) in main


def test_report_has_no_controls(tmp_path):
    page = U.page(U.Session(ran(tmp_path)), theme="auto", controls=False)
    for control in CONTROLS:
        assert control not in page, control
    assert "Layer model" in page and "Results" in page


def test_the_theme_is_set_on_the_page(tmp_path):
    state = state_for(tmp_path)
    for theme in ("light", "dark", "auto"):
        assert f'<html lang="en" data-theme="{theme}">' in U.page(U.Session(state), theme=theme)


def test_the_page_carries_the_sites_tokens_fonts_and_logo(tmp_path):
    page = U.page(U.Session(state_for(tmp_path)), theme="dark")
    assert "--background: #0b0d10;" in page and "--primary: #2563eb;" in page
    assert page.count("@font-face") == 4 and "font/woff2;base64," in page
    assert '<symbol id="rf-mark"' in page


def test_forty_classes_render_quickly_and_scroll(tmp_path):
    stackup = four_layer()
    classes = [NetClassValues(f"Class{i}", track_width_nm=150000 + i * 1000) for i in range(40)]
    state = DialogState(layer_model(stackup), classes, FakeServices(tmp_path))
    started = time.perf_counter()
    for step in U.STEPS:
        U.render_regions(U.Session(state, step=step))
    assert time.perf_counter() - started < 0.2
    css = (U.UI_DIR / "plugin.css").read_text(encoding="utf-8")
    assert re.search(r"\.main\s*\{[^}]*overflow:\s*auto", css)
