"""The dialog's decisions, tested without a display (task 4.1).

dialog_logic.DialogState holds everything the wx dialog shows and does; the
wx layer only draws it. What these tests cannot reach — widgets drawing,
KiCad launching the action, a real SetNetClasses — is the manual run in
docs/manual-tests.md.
"""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from rftools_kicad import dialog_logic as D
from rftools_kicad import netclasses as N
from rftools_kicad.mapping import (
    COPLANAR,
    CURRENT,
    MICROSTRIP,
    PRIMARY_OUTPUT,
    STRIPLINE,
    VIA,
    NetClassValues,
)
from rftools_kicad.settings import KEY_URL
from rftools_kicad.stackup import from_kipy, layer_model
from tests.support import (
    BROKEN_KICAD,
    FIXED_KICAD,
    KEY,
    PROVENANCE,
    USAGE,
    FakeBoard,
    FakeClient,
    FakeKiCad,
    FakeProject,
    FakeServices,
    calc_response,
    four_layer,
    golden_case,
    kipy_stackup,
    missing_er,
    sample_classes,
    sdk_available,
    sdk_error,
    six_layer,
    solve_response,
    two_layer,
)

HIGH = NetClassValues(
    "HighSpeed", track_width_nm=200000, diff_pair_width_nm=130000, diff_pair_gap_nm=250000,
    clearance_nm=200000, via_diameter_nm=600000, via_drill_nm=300000,
)
USB = NetClassValues(
    "USB", track_width_nm=147000, diff_pair_width_nm=140000, diff_pair_gap_nm=154000,
    clearance_nm=200000, via_diameter_nm=450000, via_drill_nm=200000,
)
POWER = NetClassValues("Power", track_width_nm=500000, clearance_nm=250000)
CLASSES = [HIGH, USB, POWER]

VALUES = {
    CURRENT: {"width2221mm": 0.7859736537100402, "width2152mm": 0.5894802402825302},
    VIA: {"impedance": 48.21, "capacitancePF": 0.412, "inductanceNH": 1.02,
          "currentCapacityA": 3.1, "aspectRatio": 5.3},
}


def answer(slug, inputs):
    values = VALUES.get(slug) or {PRIMARY_OUTPUT[slug]: 52.25}
    return calc_response(slug, inputs, dict(values))


def solver(value=0.284, reached=True, achieved=None):
    def solve(slug, inputs, solve_for, output, target, grid, range_):
        return solve_response(slug, inputs, solve_for, output, target, value=value,
                              reached=reached, achieved=achieved, grid=grid)
    return solve


def state_for(tmp_path, model=None, classes=CLASSES, **kwargs):
    client = kwargs.pop("client", None) or FakeClient(calculate=answer, solve=solver())
    services = FakeServices(tmp_path, client=client, **kwargs)
    return D.DialogState(model or layer_model(two_layer()), classes, services)


# ── (a) The layer model under review ─────────────────────────────────────────


def test_each_copper_layer_is_listed_with_its_proposed_structure_and_plane(tmp_path):
    state = state_for(tmp_path, layer_model(four_layer(roles=False)))
    rows = state.layer_rows()
    assert [r.name for r in rows] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert all(r.plane and r.computed for r in rows)  # the adjacency proposal
    assert rows[0].text == state.model.structure("F.Cu").describe()
    assert rows[0].text.startswith("F.Cu: microstrip over In1.Cu, 0.2 mm of dielectric")
    assert "does not say which copper layers are planes" in state.plane_source_text()
    assert state.services.client.calls == []  # nothing computed to show the model


