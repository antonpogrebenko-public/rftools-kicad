"""The HTML report for a platform without wx (task 4.2): results, provenance, no write control."""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser

import pytest

from rftools_kicad import main as M
from rftools_kicad import report as R
from rftools_kicad.mapping import NetClassValues
from rftools_kicad.stackup import layer_model
from tests.support import (
    KEY,
    PROVENANCE,
    USAGE,
    FakeClient,
    FakeKiCad,
    FakeProject,
    FakeServices,
    api_error,
    four_layer,
    sample_classes,
)

CLASSES = [
    NetClassValues("HighSpeed", track_width_nm=200000, diff_pair_width_nm=130000,
                   diff_pair_gap_nm=250000, clearance_nm=200000, via_diameter_nm=600000,
                   via_drill_nm=300000),
    NetClassValues("Power", track_width_nm=500000, clearance_nm=250000),
]

#: Elements a page would need to change anything, or to run code.
CONTROLS = {"form", "input", "button", "select", "textarea", "script", "iframe", "object",
            "embed"}


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attrs = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(name for name, _ in attrs)

    def handle_data(self, data):
        self.text.append(data)


def parse(page):
    tags = Tags()
    tags.feed(page)
    return tags


def unwritable_project():
    kicad = FakeKiCad(sample_classes())

    def refuse(command):
        pytest.fail("the report sent KiCad a command")

    kicad.on_send = refuse
    return FakeProject(kicad)


def services_for(tmp_path, **kwargs):
    kwargs.setdefault("project", unwritable_project())
    return FakeServices(tmp_path, **kwargs)


def board():
    """Four layers, planes typed, B.Cu's prepreg thicker than F.Cu's: every
    class's two outer layers are different requests (4 calls for 2 classes)."""
    stackup = four_layer()
    stackup["layers"][4]["sublayers"][0]["thicknessNm"] = 1030000
    stackup["layers"][6]["sublayers"][0]["thicknessNm"] = 210000
    return layer_model(stackup)


def make_report(tmp_path, services, model=None, classes=CLASSES):
    opened = []
    code = R.present(model or board(), classes, services,
                     opener=opened.append, directory=str(tmp_path))
    assert code == 0
    [uri] = opened
    assert uri.startswith("file://")
    [path] = tmp_path.glob("rftools-kicad-report-*.html")
    return path.read_text(encoding="utf-8")


def test_the_report_renders_model_plan_results_and_provenance_with_no_write_control(tmp_path):
    services = services_for(tmp_path)
    page = make_report(tmp_path, services)
    tags = parse(page)
    assert not CONTROLS & set(tags.tags), CONTROLS & set(tags.tags)
    assert not [a for a in tags.attrs if a.startswith("on")]  # no event handlers
    text = " ".join(tags.text)
    for gone in ("Apply to net classes", "Restore"):
        assert gone not in text
    assert "Nothing was written to the board or the project." in text

    # The layer model, as the model describes it.
    assert "F.Cu: microstrip over In1.Cu" in text and "In2.Cu: reference plane." in text
    # The planned calculations: every class on every trace layer, forward only.
    assert "This run will use at most 4 API calls; 40 of 50 remain this month" in text
    assert text.count("microstrip-impedance") >= 2
    assert "traceWidth 0.2" in text and "traceWidth 0.5" in text
    # The results, with the provenance of each.
    assert PROVENANCE["formulaRef"] in text and "api@0123456789ab" in text
    assert "Z₀ at the class width" in text and "inside" in text
    assert [c[0] for c in services.client.calls] == ["usage"] + ["calculate"] * 4
    # How to get the dialog.
    assert "python3-wxgtk4.0" in text and "Recreate Plugin Environment" in text
    assert "https://rftools.io/privacy/" in page


def test_the_report_renders_recorded_results_offline(tmp_path):
    # Record a run, then render the report with the service unreachable.
    services = services_for(tmp_path)
    first = tmp_path / "first"
    first.mkdir()
    make_report(first, services)
    offline = FakeClient()
    offline.usage_error = ConnectionError("no route to host")
    services.client = offline
    page = make_report(tmp_path, services)
    text = " ".join(parse(page).text)
    assert "at most 0 API calls (4 more answered from the cache)" in text
    assert "The allowance remaining could not be read: Could not reach rftools.io" in text
    assert text.count("cached") >= 4 and "api@0123456789ab" in text
    assert offline.metered == []


def test_offline_with_nothing_cached_says_so_and_spends_nothing(tmp_path):
    offline = FakeClient()
    offline.usage_error = ConnectionError("no route to host")
    page = make_report(tmp_path, services_for(tmp_path, client=offline))
    text = " ".join(parse(page).text)
    assert "Only cached results are shown: the allowance remaining could not be read" in text
    assert "Not computed" in text and offline.metered == []


def test_a_run_larger_than_the_allowance_spends_nothing(tmp_path):
    client = FakeClient(usage=dict(USAGE, remaining=3))
    page = make_report(tmp_path, services_for(tmp_path, client=client))
    text = " ".join(parse(page).text)
    assert "it needs 4 API calls and 3 remain this month" in text
    assert "Not computed" in text
    assert client.metered == []


def test_without_a_key_nothing_is_computed_and_the_key_link_is_given(tmp_path):
    client = FakeClient()
    page = make_report(tmp_path, services_for(tmp_path, key=None, client=client))
    text = " ".join(parse(page).text)
    assert "No API key is set" in text
    assert "https://rftools.io/dashboard/?newKey=1&client=kicad-plugin" in text
    assert "RFTOOLS_API_KEY" in text
    assert client.calls == []


def test_a_refusal_is_shown_beside_what_was_computed(tmp_path):
    client = FakeClient()
    count = {"n": 0}

    def calculate(slug, inputs):
        count["n"] += 1
        if count["n"] == 3:
            raise api_error("quota")
        return FakeClient._default_calculate(slug, inputs)

    client._calculate = calculate
    page = make_report(tmp_path, services_for(tmp_path, client=client))
    text = " ".join(parse(page).text)
    assert "Refusals" in text and "used up" in text and "2026-10-01T00:00:00Z" in text
    assert text.count("50.00 Ω") == 2  # the two computed before the 402


def test_the_key_never_reaches_the_page(tmp_path):
    services = services_for(tmp_path)
    page = make_report(tmp_path, services)
    assert KEY not in page and KEY[:13] not in page
    assert "rfc_TESTkey0" in page  # the public id is shown
    assert not re.search(r"rfc_[A-Za-z0-9_\-]{9,}", page)


def test_render_alone_is_a_page_without_controls(tmp_path):
    from rftools_kicad.dialog_logic import DialogState

    state = DialogState(board(), CLASSES, services_for(tmp_path))
    page = R.render(state, ["a note"])
    assert not CONTROLS & set(parse(page).tags)
    assert "Nothing was computed." in page and "a note" in page


def test_main_falls_back_to_the_report_when_wx_cannot_load(monkeypatch):
    monkeypatch.setitem(sys.modules, "wx", None)  # import wx fails
    monkeypatch.delitem(sys.modules, "rftools_kicad.dialog", raising=False)
    assert M.presenter() is R.present
