"""Target searches: one solve call per target, read from recorded responses."""
from __future__ import annotations

from rftools_kicad import api as A
from rftools_kicad.cache import ResultCache
from rftools_kicad.mapping import differential, single_ended
from rftools_kicad.solve import (
    DEFAULT_GRID_MM,
    SEARCH_MAX_MM,
    from_response,
    run_target,
    search_range,
    target_request,
)
from rftools_kicad.stackup import layer_model
from tests.support import (
    FakeClient,
    api_error,
    golden_case,
    six_layer,
    solve_response,
    two_layer,
)


def g1_recorded_response(case: dict) -> dict:
    """The solve response the API gives for G1, built from the golden file."""
    solve = case["solve"]
    return {
        "slug": case["calculator"],
        "solveFor": solve["solveFor"],
        "target": solve["target"],
        "grid": solve["grid"],
        "value": solve["value"],
        "unrounded": solve["unrounded"],
        "reached": True,
        "evaluations": 31,
        "warnings": [],
        "result": {
            "slug": case["calculator"],
            "values": case["outputs"],
            "warnings": [],
            "errors": [],
            "provenance": {"version": "api@0123456789ab", "formulaRef": "Hammerstad & Jensen"},
        },
    }


def test_g1_shows_the_golden_grid_width_after_exactly_one_call(tmp_path):
    case = golden_case("G1")
    model = layer_model(case["stackup"])
    client = FakeClient(solve=lambda *args: g1_recorded_response(case))
    api = A.Api(client, ResultCache(tmp_path / "results.json"))
    forward = single_ended(model, case["layer"], 0.3)  # the class's current width
    request = target_request(forward, case["solve"]["target"]["value"])

    result = run_target(api, request)

    assert result.value == case["solve"]["value"] == 2.784
    assert result.unrounded == case["solve"]["unrounded"]
    assert result.reached and result.nearest is None
    assert result.achieved == case["outputs"]["impedance"]
    assert result.engine_version == "api@0123456789ab"
    assert result.formula_ref == "Hammerstad & Jensen"
    assert len(client.metered) == 1
    [(method, body)] = client.metered
    assert method == "solve"
    assert body == {
        "slug": "microstrip-impedance",
        "inputs": {"traceWidth": 0.3, "substrateHeight": 1.51, "dielectricConstant": 4.5,
                   "copperThickness": 35.0},
        "solveFor": "traceWidth",
        "target": {"output": "impedance", "value": 50.0},
        "grid": case["solve"]["grid"],
    }


def test_a_cached_target_makes_no_call(tmp_path):
    case = golden_case("G1")
    model = layer_model(case["stackup"])
    client = FakeClient(solve=lambda *args: g1_recorded_response(case))
    path = tmp_path / "results.json"
    request = target_request(single_ended(model, "F.Cu", 0.3), 50)
    run_target(A.Api(client, ResultCache(path)), request)
    again = run_target(A.Api(client, ResultCache(path)), request)
    assert again.cached and again.value == 2.784
    assert len(client.metered) == 1


def test_an_unreachable_target_reports_the_nearest_impedance():
    # Spec scenario "An unreachable target".
    model = layer_model(two_layer())
    client = FakeClient(solve=lambda slug, inputs, key, output, target, grid, rng: solve_response(
        slug, inputs, key, output, target, value=50.0, unrounded=50.0, reached=False,
        achieved=11.87,
    ))
    result = run_target(A.Api(client), target_request(single_ended(model, "F.Cu", 0.3), 5))
    assert not result.reached
    assert result.nearest == 50.0 and result.achieved == 11.87
    assert "The target was not reached." in result.warnings


def test_the_search_range_is_supplied_only_where_the_calculator_states_no_maximum():
    assert search_range("microstrip-impedance", "traceWidth") is None
    assert search_range("differential-pair", "traceSpacing") is None
    assert search_range("asymmetric-stripline", "traceWidth") == (0.02, SEARCH_MAX_MM)
    assert search_range("edge-coupled-internal-asymmetric", "traceSpacing") == (0.02, SEARCH_MAX_MM)
    assert search_range("controlled-impedance", "traceWidth") == (0.05, SEARCH_MAX_MM)


def test_a_pair_solves_for_its_gap():
    model = layer_model(six_layer())
    outer = target_request(differential(model, "F.Cu", 0.15, 0.2), 90)
    assert (outer.solve_for, outer.output, outer.search_range) == ("traceSpacing", "zdiff", None)
    inner = target_request(differential(model, "In2.Cu", 0.1, 0.15), 100, grid=0.005)
    assert inner.payload()["solveFor"] == "traceSpacing"
    assert inner.payload()["target"] == {"output": "diffImpedance", "value": 100.0}
    assert inner.payload()["range"] == [0.02, SEARCH_MAX_MM]
    assert inner.payload()["grid"] == 0.005


def test_the_default_grid_and_no_grid():
    model = layer_model(two_layer())
    assert target_request(single_ended(model, "F.Cu", 0.3), 50).grid == DEFAULT_GRID_MM == 0.001
    assert "grid" not in target_request(single_ended(model, "F.Cu", 0.3), 50, grid=None).payload()


def test_a_refusal_is_carried_to_the_result():
    model = layer_model(two_layer())
    client = FakeClient()
    client.errors.append(api_error("quota"))
    result = run_target(A.Api(client), target_request(single_ended(model, "F.Cu", 0.3), 50))
    assert not result.ok and result.refusal.kind == A.QUOTA


def test_range_notes_name_the_bound_crossed():
    model = layer_model(two_layer())
    request = target_request(single_ended(model, "F.Cu", 0.3), 50)
    response = solve_response("microstrip-impedance", request.input_dict, "traceWidth",
                              "impedance", 50, value=0.005)
    response["result"]["provenance"]["validRange"] = {
        "status": "outside",
        "bounds": {"traceWidth": {"min": 0.01, "max": 50, "unit": "mm"}},
        "outside": ["traceWidth"],
    }
    result = from_response(request, response)
    assert result.outside == ("traceWidth 0.005 mm is below the minimum 0.01 mm",)