def test_marking_the_planes_recomputes_the_structures_before_any_call(tmp_path):
    case = golden_case("G2")
    # As KiCad sends it: no layer roles, so every layer starts as a plane.
    state = state_for(tmp_path, layer_model(from_kipy(kipy_stackup(case["stackup"]))))
    for signal in ("F.Cu", "In2.Cu", "In4.Cu", "B.Cu"):
        state.set_plane(signal, False)
    assert state.model.planes == ("In1.Cu", "In3.Cu")
    assert state.model.plane_source == "override"
    # In4.Cu has no plane below it now, and says so.
    assert state.trace_layers() == ["F.Cu", "In2.Cu", "B.Cu"]
    assert any("In4.Cu has no reference plane below" in p for p in state.problems())
    in2 = state.model.structure("In2.Cu")
    assert (in2.kind, in2.plane_above, in2.plane_below) == ("stripline", "In1.Cu", "In3.Cu")
    rows = {r.name: r for r in state.layer_rows()}
    assert rows["In1.Cu"].plane and not rows["In1.Cu"].computed
    assert rows["In1.Cu"].text == "In1.Cu: reference plane."
    assert "Combined 3 dielectric plies" in rows["In2.Cu"].text
    # The class form now computes G2's forward inputs on In2.Cu.
    state.set_form("HighSpeed", selected=True, layer="In2.Cu")
    [se] = [i for i in state.plan().items if i.figure == "impedance"]
    assert se.computation.slug == STRIPLINE
    assert se.computation.input_dict["heightToNearPlane"] == 0.19
    assert state.services.client.calls == []
    state.reset_planes()
    assert state.model.plane_source == "adjacency"


def test_a_routing_layer_that_becomes_a_plane_moves_to_a_trace_layer(tmp_path):
    state = state_for(tmp_path, layer_model(four_layer(roles=False)))
    state.set_form("HighSpeed", selected=True, layer="In1.Cu", extra_layers=("B.Cu",))
    state.set_plane("F.Cu", False)  # planes now In1, In2, B.Cu: only F.Cu is a trace layer
    assert state.trace_layers() == ["F.Cu"]
    assert state.forms["HighSpeed"].layer == "F.Cu"
    assert state.forms["HighSpeed"].extra_layers == ()


def test_the_stackups_problems_are_listed(tmp_path):
    state = state_for(tmp_path, layer_model(missing_er()))
    assert any("dielectric 2 has no relative permittivity (εr)" in p for p in state.problems())
    rows = {r.name: r for r in state.layer_rows()}
    assert rows["In1.Cu"].problems and not rows["In1.Cu"].computed


# ── (b) The net-class form ───────────────────────────────────────────────────


def test_the_form_becomes_class_requests(tmp_path):
    state = state_for(tmp_path, layer_model(four_layer()))
    state.set_form("HighSpeed", selected=True, impedance_target="50", current="2,5",
                   layer="F.Cu", extra_layers=("B.Cu", "F.Cu"), via=True)
    state.set_form("USB", selected=True, single_ended=False, diff_target="90")
    [high, usb], errors = state.requests()
    assert errors == []
    assert high.layers == ("F.Cu", "B.Cu") and high.current_layer == "F.Cu"
    assert high.impedance_target == 50 and high.current_a == 2.5 and high.via
    assert usb.diff_target == 90 and not usb.single_ended and usb.layers == ("F.Cu",)


def test_bad_entries_are_named_and_block_the_run(tmp_path):
    state = state_for(tmp_path)
    state.set_form("HighSpeed", selected=True, impedance_target="fifty", current="0")
    state.set_form("Power", selected=True, single_ended=False)
    state.grid_text = "2"
    errors = state.errors()
    assert "HighSpeed: the single-ended target must be a number in Ω, not 'fifty'." in errors
    assert "HighSpeed: the current must be more than 0 A." in errors
    assert any(e.startswith("Power: nothing to compute") for e in errors)
    assert "The grid must be at most 1 mm." in errors
    ok, message = state.run_check()
    assert not ok and message.startswith("Correct these first")
    assert state.services.client.calls == []


def test_nothing_selected_cannot_run(tmp_path):
    ok, message = state_for(tmp_path).run_check()
    assert not ok and "Select a net class" in message


