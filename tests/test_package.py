"""The package: plugin.json and metadata.json against KiCad's pinned schemas, icons, versions."""
from __future__ import annotations

import importlib.util
import json
import struct

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
    assert plugin["runtime"] == {"type": "python", "min_version": "3.12"}
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


def test_requirements_are_bounded_and_wx_is_left_to_linux_distributions():
    from packaging.requirements import Requirement

    lines = (PLUGIN_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    reqs = {r.name.lower(): r for r in map(Requirement, filter(None, lines))}
    assert str(reqs["rftools-io"].specifier) == "<0.5,>=0.4"
    assert str(reqs["kicad-python"].specifier) == "<0.10,>=0.8"
    wx = reqs["wxpython"]
    assert str(wx.specifier) == "<4.3,>=4.2.2"
    assert not wx.marker.evaluate({"sys_platform": "linux"})
    assert wx.marker.evaluate({"sys_platform": "darwin"})
    assert wx.marker.evaluate({"sys_platform": "win32"})
    for req in reqs.values():
        operators = {s.operator for s in req.specifier}
        assert ">=" in operators and "<" in operators, req


def test_every_module_defers_annotations():
    for path in (PLUGIN_DIR / "rftools_kicad").glob("*.py"):
        assert "from __future__ import annotations" in path.read_text(encoding="utf-8"), path
