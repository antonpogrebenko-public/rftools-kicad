"""The wx view over DialogState, where wx and a display are available (skipped otherwise).

CI runs on Linux without wxPython, so these run on a developer's machine. The
dialog is built and driven without being shown modally; everything it decides
is tested in test_dialog_logic.py.
"""
from __future__ import annotations

import pytest

try:
    import wx
except Exception as exc:  # absent (CI), or present but unable to load its libraries
    pytest.skip(f"wx cannot be imported: {exc}", allow_module_level=True)

from rftools_kicad import dialog as DLG  # noqa: E402
from rftools_kicad.dialog_logic import DialogState  # noqa: E402
from rftools_kicad.mapping import NetClassValues  # noqa: E402
from rftools_kicad.stackup import layer_model  # noqa: E402
from tests.support import (  # noqa: E402
    BROKEN_KICAD,
    FIXED_KICAD,
    FakeClient,
    FakeKiCad,
    FakeProject,
    FakeServices,
    four_layer,
    sample_classes,
)
from tests.test_dialog_logic import answer, solver  # noqa: E402


@pytest.fixture(scope="module")
def app():
    try:
        application = wx.App(False)
    except (Exception, SystemExit) as exc:
        pytest.skip(f"wx cannot open a display here: {exc}")
    yield application


def make_dialog(tmp_path, version=FIXED_KICAD):
    project = FakeProject(FakeKiCad(sample_classes(), version=version))
    classes = [NetClassValues.from_kipy(nc) for nc in project.get_net_classes()]
    services = FakeServices(tmp_path, client=FakeClient(calculate=answer, solve=solver()),
                            project=project)
    state = DialogState(layer_model(four_layer(roles=False)), classes, services)
    return DLG.MainDialog(None, state)


@pytest.fixture
def dialog(app, tmp_path):
    window = make_dialog(tmp_path)
    yield window
    window.Destroy()


@pytest.fixture
def old_kicad_dialog(app, tmp_path):
    window = make_dialog(tmp_path, version=BROKEN_KICAD)
    yield window
    window.Destroy()


def controls(parent, kind):
    return [c for c in parent.GetChildren() if isinstance(c, kind)]


def labels(parent):
    return [c.GetLabel() for c in parent.GetChildren() if isinstance(c, wx.StaticText)]


def test_the_layer_page_has_a_plane_box_per_copper_layer(dialog):
    boxes = controls(dialog.layers_page, wx.CheckBox)
    assert [b.GetValue() for b in boxes] == [True, True, True, True]
    text = labels(dialog.layers_page)
    assert "F.Cu" in text and any(t.startswith("F.Cu: microstrip over In1.Cu") for t in text)
    dialog.state.set_plane("F.Cu", False)
    dialog._model_changed()
    boxes = controls(dialog.layers_page, wx.CheckBox)
    assert [b.GetValue() for b in boxes] == [False, True, True, True]


def test_the_class_page_has_a_row_per_class_and_the_budget_follows_the_form(dialog):
    names = [b.GetLabel() for b in controls(dialog.classes_page, wx.CheckBox) if b.GetLabel()]
    assert names[1:] == ["Default", "HighSpeed", "USB", "Power"]  # after the mask option
    choices = controls(dialog.classes_page, wx.Choice)
    assert len(choices) == 4 and choices[0].GetStringSelection() == "F.Cu"
    dialog.on_form("HighSpeed", selected=True, impedance_target="50")
    assert dialog.budget_text.GetLabel() == dialog.state.budget_line()
    assert "at most 2 API calls" in dialog.budget_text.GetLabel()
    dialog.on_form("HighSpeed", current="x")
    assert "HighSpeed: the current must be a number" in dialog.form_errors.GetLabel()


def test_results_fill_the_table_and_enable_apply(dialog):
    assert not dialog.apply_button.IsEnabled()
    dialog.state.set_form("HighSpeed", selected=True, impedance_target="50")
    dialog.state.run()
    dialog._fill_results()
    dialog._refresh()
    assert dialog.table.GetItemCount() == 2
    assert dialog.table.GetItemText(1, 4) == "0.284 mm"
    assert dialog.apply_button.IsEnabled()


def test_the_preview_lists_three_fields_per_class(dialog):
    dialog.state.set_form("HighSpeed", selected=True, impedance_target="50")
    dialog.state.run()
    preview, reason = dialog.state.preview()
    assert reason is None
    with DLG.PreviewDialog(dialog, preview) as window:
        [table] = controls(window, wx.ListCtrl)
        assert table.GetItemCount() == 3
        assert [table.GetItemText(i, 3) for i in range(3)] == [
            "0.284 mm", "unchanged", "unchanged",
        ]
        buttons = [b.GetLabel() for b in controls(window, wx.Button)]
        assert "Write to net classes" in buttons


def test_the_write_buttons_say_what_they_do(dialog):
    assert dialog.apply_button.GetLabel() == DLG.APPLY_LABEL
    assert dialog.restore_button.GetLabel() == DLG.RESTORE_LABEL
    assert dialog.write_note.GetLabel() == ""


def test_on_kicad_10_0_6_the_values_are_offered_for_board_setup(old_kicad_dialog, monkeypatch):
    dialog = old_kicad_dialog
    state = dialog.state
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.run()
    dialog._fill_results()
    dialog._refresh()
    assert dialog.apply_button.GetLabel() == DLG.MANUAL_APPLY_LABEL
    assert dialog.apply_button.IsEnabled()
    assert dialog.restore_button.GetLabel() == DLG.MANUAL_RESTORE_LABEL
    assert "KiCad 10.0.6 and earlier corrupt" in dialog.write_note.GetLabel()

    preview, reason = state.preview()
    assert reason is None and preview.manual
    copied = []
    monkeypatch.setattr(DLG, "copy_text", lambda text: copied.append(text) or True)
    with DLG.PreviewDialog(dialog, preview) as window:
        buttons = [b.GetLabel() for b in controls(window, wx.Button)]
        assert DLG.COPY_LABEL in buttons and "Close" in buttons
        assert "Write to net classes" not in buttons
        [table] = controls(window, wx.ListCtrl)
        assert table.GetColumn(1).GetText() == "Board Setup column"
        assert [table.GetItemText(i, 1) for i in range(3)] == ["Track Width", "DP Width",
                                                               "DP Gap"]
        assert [table.GetItemText(i, 3) for i in range(3)] == [
            "0.284 mm", "unchanged", "unchanged",
        ]
        assert any("KiCad 10.0.6 and earlier corrupt" in text for text in labels(window))
        window.on_copy(None)
        assert copied == [preview.manual_text()]
        assert window.copied.GetLabel() == "Copied."
    assert state.services.project._kicad.sent == []


def test_without_a_display_present_falls_back_to_the_report(monkeypatch, tmp_path):
    from rftools_kicad import report

    def no_display(*args, **kwargs):
        raise SystemExit("Unable to access the X Display")

    shown = []
    monkeypatch.setattr(DLG.wx, "App", no_display)
    monkeypatch.setattr(report, "present", lambda *args: shown.append(args) or 0)
    assert DLG.present("model", ["classes"], "services") == 0
    assert shown == [("model", ["classes"], "services")]