def test_a_coplanar_gap_is_offered_per_class(tmp_path):
    state = state_for(tmp_path)
    state.set_form("HighSpeed", selected=True, cpw_gap="0.2", impedance_target="50")
    items = state.plan().items
    assert [i.computation.slug for i in items] == [COPLANAR, COPLANAR]
    assert D.figure_label(items[0]) == "Z₀ at the class width (grounded CPW, gap 0.2 mm)"
    assert D.figure_label(items[1]) == "Width for 50 Ω"


def test_the_solder_mask_option_and_the_grid_reach_the_requests(tmp_path):
    state = state_for(tmp_path)
    state.solder_mask_cover = True
    state.grid_text = "0.01"
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    impedance, width = state.plan().items
    assert impedance.computation.slug == "controlled-impedance"
    assert D.figure_label(impedance).endswith("(under solder mask)")
    assert width.computation.grid == 0.01
    assert state.saved_options() == {"solder_mask_cover": True, "grid": 0.01}


# ── (c) Budget and key ───────────────────────────────────────────────────────


def reference_state(tmp_path, **kwargs):
    """test_budget's four-layer reference board and requests, entered through the form."""
    stackup = four_layer()
    stackup["layers"][4]["sublayers"][0]["thicknessNm"] = 1030000
    stackup["layers"][6]["sublayers"][0]["thicknessNm"] = 210000
    se50 = NetClassValues("SE50", track_width_nm=300000, clearance_nm=200000,
                          via_diameter_nm=600000, via_drill_nm=300000)
    usb = NetClassValues("USB", track_width_nm=200000, diff_pair_width_nm=180000,
                         diff_pair_gap_nm=150000, clearance_nm=150000,
                         via_diameter_nm=450000, via_drill_nm=200000)
    state = state_for(tmp_path, layer_model(stackup), [se50, usb], **kwargs)
    state.set_form("SE50", selected=True, impedance_target="50", current="2", via=True,
                   layer="F.Cu", extra_layers=("B.Cu",))
    state.set_form("USB", selected=True, single_ended=False, diff_target="90", current="0.5",
                   via=True, layer="F.Cu", extra_layers=("B.Cu",))
    return state


def test_the_budget_line_states_calls_and_allowance_before_any_calculation(tmp_path):
    state = reference_state(tmp_path)
    assert state.budget_line() == (
        "This run will use at most 12 API calls. Check the allowance to see how many remain."
    )
    state.refresh_usage()
    assert state.budget_line() == (
        "This run will use at most 12 API calls; 40 of 50 remain this month, "
        "resetting 2026-10-01T00:00:00Z."
    )
    ok, message = state.run_check()
    assert ok and "at most 12 API calls; 40 of 50 remain" in message
    assert [c[0] for c in state.services.client.calls] == ["usage"]  # free, and nothing else


def test_a_run_larger_than_the_allowance_says_where_it_stops(tmp_path):
    client = FakeClient(calculate=answer, solve=solver(), usage=dict(USAGE, remaining=5))
    state = reference_state(tmp_path, client=client)
    state.refresh_usage()
    ok, message = state.run_check()
    assert ok and "more than the 5 left" in message and "stops when the allowance is spent" in (
        message
    )


def test_without_a_key_the_budget_asks_for_one_and_nothing_runs(tmp_path):
    state = state_for(tmp_path, key=None)
    state.set_form("HighSpeed", selected=True)
    assert state.key_status() == "No API key. Get a free key, paste it below and save it."
    assert "Set an API key to see the allowance" in state.budget_line()
    ok, message = state.run_check()
    assert not ok and KEY_URL in message
    summary = state.run()
    assert summary.requests == 0 and "No API key" in summary.message


def test_get_a_free_key_opens_the_key_link_naming_the_plugin(tmp_path):
    opened = []
    services = FakeServices(tmp_path, key=None)
    state = D.DialogState(layer_model(two_layer()), CLASSES, services, opener=opened.append)
    assert state.open_key_link() == KEY_URL
    assert opened == ["https://rftools.io/dashboard/?newKey=1&client=kicad-plugin"]


