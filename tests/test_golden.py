"""The golden cases (design Decision 8): the plugin's stackup mapping reproduces the
web calculators' inputs, and the solve reports the recorded grid width.

golden/kicad-golden.json is generated in the monorepo from the frontend
calculator registry (scripts/generate_kicad_golden.ts) and vendored here.
"""
from __future__ import annotations

import math

import pytest

from rftools_kicad import api as A
from rftools_kicad.mapping import single_ended, trace_current
from rftools_kicad.solve import run_target, target_request
from rftools_kicad.stackup import from_kipy, layer_model
from tests.support import GOLDEN, FakeClient, golden, kipy_stackup

pytestmark = pytest.mark.skipif(not GOLDEN.is_file(), reason="golden/kicad-golden.json absent")

#: Inputs the mapping copies from the case (the width, the current); every
#: other input is derived from the stackup.
COPIED = {"traceWidth", "current", "tempRise", "traceLength", "isExternal"}


def cases():
    return golden()["cases"] if GOLDEN.is_file() else []


def forward_for(case, model):
    inputs = case["forwardInputs"]
    if case["calculator"] == "trace-width-current":
        return trace_current(model, case["layer"], inputs["current"])
    width = case["solve"]["value"] if "solve" in case else inputs["traceWidth"]
    return single_ended(model, case["layer"], width)


@pytest.mark.parametrize("case", cases(), ids=lambda c: c["id"])
def test_the_stackup_maps_to_the_golden_forward_inputs(case):
    model = layer_model(case["stackup"])
    structure = model.structure(case["layer"])
    if "planes" in case:
        # The planes are proposed from the stackup, not handed over.
        assert (structure.plane_above, structure.plane_below) == tuple(case["planes"])
    elif case["calculator"] != "trace-width-current":
        assert structure.reference == "B.Cu"  # G1: the nearest copper inward
    forward = forward_for(case, model)
    assert forward.slug == case["calculator"]
    expected = case["forwardInputs"]
    assert list(forward.input_dict) == list(expected)
    for key, want in expected.items():
        got = forward.input_dict[key]
        if key in COPIED:
            assert got == want, key
        else:
            assert math.isclose(got, want, rel_tol=1e-12, abs_tol=0), (key, got, want)
    assert forward.out_of_range == ()


@pytest.mark.parametrize("case", cases(), ids=lambda c: c["id"])
def test_the_forward_inputs_are_the_same_doubles(case):
    # Stronger than 1e-12: nanometres summed then divided once, and εr weighted
    # on nanometres, give the generator's doubles exactly.
    assert forward_for(case, layer_model(case["stackup"])).input_dict == case["forwardInputs"]


@pytest.mark.parametrize("case", cases(), ids=lambda c: c["id"])
def test_the_same_holds_for_the_stackup_as_kicad_sends_it(case):
    model = layer_model(from_kipy(kipy_stackup(case["stackup"])))
    if "planes" in case:
        # KiCad exposes no layer roles: the user marks the planes.
        model = model.with_planes(case["planes"])
    assert forward_for(case, model).input_dict == case["forwardInputs"]


def test_g1_solve_request_and_recorded_response_give_the_golden_grid_width():
    [case] = [c for c in cases() if c["id"] == "G1"]
    solve = case["solve"]
    model = layer_model(case["stackup"])
    request = target_request(
        single_ended(model, case["layer"], 0.2),  # a class width, the start value
        solve["target"]["value"],
        grid=solve["grid"],
    )
    body = request.payload()
    assert body["solveFor"] == solve["solveFor"]
    assert body["target"] == {"output": solve["target"]["output"],
                              "value": float(solve["target"]["value"])}
    assert body["grid"] == solve["grid"] == golden()["grid"]
    assert "range" not in body  # microstrip-impedance states both width bounds
    expected_fixed = {k: v for k, v in case["forwardInputs"].items() if k != solve["solveFor"]}
    assert {k: v for k, v in body["inputs"].items() if k != solve["solveFor"]} == expected_fixed

    recorded = {
        "slug": case["calculator"], "solveFor": solve["solveFor"], "target": solve["target"],
        "grid": solve["grid"], "value": solve["value"], "unrounded": solve["unrounded"],
        "reached": True, "evaluations": 30, "warnings": [],
        "result": {"slug": case["calculator"], "values": case["outputs"], "warnings": [],
                   "errors": [], "provenance": {"version": "api@0123456789ab"}},
    }
    client = FakeClient(solve=lambda *args: recorded)
    result = run_target(A.Api(client), request)
    assert result.value == solve["value"]
    assert result.unrounded == solve["unrounded"]
    assert math.isclose(result.achieved, case["outputs"]["impedance"], rel_tol=1e-6)
    assert len(client.metered) == 1
    # The forward check at the returned width is exactly the golden forward call.
    assert request.with_input("traceWidth", result.value).input_dict == case["forwardInputs"]
