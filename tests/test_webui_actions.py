"""The page's actions call the dialog state as the native dialog does (site-design 5.3)."""
from __future__ import annotations

from rftools_kicad import webui as U
from rftools_kicad.dialog_logic import DialogState
from rftools_kicad.mapping import NetClassValues
from rftools_kicad.stackup import layer_model
from tests.support import (
    BROKEN_KICAD,
    FIXED_KICAD,
    FakeKiCad,
    FakeProject,
    FakeServices,
    four_layer,
    sample_classes,
)


class FakeUi:
    def __init__(self, browser: bool = True):
        self.browser = browser
        self.opened: list = []
        self.copied: list = []
        self.closed = False
        self.steps: list = []

    def open_url(self, url):
        self.opened.append(url)
        return self.browser

    def copy(self, text):
        self.copied.append(text)
        return True

    def close(self):
        self.closed = True

    def progress(self, done, count, label):
        self.steps.append((done, count))


def project_state(tmp_path, version=FIXED_KICAD, **kwargs):
    kicad = FakeKiCad(sample_classes(), version=version)
    project = FakeProject(kicad)
    classes = [NetClassValues.from_kipy(nc) for nc in project.get_net_classes()]
    services = FakeServices(tmp_path, project=project, **kwargs)
    return DialogState(layer_model(four_layer()), classes, services), kicad


def every_region(session) -> str:
    return "".join(U.render_regions(session).values())


def test_typing_in_a_field_never_rerenders_main(tmp_path):
    state, _ = project_state(tmp_path)
    session, ui = U.start(state), FakeUi()
    messages = (
        {"action": "form", "name": "HighSpeed", "field": "impedance_target", "value": "5"},
        {"action": "form", "name": "HighSpeed", "field": "selected", "value": True},
        {"action": "form", "name": "HighSpeed", "field": "extra_layers", "value": ["B.Cu"]},
        {"action": "grid", "text": "0.01"},
        {"action": "mask", "on": True},
    )
    for message in messages:
        assert "main" not in U.handle(session, message, ui).regions, message
    form = state.forms["HighSpeed"]
    assert form.impedance_target == "5" and form.selected and form.extra_layers == ("B.Cu",)
    assert state.grid_text == "0.01" and state.solder_mask_cover


def test_run_shows_results_and_spends_like_native(tmp_path):
    def prepared(path):
        state, _ = project_state(path)
        state.set_form("HighSpeed", selected=True, impedance_target="50")
        state.set_form("Power", selected=True, single_ended=False, current="2")
        return state

    (tmp_path / "web").mkdir()
    (tmp_path / "native").mkdir()
    web, native = prepared(tmp_path / "web"), prepared(tmp_path / "native")
    session, ui = U.Session(web, step="classes"), FakeUi()

    U.handle(session, {"action": "run"}, ui)
    assert session.panel == "confirm_run" and "Run" in every_region(session)
    update = U.handle(session, {"action": "run_confirmed"}, ui)
    assert set(update.regions) == set(U.REGIONS)
    assert session.step == "results" and session.panel is None and web.results

    native.refresh_usage()  # what dialog.on_run does: usage, the check, then the run
    assert native.run_check()[0]
    native.run()
    assert web.services.client.calls == native.services.client.calls
    assert ui.steps  # progress was reported


def test_a_refused_key_is_not_echoed(tmp_path):
    state, _ = project_state(tmp_path, key=None)
    session, ui = U.start(state), FakeUi()
    U.handle(session, {"action": "save_key", "text": "  rfc_bad\n"}, ui)
    assert session.panel == "key" and session.notice_error and session.notice
    assert "rfc_bad" not in every_region(session)


def test_first_run_opens_the_key_panel_without_locking(tmp_path):
    state, _ = project_state(tmp_path, key=None)
    session, ui = U.start(state), FakeUi()
    assert session.panel == "key"
    U.handle(session, {"action": "close_panel"}, ui)
    U.handle(session, {"action": "step", "step": "classes"}, ui)
    assert session.panel is None
    assert 'data-field="current"' in U.render_regions(session)["main"]
    U.handle(session, {"action": "run"}, ui)  # running needs a key: said, not done
    assert session.panel == "notice" and "API key" in session.notice


def test_open_goes_to_the_system_browser(tmp_path):
    state, _ = project_state(tmp_path)
    session, ui = U.start(state), FakeUi()
    U.handle(session, {"action": "open", "href": "https://rftools.io/docs/api/kicad/"}, ui)
    for refused in ("javascript:alert(1)", "file:///etc/passwd", "http://example.com/"):
        U.handle(session, {"action": "open", "href": refused}, ui)
    assert ui.opened == ["https://rftools.io/docs/api/kicad/"]


def test_get_key_without_a_browser_copies_the_link(tmp_path):
    state, _ = project_state(tmp_path, key=None)
    session, ui = U.start(state), FakeUi(browser=False)
    U.handle(session, {"action": "get_key"}, ui)
    assert ui.opened == [state.services.key_url] and ui.copied == [state.services.key_url]
    assert "copied to the clipboard" in session.notice


def test_write_on_10_0_6_offers_values_not_write(tmp_path):
    state, kicad = project_state(tmp_path, version=BROKEN_KICAD)
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.run()
    session, ui = U.Session(state, step="results"), FakeUi()
    U.handle(session, {"action": "write"}, ui)
    assert session.panel == "values"
    panel = U.render_regions(session)["panel"]
    assert "Copy values" in panel and 'data-action="confirm"' not in panel
    U.handle(session, {"action": "copy_values"}, ui)
    assert ui.copied == [session.preview.manual_text()] and kicad.sent == []


def test_write_previews_then_confirms_like_native(tmp_path):
    state, kicad = project_state(tmp_path)
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.run()
    session, ui = U.Session(state, step="results"), FakeUi()
    U.handle(session, {"action": "write"}, ui)
    assert session.panel == "write" and kicad.sent == []
    assert 'data-action="confirm"' in U.render_regions(session)["panel"]
    U.handle(session, {"action": "confirm"}, ui)
    assert kicad.sent  # written, through the same state.apply as the native dialog
    assert session.panel == "notice" and session.preview is None


def test_close_and_unknown_actions(tmp_path):
    state, _ = project_state(tmp_path)
    session, ui = U.start(state), FakeUi()
    assert U.handle(session, {"action": "nonsense"}, ui) == U.Update(())
    assert U.handle(session, {"action": "close"}, ui).close and ui.closed


def test_the_window_page_carries_the_bridge(tmp_path):
    state, _ = project_state(tmp_path)
    page = U.page(U.start(state), theme="dark")
    assert "window.rftoolsUpdate" in page and "rftools" in page and '"ready"' in page
