"""The live golden check's logic (task 5.3), against a fake client answering as production would."""
from __future__ import annotations

import importlib.util
import sys

import pytest

from tests.support import (
    KEY,
    USAGE,
    FakeClient,
    calc_response,
    golden,
    golden_case,
    sdk_available,
    sdk_error,
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


def production(g1_value=None, scale=None):
    """A client answering G1–G3 with the golden values (optionally one of them off)."""
    by_slug = {c["calculator"]: c for c in golden()["cases"]}

    def calculate(slug, inputs):
        values = dict(by_slug[slug]["outputs"])
        if scale and slug == scale[0]:
            values[scale[1]] *= scale[2]
        return calc_response(slug, inputs, values)

    def solve(slug, inputs, solve_for, output, target, grid, range_):
        case = by_slug[slug]
        value = case["solve"]["value"] if g1_value is None else g1_value
        return {
            "slug": slug, "solveFor": solve_for, "target": {"output": output, "value": target},
            "grid": grid, "value": value, "unrounded": case["solve"]["unrounded"],
            "reached": True, "evaluations": 30, "warnings": [],
            "result": calc_response(slug, dict(inputs, **{solve_for: value}),
                                    dict(case["outputs"])),
        }

    return FakeClient(calculate=calculate, solve=solve)


def test_the_golden_cases_pass_with_exactly_three_metered_calls():
    client = production()
    results, requests = L.run(client, golden())
    assert [r.id for r in results] == ["G1", "G2", "G3"]
    assert all(r.ok for r in results), [r.problems for r in results]
    assert requests == 3 == L.MAX_CALLS
    kinds = [c[0] for c in client.calls]
    assert kinds == ["usage", "solve", "calculate", "calculate"]
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


def test_g1_must_return_the_golden_grid_width():
    results, _ = L.run(production(g1_value=2.785), golden())
    g1 = results[0]
    assert not g1.ok and "not the golden grid width 2.784" in g1.problems[0]


def test_a_value_beyond_one_part_in_a_million_fails_and_is_named():
    results, _ = L.run(production(scale=("asymmetric-stripline", "impedance", 1 + 2e-6)),
                       golden())
    g2 = results[1]
    assert not g2.ok and g2.problems[0].startswith("impedance is ")
    assert results[0].ok and results[2].ok


def test_a_value_within_tolerance_passes():
    results, _ = L.run(production(scale=("trace-width-current", "width2152mm", 1 + 5e-7)),
                       golden())
    assert all(r.ok for r in results)


def test_too_little_allowance_runs_nothing():
    client = production()
    client._usage = dict(USAGE, remaining=2)
    results, requests = L.run(client, golden())
    assert requests == 0 and client.metered == []
    assert not results[0].ok and "has 2 calls left" in results[0].problems[0]


@pytest.mark.skipif(not sdk_available(), reason="rftools-io not installed")
def test_a_refusal_fails_the_case_with_its_message():
    client = production()
    client.errors.append(sdk_error("auth"))
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
    assert "## Live golden cases" in text and "Passed: 3 metered calls (at most 3)" in text
    assert KEY not in capsys.readouterr().out
    bad = lambda key: production(g1_value=2.7)  # noqa: E731
    assert L.main([], environ=environ, client_factory=bad) == 1
    assert "FAILED" in summary.read_text()


@pytest.mark.skipif(
    not sdk_available() or not hasattr(__import__("rftools").Client, "solve"),
    reason="the installed rftools-io has no Client.solve (0.4)",
)
def test_through_the_real_sdk_over_http():
    import json

    import httpx
    import respx
    import rftools

    base = "https://rftools.test/api/py/v1"
    by_slug = {c["calculator"]: c for c in golden()["cases"]}

    def calculate(request):
        body = json.loads(request.content)
        case = by_slug[body["slug"]]
        return httpx.Response(200, json={
            "slug": body["slug"], "values": case["outputs"], "warnings": [], "errors": [],
            "provenance": {"version": "api@0123456789ab", "inputs": body["inputs"]},
        })

    def solve(request):
        body = json.loads(request.content)
        case = by_slug[body["slug"]]
        return httpx.Response(200, json={
            "slug": body["slug"], "solveFor": body["solveFor"], "target": body["target"],
            "grid": body.get("grid"), "value": case["solve"]["value"],
            "unrounded": case["solve"]["unrounded"], "reached": True, "evaluations": 31,
            "warnings": [],
            "result": {"slug": body["slug"], "values": case["outputs"], "warnings": [],
                       "errors": [], "provenance": {"version": "api@0123456789ab"}},
        })

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{base}/usage").mock(return_value=httpx.Response(200, json=USAGE))
        solved = router.post(f"{base}/calculate/solve").mock(side_effect=solve)
        forward = router.post(f"{base}/calculate").mock(side_effect=calculate)
        client = rftools.Client(api_key=KEY, base_url=base, max_retries=0)
        results, requests = L.run(client, golden())
    assert all(r.ok for r in results), [r.problems for r in results]
    assert requests == 3 and solved.call_count == 1 and forward.call_count == 2