def test_a_pasted_key_is_checked_then_saved_and_only_its_public_id_shown(tmp_path):
    state = state_for(tmp_path, key=None)
    saved, message = state.save_key("  " + KEY + "  ")
    assert saved
    assert message == "Saved key rfc_TESTkey0. 40 of 50 remain this month."
    assert KEY not in message and KEY[:13] not in message
    assert state.has_key and state.key_status() == (
        "API key rfc_TESTkey0 (saved in the plugin's settings)."
    )
    path = state.services.settings.path
    assert json.loads(path.read_text(encoding="utf-8"))["apiKey"] == KEY
    if not sys.platform.startswith("win"):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [c[0] for c in state.services.client.calls] == ["usage"]


@pytest.mark.skipif(not sdk_available(), reason="rftools-io not installed")
def test_a_rejected_key_is_not_saved_and_the_message_has_no_key(tmp_path):
    client = FakeClient()
    client.usage_error = sdk_error("auth")
    state = state_for(tmp_path, key=None, client=client)
    saved, message = state.save_key(KEY)
    assert not saved
    assert "did not accept the API key rfc_TESTkey0" in message and KEY not in message
    assert not state.services.settings.path.exists() and not state.has_key


def test_a_malformed_key_is_refused_before_any_request(tmp_path):
    state = state_for(tmp_path, key=None)
    saved, message = state.save_key("not-a-key")
    assert not saved and "start with rfc_" in message
    assert state.services.client.calls == []


# ── (d) The run ──────────────────────────────────────────────────────────────


def test_a_run_shows_current_suggested_achieved_formula_range_engine_and_cache(tmp_path):
    state = state_for(tmp_path)
    state.set_form("HighSpeed", selected=True, impedance_target="50", current="2", via=True)
    summary = state.run()
    assert summary.requests == 4 and summary.refusals == ()
    impedance, width, current, via = summary.rows
    assert impedance.cells() == (
        "HighSpeed", "F.Cu", "Z₀ at the class width", "0.2 mm", "", "52.25 Ω",
        PROVENANCE["formulaRef"], "inside", "api@0123456789ab", "",
    )
    assert impedance.calculator == MICROSTRIP
    assert width.label == "Width for 50 Ω" and width.current == "0.2 mm"
    assert width.suggested == "0.284 mm" and width.achieved == "50.00 Ω"
    assert width.suggested_mm == 0.284 and width.status == D.OK
    assert current.label == "IPC-2152 width for 2 A"
    assert current.suggested == "0.5895 mm" and current.current == "0.2 mm"
    assert via.current == "0.6 / 0.3 mm (pad / drill)"
    assert via.achieved == "Z 48.21 Ω, C 0.412 pF, L 1.02 nH, I 3.1 A"
    assert via.layer is None and via.cells()[1] == "board"

    # The same run again: every figure from the cache, no call, the same values.
    again = state.run()
    assert again.requests == 0
    assert all(r.cached for r in again.rows)
    assert [r.cells()[:-1] for r in again.rows] == [r.cells()[:-1] for r in summary.rows]
    assert again.rows[0].cells()[-1] == "cached"
    assert len(state.services.client.metered) == 4


def test_a_result_outside_the_range_names_the_bound(tmp_path):
    def outside(slug, inputs):
        response = answer(slug, inputs)
        response["provenance"]["validRange"] = {
            "status": "outside",
            "bounds": {"traceWidth": {"min": 0.02, "max": None, "unit": "mm"}},
            "outside": ["traceWidth"],
        }
        response["provenance"]["inputs"] = dict(inputs, traceWidth=0.01)
        return response

    state = state_for(tmp_path, client=FakeClient(calculate=outside))
    state.set_form("HighSpeed", selected=True)
    [row] = state.run().rows
    assert row.range == "outside: traceWidth 0.01 mm is below the minimum 0.02 mm"


