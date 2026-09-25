"""golden/kicad-golden.json must be a byte-for-byte copy of ../shared/kicad-golden.json.

Skipped in a standalone clone of rftools-kicad, where the monorepo is not there
to compare against (as rftools-py's tests/test_vendored_schemas.py does).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VENDORED = ROOT / "golden" / "kicad-golden.json"
SOURCE = ROOT.parent / "shared" / "kicad-golden.json"

needs_monorepo = pytest.mark.skipif(
    not SOURCE.is_file(), reason="../shared/kicad-golden.json not present (standalone checkout)"
)


@needs_monorepo
def test_vendored_golden_file_is_byte_identical_to_the_monorepo_copy():
    assert VENDORED.is_file(), "Run scripts/vendor_golden.py."
    assert VENDORED.read_bytes() == SOURCE.read_bytes(), (
        "golden/kicad-golden.json is out of date with ../shared/kicad-golden.json. "
        "Run scripts/vendor_golden.py."
    )


def test_the_vendored_golden_file_is_committed_and_well_formed():
    # Not skipped: a standalone clone's CI runs the golden cases from this copy.
    data = json.loads(VENDORED.read_text(encoding="utf-8"))
    assert [c["id"] for c in data["cases"]] == ["G1", "G2", "G3"]
    assert data["grid"] == 0.001
