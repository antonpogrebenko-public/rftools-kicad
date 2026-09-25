"""The release builder (task 5.2): archive layout, SHA-256, PCM files, version agreement."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import zipfile

import jsonschema
import pytest

import rftools_kicad
from tests.support import ROOT

TIMESTAMP = 1790380800  # 2026-09-26 00:00:00 UTC
TAG = f"v{rftools_kicad.__version__}"
VERSION = rftools_kicad.__version__


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_release", ROOT / "scripts" / "build_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load_builder()


@pytest.fixture
def built(tmp_path):
    out = tmp_path / "dist"
    release = B.build(TAG, out, timestamp=TIMESTAMP)
    return out, release


def copy_of_repo(tmp_path):
    """The parts of the repository the builder reads, to edit without touching the real one."""
    root = tmp_path / "repo"
    for name in ("plugins", "resources", "pcm"):
        shutil.copytree(ROOT / name, root / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(ROOT / "metadata.json", root / "metadata.json")
    return root


def v2_validator(definition):
    schema = json.loads((ROOT / "pcm" / "schemas" / "pcm.v2.schema.json").read_text())
    schema["$ref"] = f"#/definitions/{definition}"
    return jsonschema.Draft7Validator(schema)


# ── The archive ──────────────────────────────────────────────────────────────


def test_the_archive_has_kicads_plugin_layout_and_nothing_else(built):
    out, release = built
    assert release["archive"] == f"rftools-kicad-{VERSION}.zip"
    with zipfile.ZipFile(out / release["archive"]) as archive:
        names = [i.filename for i in archive.infolist() if not i.is_dir()]
        metadata = archive.read("metadata.json")
    tops = {name.split("/")[0] for name in names}
    assert tops == {"plugins", "resources", "metadata.json"}
    assert "resources/icon.png" in names
    # The plugin sits directly in plugins/, not a level deeper.
    assert "plugins/plugin.json" in names and "plugins/requirements.txt" in names
    assert "plugins/rftools_kicad/main.py" in names
    for module in ("dialog", "dialog_logic", "report", "netclasses"):
        assert f"plugins/rftools_kicad/{module}.py" in names
    # Every file of the plugin, and no cache or hidden file.
    expected = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "plugins").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith(".")
        and p.suffix not in (".pyc", ".pyo")
    )
    assert sorted(n for n in names if n.startswith("plugins/")) == expected
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    # The metadata inside is the repository's, with no download fields.
    assert metadata == (ROOT / "metadata.json").read_bytes()
    assert b"download_" not in metadata


def test_the_entrypoint_resolves_as_kicad_extracts_the_package(built, tmp_path):
    out, release = built
    target = tmp_path / "3rdparty" / "plugins" / "io_rftools_kicad"
    with zipfile.ZipFile(out / release["archive"]) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.startswith("plugins/"):
                continue
            path = target / info.filename.removeprefix("plugins/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(info))
    plugin = json.loads((target / "plugin.json").read_text())
    for action in plugin["actions"]:
        assert (target / action["entrypoint"]).is_file()
        for icon in action["icons-light"] + action["icons-dark"]:
            assert (target / icon).is_file()


def test_the_same_commit_builds_the_same_bytes(tmp_path):
    one = B.build(TAG, tmp_path / "one", timestamp=TIMESTAMP)
    two = B.build(TAG, tmp_path / "two", timestamp=TIMESTAMP)
    assert one["sha256"] == two["sha256"]
    assert (tmp_path / "one" / one["archive"]).read_bytes() == (
        tmp_path / "two" / two["archive"]
    ).read_bytes()


# ── The PCM repository files ─────────────────────────────────────────────────


def test_packages_json_names_the_archive_by_its_sha256_and_sizes(built):
    out, release = built
    data = (out / release["archive"]).read_bytes()
    packages = json.loads((out / "site" / "packages.json").read_text())
    [package] = packages["packages"]
    [version] = package["versions"]
    assert version["download_sha256"] == hashlib.sha256(data).hexdigest() == release["sha256"]
    assert version["download_size"] == len(data)
    with zipfile.ZipFile(out / release["archive"]) as archive:
        assert version["install_size"] == sum(i.file_size for i in archive.infolist())
    assert version["download_url"] == (
        "https://github.com/antonpogrebenko-public/rftools-kicad/releases/download/"
        f"{TAG}/rftools-kicad-{VERSION}.zip"
    )
    assert version["runtime"] == "ipc" and version["kicad_version"] == "10.0"
    assert package["identifier"] == "io.rftools.kicad" and "$schema" not in package
    B.verify(out / release["archive"], packages, VERSION)


def test_packages_json_validates_against_the_pinned_pcm_v2_schema(built):
    out, _ = built
    packages = json.loads((out / "site" / "packages.json").read_text())
    errors = list(v2_validator("PackageArray").iter_errors(packages))
    assert not errors, [e.message for e in errors]


def test_repository_json_is_schema_version_2_and_points_at_the_package_list(built):
    out, release = built
    repository = json.loads((out / "site" / "repository.json").read_text())
    assert not list(v2_validator("Repository").iter_errors(repository))
    assert repository["schema_version"] == 2  # KiCad reads it from the body
    base = "https://antonpogrebenko-public.github.io/rftools-kicad"
    assert release["repository_url"] == f"{base}/repository.json"
    packages = (out / "site" / "packages.json").read_bytes()
    assert repository["packages"]["url"] == f"{base}/packages.json"
    assert repository["packages"]["sha256"] == hashlib.sha256(packages).hexdigest()
    assert repository["packages"]["update_timestamp"] == TIMESTAMP
    assert repository["packages"]["update_time_utc"] == "2026-09-26 00:00:00"
    resources = (out / "site" / "resources.zip").read_bytes()
    assert repository["resources"]["sha256"] == hashlib.sha256(resources).hexdigest()
    with zipfile.ZipFile(out / "site" / "resources.zip") as archive:
        assert archive.namelist() == ["io.rftools.kicad/icon.png"]
        assert archive.read("io.rftools.kicad/icon.png") == (
            ROOT / "resources" / "icon.png"
        ).read_bytes()


def test_a_fork_publishes_under_its_own_pages_and_releases(tmp_path):
    release = B.build(TAG, tmp_path, repo="Someone/rftools-kicad", timestamp=TIMESTAMP)
    assert release["repository_url"] == "https://someone.github.io/rftools-kicad/repository.json"
    assert release["download_url"].startswith(
        "https://github.com/Someone/rftools-kicad/releases/download/"
    )


def test_earlier_versions_stay_listed_newest_first(tmp_path):
    previous = {"packages": [{
        "identifier": "io.rftools.kicad",
        "versions": [
            {"version": VERSION, "status": "testing", "kicad_version": "10.0",
             "download_url": "https://example.com/old-build.zip"},
            {"version": "0.0.9", "status": "testing", "kicad_version": "10.0",
             "runtime": "ipc", "download_url": "https://example.com/0.0.9.zip",
             "download_sha256": "a" * 64, "download_size": 1, "install_size": 2},
        ],
    }, {"identifier": "someone.else", "versions": []}]}
    B.build(TAG, tmp_path, previous=previous, timestamp=TIMESTAMP)
    packages = json.loads((tmp_path / "site" / "packages.json").read_text())
    [package] = packages["packages"]
    versions = package["versions"]
    assert [v["version"] for v in versions] == [VERSION, "0.0.9"]
    assert versions[0]["download_url"].endswith(f"rftools-kicad-{VERSION}.zip")  # replaced
    assert not list(v2_validator("PackageArray").iter_errors(packages))


# ── Version agreement ────────────────────────────────────────────────────────


def test_a_matching_tag_passes_the_check(capsys):
    assert B.main(["check", "--tag", TAG]) == 0
    assert VERSION in capsys.readouterr().out


def test_a_mismatched_tag_exits_non_zero_before_building(tmp_path, capsys):
    out = tmp_path / "dist"
    assert B.main(["build", "--tag", "v9.9.9", "--out", str(out)]) == 1
    assert not out.exists()
    err = capsys.readouterr().err
    assert "v9.9.9 names version 9.9.9" in err and f"metadata.json says {VERSION}" in err


def test_metadata_and_package_versions_must_agree_too(tmp_path, capsys):
    root = copy_of_repo(tmp_path)
    init = root / "plugins" / "rftools_kicad" / "__init__.py"
    init.write_text(init.read_text().replace(f'"{VERSION}"', '"0.1.1"'))
    out = tmp_path / "dist"
    assert B.main(["--root", str(root), "build", "--tag", TAG, "--out", str(out)]) == 1
    assert not out.exists()
    assert "rftools_kicad.__version__ says 0.1.1" in capsys.readouterr().err


@pytest.mark.parametrize("tag", ["0.1.0", "release-0.1.0", "v0.1.0-rc1"])
def test_a_tag_that_is_not_a_pcm_version_is_refused(tag):
    with pytest.raises(B.ReleaseError):
        B.check_tag(tag)


def test_the_tag_may_come_as_a_full_ref():
    assert B.check_tag(f"refs/tags/{TAG}") == VERSION


def test_verify_catches_an_archive_that_is_not_the_published_one(built, tmp_path):
    out, release = built
    packages = json.loads((out / "site" / "packages.json").read_text())
    other = tmp_path / "other.zip"
    other.write_bytes((out / release["archive"]).read_bytes() + b"\0")
    with pytest.raises(B.ReleaseError, match="SHA-256"):
        B.verify(other, packages, VERSION)
    assert B.main(["verify", "--archive", str(other), "--packages",
                   str(out / "site" / "packages.json"), "--version", VERSION]) == 1


def test_pages_url(capsys):
    assert B.main(["pages-url"]) == 0
    assert capsys.readouterr().out.strip() == (
        "https://antonpogrebenko-public.github.io/rftools-kicad"
    )