@pytest.mark.skipif(not sdk_available(), reason="rftools-io not installed")
def test_a_402_mid_run_keeps_what_was_computed_and_says_how_to_get_more(tmp_path):
    count = {"n": 0}

    def calculate(slug, inputs):
        count["n"] += 1
        if count["n"] == 2:
            raise sdk_error("quota", overage_url="https://rftools.io/dashboard/?overage=1")
        return answer(slug, inputs)

    state = state_for(tmp_path, client=FakeClient(calculate=calculate, solve=solver()))
    state.set_form("HighSpeed", selected=True, impedance_target="50", current="2", via=True)
    state.set_form("Power", selected=True)
    summary = state.run()
    rows = summary.rows
    assert rows[0].ok and rows[1].ok  # Z₀, then the solve
    assert rows[2].status == D.REFUSED  # the current: the 402
    assert all(r.status == D.REFUSED for r in rows[3:])  # nothing more is asked
    [message] = summary.refusals
    assert "used up" in message and "reset at 2026-10-01T00:00:00Z" in message
    assert "https://rftools.io/pricing" in message and "overage=1" in message
    assert summary.requests == 3
    assert rows[0].achieved == "52.25 Ω"  # still shown


@pytest.mark.skipif(not sdk_available(), reason="rftools-io not installed")
def test_offline_shows_cached_results_and_a_retry_runs_again(tmp_path):
    state = state_for(tmp_path)
    state.set_form("HighSpeed", selected=True)
    state.run()
    state.set_form("Power", selected=True)
    state.services.client.errors.append(sdk_error("offline"))
    summary = state.run()
    high, power = summary.rows
    assert high.ok and high.cached
    assert power.status == D.REFUSED and "Could not reach rftools.io" in power.note
    retry = state.run()  # the service is back
    assert all(r.ok for r in retry.rows) and retry.requests == 1


def test_an_unreachable_target_shows_the_nearest_and_proposes_nothing(tmp_path):
    client = FakeClient(calculate=answer, solve=solver(value=0.02, reached=False, achieved=121.5))
    state = state_for(tmp_path, client=client, project=FakeProject(FakeKiCad(sample_classes())))
    state.set_form("HighSpeed", selected=True, impedance_target="150")
    impedance, row = state.run().rows  # a target also shows the class width's impedance
    assert impedance.ok
    assert row.status == D.UNREACHABLE
    assert row.suggested == "unreachable (nearest 0.02 mm)" and row.achieved == "121.50 Ω"
    assert "150 Ω is not reachable on F.Cu" in row.note and "nearest is 121.50 Ω" in row.note
    proposals, notes = state.proposals()
    assert proposals == {} and any("no track width proposed" in n for n in notes)
    preview, reason = state.preview()
    assert preview is None and "not reachable" in reason


def test_a_problem_is_a_row_not_a_call(tmp_path):
    state = state_for(tmp_path, classes=[NetClassValues("Bare")])
    state.set_form("Bare", selected=True, via=True)
    summary = state.run()
    assert [r.status for r in summary.rows] == [D.PROBLEM, D.PROBLEM]
    assert summary.rows[0].note == "Net class Bare has no track width."
    assert "has no via diameter" in summary.rows[1].note
    assert summary.requests == 0


def test_cached_only_spends_nothing_and_says_why(tmp_path):
    state = state_for(tmp_path)
    state.set_form("HighSpeed", selected=True)
    state.run()
    state.set_form("Power", selected=True)
    summary = state.run(cached_only=True, skip_reason="Not computed: over the allowance.")
    high, power = summary.rows
    assert high.cached and high.ok
    assert power.status == D.SKIPPED and power.note == "Not computed: over the allowance."
    assert summary.requests == 0


# ── (e) Proposals, the preview, the write and the restore ────────────────────


def project_state(tmp_path, version=FIXED_KICAD, **kwargs):
    kicad = FakeKiCad(sample_classes(), version=version)
    project = FakeProject(kicad)
    classes = [NetClassValues.from_kipy(nc) for nc in project.get_net_classes()]
    return state_for(tmp_path, classes=classes, project=project, **kwargs), kicad


