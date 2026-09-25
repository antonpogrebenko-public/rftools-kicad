"""plugins/rftools_kicad/calculators.json against the monorepo it is extracted from."""
from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from rftools_kicad.mapping import CALCULATORS, PRIMARY_OUTPUT
from tests.support import ROOT

SCRIPT = ROOT / "scripts" / "extract_calculators.py"
COMMITTED = ROOT / "plugins" / "rftools_kicad" / "calculators.json"


def _extractor():
    spec = importlib.util.spec_from_file_location("extract_calculators", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_file_matches_the_monorepo_catalogue():
    extractor = _extractor()
    if not extractor.monorepo_present():
        pytest.skip("the monorepo's calculator files are not present (standalone checkout)")
    if sys.version_info < (3, 12):
        pytest.skip("the backend's calculators run on Python 3.12")
    try:
        fresh = extractor.render(extractor.extract())
    except ImportError as exc:  # the backend's calculators need something not installed
        pytest.skip(f"the backend calculators cannot be imported here: {exc}")
    assert COMMITTED.read_text(encoding="utf-8") == fresh, (
        "calculators.json is stale; run scripts/extract_calculators.py"
    )


def test_it_holds_every_calculator_the_plugin_uses():
    extractor = _extractor()
    assert list(CALCULATORS) == list(extractor.SLUGS) == list(PRIMARY_OUTPUT)
    data = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert "GENERATED" in data["$comment"]
    for slug, calc in CALCULATORS.items():
        assert calc["inputs"] and calc["outputs"], slug
        for spec in calc["inputs"]:
            assert set(spec) == {"key", "unit", "default", "min", "max"}, (slug, spec)


def test_the_key_spellings_the_mapping_relies_on():
    def keys(slug):
        return [spec["key"] for spec in CALCULATORS[slug]["inputs"]]

    for slug in ("microstrip-impedance", "differential-pair", "via-calculator"):
        assert "dielectricConstant" in keys(slug), slug
    for slug in ("controlled-impedance", "asymmetric-stripline", "coplanar-waveguide",
                 "edge-coupled-internal-symmetric", "edge-coupled-internal-asymmetric"):
        assert "dielectricConst" in keys(slug), slug
    assert keys("edge-coupled-internal-asymmetric")[2:4] == ["heightBelow", "heightAbove"]
    assert "planeSpacing" in keys("edge-coupled-internal-symmetric")
    assert CALCULATORS["trace-width-current"]["inputs"][1] == {
        "key": "copperWeight", "unit": "oz", "default": 1, "min": 0.5, "max": 4,
    }
    via = {spec["key"]: spec for spec in CALCULATORS["via-calculator"]["inputs"]}
    assert via["copperThickness"]["default"] == 25  # plating, not the layer's foil
