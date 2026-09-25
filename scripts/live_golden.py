#!/usr/bin/env python3
"""The golden cases G1–G3 against the production API, through the plugin's client (task 5.3).

Run by .github/workflows/live-golden.yml weekly, before each release is
published, and on demand, with the ``RFTOOLS_API_KEY`` secret:

    RFTOOLS_API_KEY=rfc_... python scripts/live_golden.py

Each case starts from its stackup in golden/kicad-golden.json, goes through
the plugin's own stackup model and calculator mapping, and is sent through the
plugin's API layer over its own client (``rftools_kicad.client``, urllib and
certifi) — the path a user's run takes, with no result cache:

    G1  one solve: microstrip-impedance traceWidth for 50 Ω on the 0.001 mm grid
        must return the golden grid width, with the forward outputs at it
    G2  one forward call: asymmetric-stripline at the stated width
    G3  one forward call: trace-width-current for 2 A

That is **at most three metered calls per run**; reading the allowance first
(``GET /v1/usage``) is never metered. Every output is compared with the
golden value to a relative 1e-6, the parity check's tolerance. The exit code
is 1 on any difference, refusal or shortfall.

Without the key the check is skipped with a notice and exits 0, so a
repository without the secret stays green.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins"))

from rftools_kicad.api import Api, make_client, read_figure  # noqa: E402
from rftools_kicad.mapping import input_spec, single_ended, trace_current  # noqa: E402
from rftools_kicad.settings import public_id  # noqa: E402
from rftools_kicad.solve import run_target, target_request  # noqa: E402
from rftools_kicad.stackup import layer_model  # noqa: E402

GOLDEN = ROOT / "golden" / "kicad-golden.json"
ENV_VAR = "RFTOOLS_API_KEY"
#: G1 one solve, G2 one forward, G3 one forward.
MAX_CALLS = 3
REL_TOL = 1e-6


@dataclass
class CaseResult:
    id: str
    calculator: str
    call: str
    ok: bool = True
    lines: list = field(default_factory=list)  # (output, api value, golden value, verdict)
    problems: list = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.problems.append(message)


def load_golden(path: Path = GOLDEN) -> dict:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))


def request_for(case: dict):
    """The computation the plugin would send for *case*, built from its stackup."""
    model = layer_model(case["stackup"])
    if "planes" in case:
        model = model.with_planes(case["planes"])
    inputs = case["forwardInputs"]
    if case["calculator"] == "trace-width-current":
        return trace_current(model, case["layer"], inputs["current"])
    if "solve" in case:
        solve = case["solve"]
        start = input_spec(case["calculator"], solve["solveFor"])["default"]
        forward = single_ended(model, case["layer"], start)
        return target_request(forward, solve["target"]["value"], grid=solve["grid"])
    return single_ended(model, case["layer"], inputs["traceWidth"])


def run(client, golden: dict, *, key_id: str = "(key)") -> tuple:
    """``(case results, metered requests made)``."""
    api = Api(client, cache=None, key_id=key_id)
    results = []
    usage, refusal = api.usage()
    cases = golden["cases"]
    needed = min(MAX_CALLS, len(cases))
    if refusal is not None:
        result = CaseResult("usage", "", "usage")
        result.fail(f"the allowance could not be read: {refusal.message}")
        return [result], 0
    remaining = (usage or {}).get("remaining")
    if remaining is not None and remaining < needed:
        result = CaseResult("usage", "", "usage")
        result.fail(
            f"the key's account has {remaining} calls left this month and the check needs "
            f"{needed}; nothing was run"
        )
        return [result], 0

    for case in cases:
        computation = request_for(case)
        result = CaseResult(case["id"], case["calculator"], computation.kind)
        if computation.slug != case["calculator"]:
            result.fail(f"the plugin maps it to {computation.slug}")
            results.append(result)
            continue
        if "solve" in case:
            solved = run_target(api, computation)
            if solved.refusal is not None:
                result.fail(f"refused: {solved.refusal.message}")
                results.append(result)
                continue
            want = case["solve"]["value"]
            verdict = "ok" if solved.value == want and solved.reached else "DIFFERS"
            result.lines.append((f"{case['solve']['solveFor']} (grid)", solved.value, want,
                                 verdict))
            if verdict != "ok":
                result.fail(f"the solve returned {solved.value} (reached {solved.reached}), "
                            f"not the golden grid width {want}")
            sent = computation.with_input(case["solve"]["solveFor"], want).input_dict
            values = solved.values or {}
        else:
            sent = computation.input_dict
            figure = read_figure(api.run(computation))
            if figure.refusal is not None:
                result.fail(f"refused: {figure.refusal.message}")
                results.append(result)
                continue
            values = figure.values or {}
        if sent != case["forwardInputs"]:
            result.fail(f"the forward inputs {sent} are not the golden {case['forwardInputs']}")
        compare(result, values, case["outputs"])
        results.append(result)
    if api.requests > MAX_CALLS:
        extra = CaseResult("calls", "", "count")
        extra.fail(f"{api.requests} metered calls were made; the check may spend {MAX_CALLS}")
        results.append(extra)
    return results, api.requests


def compare(result: CaseResult, values: dict, golden: dict) -> None:
    for key, want in golden.items():
        got = values.get(key)
        if got is None:
            result.lines.append((key, None, want, "MISSING"))
            result.fail(f"{key} is missing from the API's result")
            continue
        if isinstance(want, bool) or isinstance(got, bool):
            same = got == want
        else:
            same = math.isclose(got, want, rel_tol=REL_TOL, abs_tol=0.0)
        result.lines.append((key, got, want, "ok" if same else "DIFFERS"))
        if not same:
            relative = abs(got - want) / abs(want) if want else float("inf")
            result.fail(f"{key} is {got!r}, golden {want!r} (relative {relative:.3g})")


def report(results: list, requests: int) -> str:
    lines = ["| Case | Calculator | Output | API | Golden | |", "|---|---|---|---|---|---|"]
    for r in results:
        for key, got, want, verdict in r.lines:
            lines.append(f"| {r.id} | {r.calculator} | {key} | {got!r} | {want!r} | {verdict} |")
        for problem in r.problems:
            lines.append(f"| {r.id} | {r.calculator} | | | | **{problem}** |")
    passed = all(r.ok for r in results)
    lines.append("")
    lines.append(f"{'Passed' if passed else 'FAILED'}: {requests} metered call"
                 f"{'' if requests == 1 else 's'} (at most {MAX_CALLS}), relative tolerance "
                 f"{REL_TOL:g}.")
    return "\n".join(lines)


def main(argv: list | None = None, environ: dict | None = None, client_factory=None) -> int:
    environ = os.environ if environ is None else environ
    key = (environ.get(ENV_VAR) or "").strip()
    if not key:
        print(f"::notice title=Live golden check skipped::The {ENV_VAR} secret is not set, so "
              "G1–G3 were not run against the production API. Add the secret (a free key made "
              "through the key link with client=kicad-plugin) to run them.")
        return 0
    results, requests = run((client_factory or make_client)(key), load_golden(),
                            key_id=public_id(key))
    text = report(results, requests)
    print(text)
    summary = environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Live golden cases\n\n" + text + "\n")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