def test_apply_previews_writes_and_restores_the_selected_classes(tmp_path):
    state, kicad = project_state(tmp_path)
    original = kicad.serialized()
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.set_form("USB", selected=True, single_ended=False, diff_target="90")
    state.set_form("Default", selected=True)  # computed, nothing to propose
    state.run()

    proposals, notes = state.proposals()
    assert proposals == {
        "HighSpeed": {N.TRACK_WIDTH: 284000},
        "USB": {N.DIFF_PAIR_GAP: 284000},
    }
    assert "HighSpeed: track width for 50 Ω on F.Cu (microstrip-impedance)." in notes
    preview, reason = state.preview()
    assert reason is None
    assert [c.text() for c in preview.changes] == [
        "HighSpeed: track width 0.2 mm → 0.284 mm",
        "USB: diff pair gap 0.154 mm → 0.284 mm",
    ]
    # The preview lists all three fields of each class.
    assert [row[0] for row in preview.classes[0].rows()] == [
        "Track width", "Diff pair width", "Diff pair gap",
    ]

    assert state.apply(preview, lambda p: False).cancelled
    assert kicad.sent == [] and kicad.serialized() == original

    result = state.apply(preview, lambda p: True)
    assert result.applied, result.message
    assert N.field_values(kicad.classes["HighSpeed"])[N.TRACK_WIDTH] == 284000
    assert state.netclass("HighSpeed").track_width_nm == 284000  # read back from KiCad
    history = json.loads(state.services.history_path.read_text(encoding="utf-8"))
    assert list(history["projects"]) == ["/work/rf-board"]

    restore, reason = state.restore_preview()
    assert reason is None and restore.action == N.ACTION_RESTORE
    assert state.apply(restore, lambda p: True).applied
    assert kicad.serialized() == original
    assert state.restore_preview()[0] is None


def test_without_a_target_a_current_proposes_the_ipc_width_rounded_up(tmp_path):
    state, _ = project_state(tmp_path)
    state.set_form("Power", selected=True, single_ended=False, current="3")
    state.run()
    proposals, notes = state.proposals()
    # 0.58948... mm rounded up to the 0.001 mm grid, and wider than 0.5 mm.
    assert proposals == {"Power": {N.TRACK_WIDTH: 590000}}
    assert any("IPC-2152 width for 3 A on F.Cu, rounded up" in n for n in notes)


def test_an_impedance_width_narrower_than_the_current_needs_is_flagged(tmp_path):
    state, _ = project_state(tmp_path)
    state.set_form("HighSpeed", selected=True, impedance_target="50", current="3")
    state.run()
    proposals, notes = state.proposals()
    assert proposals == {"HighSpeed": {N.TRACK_WIDTH: 284000}}
    assert any("narrower than the IPC-2152 width for 3 A (0.59 mm)" in n for n in notes)


def test_changing_the_model_after_a_run_blocks_the_write_until_run_again(tmp_path):
    state, kicad = project_state(tmp_path)
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.run()
    state.set_plane("F.Cu", False)
    assert state.results_stale
    preview, reason = state.preview()
    assert preview is None and "run again first" in reason
    state.run()
    assert not state.results_stale and state.preview()[0] is not None
    assert kicad.sent == []


def test_the_write_is_unavailable_with_its_reason(tmp_path):
    state = state_for(tmp_path, project=None)
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.run()
    assert state.write_mode()[0] is None
    preview, reason = state.preview()
    assert preview is None and "No KiCad project" in reason
    assert state.restore_preview() == (None, reason)


def test_on_kicad_10_0_6_the_values_are_listed_for_board_setup_and_not_written(tmp_path):
    state, kicad = project_state(tmp_path, version=BROKEN_KICAD)
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.run()
    mode, reason = state.write_mode()
    assert mode == D.MANUAL
    assert reason.startswith("This is KiCad 10.0.6. KiCad 10.0.6 and earlier corrupt")
    assert "Enter the values in Board Setup › Net Classes instead" in reason
    assert state.write_availability() == (None, reason)

    preview, why = state.preview()
    assert why is None and preview.manual == reason
    # Every field of the class, current → proposed, as Board Setup shows them.
    [high] = preview.classes
    assert high.rows(N.BOARD_SETUP_COLUMNS) == [
        ("Track Width", "0.2 mm", "0.284 mm", True),
        ("DP Width", "0.13 mm", "unchanged", False),
        ("DP Gap", "0.25 mm", "unchanged", False),
    ]
    assert "HighSpeed\tTrack Width\t0.2 mm\t0.284 mm" in preview.manual_text()

    confirmed = []
    result = state.apply(preview, lambda p: confirmed.append(p) or True)
    assert not result.applied and result.message == reason
    assert confirmed == [] and kicad.sent == []
    restore, why = state.restore_preview()
    assert restore is None and "no write" in why


