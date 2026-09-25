"""The package: plugin.json and metadata.json against KiCad's pinned schemas, icons, versions."""
from __future__ import annotations

import ast
import importlib.util
import json
import struct
import sys

import jsonschema
import pytest

import rftools_kicad
from tests.support import ROOT

PLUGIN_DIR = ROOT / "plugins"
SCHEMAS = ROOT / "pcm" / "schemas"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", data[16:24])


# ── plugin.json ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("schema", ["api.v1.schema.json", "api.v1.schema.10.0.json"])
def test_plugin_json_validates_against_kicads_ipc_schema(schema):
    validator = jsonschema.Draft7Validator(load(SCHEMAS / schema))
    errors = sorted(validator.iter_errors(load(PLUGIN_DIR / "plugin.json")), key=str)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]


def test_the_pinned_ipc_schema_is_the_one_plugin_json_names():
    plugin = load(PLUGIN_DIR / "plugin.json")
    assert plugin["$schema"] == load(SCHEMAS / "api.v1.schema.json")["$id"]
    assert plugin["$schema"] == "https://go.kicad.org/api/schemas/v1"


def test_one_pcb_action_whose_entrypoint_and_icons_exist():
    plugin = load(PLUGIN_DIR / "plugin.json")
    assert plugin["identifier"] == rftools_kicad.IDENTIFIER == "io.rftools.kicad"
    assert plugin["runtime"] == {"type": "python", "min_version": "3.9"}
    [action] = plugin["actions"]
    assert action["scopes"] == ["pcb"]
    assert action["entrypoint"] == "rftools_kicad/main.py"
    assert (PLUGIN_DIR / action["entrypoint"]).is_file()
    for theme in ("icons-light", "icons-dark"):
        sizes = [png_size(PLUGIN_DIR / icon) for icon in action[theme]]
        assert sizes == [(24, 24), (48, 48)], theme


def test_the_ipc_schema_rejects_a_broken_plugin_json():
    validator = jsonschema.Draft7Validator(load(SCHEMAS / "api.v1.schema.json"))
    broken = load(PLUGIN_DIR / "plugin.json")
    broken["actions"][0]["scopes"] = ["board"]
    assert list(validator.iter_errors(broken))


# ── metadata.json ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("schema", ["pcm.v2.schema.json", "pcm.v1.schema.json"])
def test_metadata_json_validates_against_the_pcm_schemas(schema):
    validator = jsonschema.Draft7Validator(load(SCHEMAS / schema))
    errors = sorted(validator.iter_errors(load(ROOT / "metadata.json")), key=str)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]


def test_metadata_is_the_ipc_plugin_for_kicad_10_at_the_package_version():
    meta = load(ROOT / "metadata.json")
    assert meta["$schema"] == load(SCHEMAS / "pcm.v2.schema.json")["$id"]
    assert meta["identifier"] == rftools_kicad.IDENTIFIER
    assert meta["type"] == "plugin" and meta["license"] == "MIT"
    assert len(meta["description"]) <= 150  # the PCM's documented limit
    [version] = meta["versions"]
    assert version["version"] == rftools_kicad.__version__ == "0.1.0"
    assert version["kicad_version"] == "10.0" and version["runtime"] == "ipc"
    # download_* belong only in the repository's copy, never in the archive's.
    assert not [k for k in version if k.startswith("download_") or k == "install_size"]


def test_the_pcm_icon_is_64_by_64():
    assert png_size(ROOT / "resources" / "icon.png") == (64, 64)


def test_the_icons_are_what_the_script_draws():
    spec = importlib.util.spec_from_file_location("make_icons", ROOT / "scripts" / "make_icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for path, size, shapes in module.targets():
        assert path.read_bytes() == module.png(size, module.render(size, shapes)), path


# ── Dependencies ─────────────────────────────────────────────────────────────


def test_requirements_are_kicad_python_and_certifi_bounded():
    from packaging.requirements import Requirement

    lines = (PLUGIN_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    reqs = {r.name.lower(): r for r in map(Requirement, filter(None, lines))}
    # The SDK needs Python 3.12 and KiCad's own Python is 3.9 on macOS; wx comes
    # with KiCad's bundled Python and from python3-wxgtk4.0 on Linux (Decision 7a).
    assert set(reqs) == {"kicad-python", "certifi"}
    assert str(reqs["kicad-python"].specifier) == "<0.10,>=0.8"
    assert str(reqs["certifi"].specifier) == "<2028,>=2024.2.2"
    for req in reqs.values():
        assert req.marker is None, req
        operators = {s.operator for s in req.specifier}
        assert ">=" in operators and "<" in operators, req


def _modules():
    return sorted((PLUGIN_DIR / "rftools_kicad").glob("*.py"))


def test_every_module_defers_annotations():
    for path in _modules():
        assert "from __future__ import annotations" in path.read_text(encoding="utf-8"), path


def test_every_module_parses_with_a_python_3_9_grammar():
    for path in _modules() + [ROOT / "scripts" / "live_golden.py"]:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))


#: What the plugin may import: the standard library, itself, and what KiCad's
#: plugin environment provides (requirements.txt, and wx from KiCad or Linux).
ALLOWED_THIRD_PARTY = {"rftools_kicad", "kipy", "google", "certifi", "wx"}


def test_the_plugin_imports_no_sdk_and_nothing_undeclared():
    stdlib = set(getattr(sys, "stdlib_module_names", ())) or None  # 3.10+
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                assert top not in ("rftools", "httpx", "requests"), (path.name, name)
                if stdlib is not None:
                    assert top in stdlib or top in ALLOWED_THIRD_PARTY or top == "__future__", (
                        path.name, name,
                    )
