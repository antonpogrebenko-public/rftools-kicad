"""The live golden check's logic (task 5.3), against a fake client answering as production would."""
from __future__ import annotations

import importlib.util
import sys

import pytest

from tests.support import (
    KEY,
    USAGE,
    FakeClient,
    api_error,
    calc_response,
    golden,
    golden_case,
)
from tests.support import ROOT as REPO


def load():
    spec = importlib.util.spec_from_file_location(
        "live_golden", REPO / "scripts" / "live_golden.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


L = load()


def case_for(slug, inputs):
    """The golden case a request is for: by its forward inputs, else (a solve) by its slug.

    G3 and G3b are both trace-width-current, told apart by the board they send.
    """
    cases = [c for c in golden()["cases"] if c["calculator"] == slug]
    exact = [c for c in cases if c["forwardInputs"] == inputs]
    return (exact or cases)[0]


def production(g1_value=None, scale=None):
    """A client answering G1–G3b with the golden values (optionally one of them off).

    ``scale`` is ``(case id, output, factor)``.
    """

    def calculate(slug, inputs):
        case = case_for(slug, inputs)
        values = dict(case["outputs"])
        if scale and case["id"] == scale[0]:
            values[scale[1]] *= scale[2]
        return calc_response(slug, inputs, values)

    def solve(slug, inputs, solve_for, output, target, grid, range_):
        case = case_for(slug, inputs)
        value = case["solve"]["value"] if g1_value is None else g1_value
        return {
            "slug": slug, "solveFor": solve_for, "target": {"output": output, "value": target},
            "grid": grid, "value": value, "unrounded": case["solve"]["unrounded"],
            "reached": True, "evaluations": 30, "warnings": [],
            "result": calc_response(slug, dict(inputs, **{solve_for: value}),
                                    dict(case["outputs"])),
        }

    return FakeClient(calculate=calculate, solve=solve)


def test_the_golden_cases_pass_with_exactly_four_metered_calls():
    client = production()
    results, requests = L.run(client, golden())
    assert [r.id for r in results] == ["G1", "G2", "G3", "G3b"]
    assert all(r.ok for r in results), [r.problems for r in results]
    assert requests == 4 == L.MAX_CALLS
    kinds = [c[0] for c in client.calls]
    assert kinds == ["usage", "solve", "calculate", "calculate", "calculate"]
    # What was sent is what the golden file says the plugin sends.
    g1 = golden_case("G1")
    solve_body = client.calls[1][1]
    assert solve_body["solveFor"] == "traceWidth" and solve_body["grid"] == 0.001
    assert solve_body["target"] == {"output": "impedance", "value": 50.0}
    fixed = {k: v for k, v in g1["forwardInputs"].items() if k != "traceWidth"}
    assert {k: v for k, v in solve_body["inputs"].items() if k != "traceWidth"} == fixed
    assert client.calls[2][1] == {"slug": "asymmetric-stripline",
                                  "inputs": golden_case("G2")["forwardInputs"]}
    assert client.calls[3][1] == {"slug": "trace-width-current",
                                  "inputs": golden_case("G3")["forwardInputs"]}
    # G3b sends the board: its thickness, and In1.Cu, a ½ oz plane 0.2 mm under F.Cu.
    g3b = client.calls[4][1]
    assert g3b == {"slug": "trace-width-current", "inputs": golden_case("G3b")["forwardInputs"]}
    assert {k: g3b["inputs"][k] for k in ("boardThickness", "planeDistance",
                                           "planeCopperWeight", "isExternal")} == {
        "boardThickness": 1.6, "planeDistance": 0.2, "planeCopperWeight": 0.5, "isExternal": 1,
    }
    g3b_result = results[3]
    assert ("widthBoardMm", golden_case("G3b")["outputs"]["widthBoardMm"],
            golden_case("G3b")["outputs"]["widthBoardMm"], "ok") in g3b_result.lines


def test_g1_must_return_the_golden_grid_width():
    results, _ = L.run(production(g1_value=2.785), golden())
    g1 = results[0]
    assert not g1.ok and "not the golden grid width 2.781" in g1.problems[0]


def test_a_value_beyond_one_part_in_a_million_fails_and_is_named():
    results, _ = L.run(production(scale=("G2", "impedance", 1 + 2e-6)), golden())
    g2 = results[1]
    assert not g2.ok and g2.problems[0].startswith("impedance is ")
    assert results[0].ok and results[2].ok and results[3].ok


def test_a_board_specific_width_that_differs_fails_g3b_alone():
    results, _ = L.run(production(scale=("G3b", "widthBoardMm", 1 + 2e-6)), golden())
    assert [r.ok for r in results] == [True, True, True, False]
    assert results[3].problems[0].startswith("widthBoardMm is ")


def test_a_value_within_tolerance_passes():
    results, _ = L.run(production(scale=("G3", "width2152mm", 1 + 5e-7)), golden())
    assert all(r.ok for r in results)


def test_too_little_allowance_runs_nothing():
    client = production()
    client._usage = dict(USAGE, remaining=3)  # one short of G1–G3b's four
    results, requests = L.run(client, golden())
    assert requests == 0 and client.metered == []
    assert not results[0].ok and "has 3 calls left" in results[0].problems[0]


def test_a_refusal_fails_the_case_with_its_message():
    client = production()
    client.errors.append(api_error("auth"))
    results, _ = L.run(client, golden(), key_id="rfc_TESTkey0")
    assert not results[0].ok and "did not accept the API key rfc_TESTkey0" in results[0].problems[0]


def test_without_the_secret_the_check_is_skipped_not_failed(capsys):
    def factory(key):
        pytest.fail("a client was made without a key")

    assert L.main([], environ={}, client_factory=factory) == 0
    out = capsys.readouterr().out
    assert out.startswith("::notice title=Live golden check skipped::")
    assert "RFTOOLS_API_KEY" in out


def test_main_reports_to_the_step_summary_and_the_exit_code(tmp_path, capsys):
    summary = tmp_path / "summary.md"
    environ = {"RFTOOLS_API_KEY": KEY, "GITHUB_STEP_SUMMARY": str(summary)}
    assert L.main([], environ=environ, client_factory=lambda key: production()) == 0
    text = summary.read_text()
    assert "## Live golden cases" in text and "Passed: 4 metered calls (at most 4)" in text
    assert KEY not in capsys.readouterr().out
    bad = lambda key: production(g1_value=2.7)  # noqa: E731
    assert L.main([], environ=environ, client_factory=bad) == 1
    assert "FAILED" in summary.read_text()


def test_through_the_plugins_client_over_http(service):
    def answer(request):
        if request.path.endswith("/usage"):
            return 200, dict(USAGE), {}
        body = request.json
        case = case_for(body["slug"], body["inputs"])
        forward = {"slug": body["slug"], "values": case["outputs"], "warnings": [],
                   "errors": [], "provenance": {"version": "api@0123456789ab",
                                                "inputs": body["inputs"]}}
        if not request.path.endswith("/solve"):
            return 200, forward, {}
        return 200, {
            "slug": body["slug"], "solveFor": body["solveFor"], "target": body["target"],
            "grid": body.get("grid"), "value": case["solve"]["value"],
            "unrounded": case["solve"]["unrounded"], "reached": True, "evaluations": 31,
            "warnings": [], "result": forward,
        }, {}

    service.handler = answer
    results, requests = L.run(service.client(), golden())
    assert all(r.ok for r in results), [r.problems for r in results]
    paths = [r.path for r in service.requests]
    assert paths == ["/api/py/v1/usage", "/api/py/v1/calculate/solve",
                     "/api/py/v1/calculate", "/api/py/v1/calculate", "/api/py/v1/calculate"]
    assert requests == 4


def test_main_makes_the_plugins_client(monkeypatch, capsys):
    from rftools_kicad import api as A
    from rftools_kicad.client import Client

    made = []

    def make(key):
        made.append(A.make_client(key))
        return production()

    monkeypatch.setattr(L, "make_client", make)
    assert L.main([], environ={"RFTOOLS_API_KEY": KEY}) == 0
    [client] = made
    assert isinstance(client, Client) and client.base_url == "https://rftools.io/api/py/v1"
    assert KEY not in capsys.readouterr().out
