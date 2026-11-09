#!/usr/bin/env python3.12
"""Write the plugin's window, as webui renders it, for the README's screenshots.

    python3.12 scripts/export_demo_pages.py OUT_DIR

The board is the four-layer 1.6 mm demo of the design's mockups: 0.2 mm
prepreg at εr 4.4 to typed power planes, a 1.04 mm core at εr 4.6, 35 µm
copper. Four net classes are computed through the plugin's own code against a
local client that runs the site's calculators from the monorepo
(backend/src/core/calculators) with the backend's own provenance envelope, so
every value, formula reference and engine version is what rftools.io's API
returns for those inputs. Needs the monorepo beside rftools-kicad and Python
3.12, as the backend does. Writes layers, classes and results pages in both
themes; scripts/kicad_plugin_screenshots.mjs in the monorepo photographs them.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT.parent / "backend" / "src"
sys.path[:0] = [str(ROOT / "plugins"), str(ROOT), str(BACKEND)]

from core.calculators.registry import ALL_CALCULATORS  # noqa: E402
from services import provenance  # noqa: E402

from rftools_kicad import webui  # noqa: E402
from rftools_kicad.dialog_logic import DialogState  # noqa: E402
from rftools_kicad.mapping import NetClassValues, calculator  # noqa: E402
from rftools_kicad.stackup import layer_model  # noqa: E402
from tests.support import (  # noqa: E402,E501
    FakeKiCad,
    FakeProject,
    FakeServices,
    four_layer,
    sample_classes,
)

USAGE = {"tier": "pro", "period": "2026-11", "resetAt": "2026-12-01T00:00:00Z",
         "allowance": 100, "used": 12, "remaining": 88}


class LocalClient:
    """client.Client's three calls, answered by the site's calculators."""

    def __init__(self) -> None:
        self.calls: list = []

    def usage(self):
        return dict(USAGE)

    def calculate(self, slug, inputs):
        self.calls.append(("calculate", slug))
        started = time.time()
        values = ALL_CALCULATORS[slug](dict(inputs))["values"]
        envelope = provenance.for_calculator(slug, inputs, started=started, values=values)
        return {"slug": slug, "values": values, "warnings": [], "errors": [],
                "provenance": envelope}

    def solve(self, slug, inputs, solve_for, target_output, target_value, grid=None,
              range=None):  # noqa: A002
        self.calls.append(("solve", slug))
        spec = next(i for i in calculator(slug)["inputs"] if i["key"] == solve_for)
        lo, hi = range or (spec["min"], spec["max"])  # "range" is the client's own name
        steps = 200

        def f(x):
            return ALL_CALCULATORS[slug](dict(inputs, **{solve_for: x}))["values"][
                target_output] - target_value

        a, b = lo, hi
        while steps:
            steps -= 1
            m = (a + b) / 2
            if (f(a) > 0) == (f(m) > 0):
                a = m
            else:
                b = m
        unrounded = (a + b) / 2
        value = round(round(unrounded / grid) * grid, 9) if grid else unrounded
        result = self.calculate(slug, dict(inputs, **{solve_for: value}))
        self.calls.pop()  # the forward result is part of the one solve call
        return {"slug": slug, "solveFor": solve_for,
                "target": {"output": target_output, "value": target_value}, "grid": grid,
                "value": value, "unrounded": unrounded, "reached": True, "evaluations": 200,
                "warnings": [], "result": result}


def demo_state(tmp: Path) -> DialogState:
    classes = [
        NetClassValues("Default", track_width_nm=250000, clearance_nm=200000),
        NetClassValues("HighSpeed", track_width_nm=200000, clearance_nm=200000),
        NetClassValues("USB_D", track_width_nm=250000, diff_pair_width_nm=250000,
                       diff_pair_gap_nm=200000, clearance_nm=200000),
        NetClassValues("Power", track_width_nm=500000, clearance_nm=250000),
    ]
    # A KiCad 10.0.8 project behind the board, so the write is offered as it is to a user.
    project = FakeProject(FakeKiCad(sample_classes(), version=(10, 0, 8)))
    services = FakeServices(tmp, client=LocalClient(), project=project)
    return DialogState(layer_model(four_layer()), classes, services)


def main(argv=None) -> int:
    out = Path((argv or sys.argv[1:])[0])
    out.mkdir(parents=True, exist_ok=True)
    state = demo_state(Path(tempfile.mkdtemp()))
    state.refresh_usage()
    for theme in ("dark", "light"):
        for step in ("layers", "classes"):
            (out / f"{step}-{theme}.html").write_text(
                webui.page(webui.Session(state, step=step), theme=theme), encoding="utf-8")
    state.set_form("Default", selected=True)
    state.set_form("HighSpeed", selected=True, impedance_target="50")
    state.set_form("USB_D", selected=True, single_ended=False, diff_target="90")
    state.set_form("Power", selected=True, single_ended=False, current="2")
    state.run()
    for theme in ("dark", "light"):
        (out / f"results-{theme}.html").write_text(
            webui.page(webui.Session(state, step="results"), theme=theme), encoding="utf-8")
    rows = [(r.netclass, r.label, r.suggested or r.achieved, r.engine) for r in state.results]
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