def test_a_kicad_version_that_cannot_be_read_is_entered_by_hand(tmp_path):
    state, kicad = project_state(tmp_path, version=None)
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.run()
    mode, reason = state.write_mode()
    assert mode == D.MANUAL and reason.startswith("KiCad's version could not be read")
    preview, _ = state.preview()
    assert preview.manual == reason
    assert not state.apply(preview, lambda p: True).applied and kicad.sent == []


def test_the_history_is_shown_for_entering_by_hand_on_10_0_6(tmp_path):
    state, kicad = project_state(tmp_path)
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.run()
    assert state.write_mode() == (D.WRITE, None)
    assert state.apply(state.preview()[0], lambda p: True).applied
    sent = len(kicad.sent)

    # The same project and history, with KiCad reporting 10.0.6.
    services = FakeServices(tmp_path, project=state.services.project,
                            kicad=FakeKiCad([], version=BROKEN_KICAD))
    later = D.DialogState(state.model, state.netclasses, services)
    preview, why = later.restore_preview()
    assert why is None and preview.action == N.ACTION_RESTORE and preview.manual
    assert preview.manual_text().startswith("Previous net-class values recorded by the "
                                            "rftools.io plugin")
    assert "HighSpeed\tTrack Width\t0.284 mm\t0.2 mm" in preview.manual_text()
    assert not later.apply(preview, lambda p: True).applied
    assert len(kicad.sent) == sent


def test_the_kicad_version_is_read_once(tmp_path):
    state, kicad = project_state(tmp_path)
    calls = []
    real = kicad.get_version
    kicad.get_version = lambda: calls.append(1) or real()
    for _ in range(3):
        state.write_mode()
        state.write_availability()
    assert state.kicad_version() == FIXED_KICAD and calls == [1]


# ── The real Services object ─────────────────────────────────────────────────


def test_the_state_works_with_mains_services(tmp_path, monkeypatch):
    from rftools_kicad import api as A
    from rftools_kicad import main as M
    from rftools_kicad.cache import ResultCache
    from rftools_kicad.mapping import Options
    from rftools_kicad.settings import Settings

    client = FakeClient(calculate=answer, solve=solver())
    monkeypatch.setattr(A, "make_client", lambda key: client)
    settings = Settings(api_key=KEY, key_source="settings", options={"grid": 0.01},
                        path=tmp_path / "settings.json")
    kicad = FakeKiCad(sample_classes())
    project = FakeProject(kicad)
    services = M.Services(settings, Options(), ResultCache(tmp_path / "r.json"),
                          kicad=kicad, board=FakeBoard(kicad), project=project)
    state = D.DialogState(layer_model(two_layer()), CLASSES, services)
    assert state.grid_text == "0.01"
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.refresh_usage()
    assert "at most 2 API calls; 40 of 50 remain" in state.budget_line()
    summary = state.run()
    assert summary.requests == 2 and all(r.ok for r in summary.rows)
    assert state.kicad_version() == FIXED_KICAD  # through Services.kicad.get_version()
    assert state.write_availability() == (N.METHOD_COMMAND, None)
    assert state.write_mode() == (D.WRITE, None)


def test_the_six_layer_board_computes_its_inner_layer_as_stripline(tmp_path):
    state = state_for(tmp_path, layer_model(six_layer()))
    state.set_form("HighSpeed", selected=True, layer="In2.Cu")
    [row] = state.run().rows
    assert row.calculator == STRIPLINE and row.layer == "In2.Cu"
