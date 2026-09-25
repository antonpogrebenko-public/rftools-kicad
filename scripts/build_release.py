#!/usr/bin/env python3
"""Build the PCM release: the package archive and the custom PCM repository files.

Used by .github/workflows/release.yml on a ``v*`` tag (task 5.2):

    python scripts/build_release.py check --tag v0.1.0
    python scripts/build_release.py build --tag v0.1.0 --out dist \\
        [--repo owner/name] [--previous packages.json] [--timestamp SECONDS]
    python scripts/build_release.py verify --archive downloaded.zip \\
        --packages dist/site/packages.json --version 0.1.0
    python scripts/build_release.py pages-url [--repo owner/name]

``check`` exits non-zero unless the tag names exactly the version in
``metadata.json`` and ``rftools_kicad.__version__`` (KiCad's ``plugin.json`` has
no version field). ``build`` runs the same check before writing anything, then
writes into ``--out``:

    rftools-kicad-<version>.zip   the package archive
    site/packages.json            the package list (every published version)
    site/repository.json          the repository, schema_version 2 in the body
    site/resources.zip            <identifier>/icon.png, the icon the PCM lists
    release.json                  version, archive name, sha256 and sizes

The archive follows KiCad's add-on layout (dev-docs.kicad.org/en/addons/,
checked 2026-09-25): ``plugins/`` holding the plugin directly (``plugin.json``,
``requirements.txt``, the icons and the ``rftools_kicad`` package), ``resources/
icon.png`` (64 × 64) and ``metadata.json`` at the root, nothing else. KiCad
extracts ``plugins/…`` to ``<3rd party>/plugins/io_rftools_kicad/…`` and ignores
directory entries and root files other than ``metadata.json``
(kicad/pcm/pcm_task_manager.cpp, branch 10.0). The archive's ``metadata.json``
is the repository's, without ``download_*`` fields, which only the package
list carries. Entries are sorted, dated from ``--timestamp`` and written with
fixed permissions, so the same commit builds the same bytes and the same
SHA-256.

KiCad 10 sends ``Accept: application/vnd.kicad.pcm.v2+json`` but reads the
schema version from the body's ``schema_version`` (kicad/pcm/pcm.cpp), so
GitHub Pages serves these files as they are. KiCad refetches the package list
only when ``packages.update_timestamp`` changes, which every release does.
The package list keeps earlier versions from ``--previous`` (the list currently
published), newest first, so the PCM can still offer them.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_INIT = Path("plugins") / "rftools_kicad" / "__init__.py"
SCHEMA = Path("pcm") / "schemas" / "pcm.v2.schema.json"

DEFAULT_REPO = "antonpogrebenko-public/rftools-kicad"
REPOSITORY_NAME = "rftools.io KiCad plugins"
MAINTAINER = {"name": "rftools.io", "contact": {"web": "https://rftools.io"}}

#: What the archive holds, relative to the repository root.
ARCHIVE_DIRS = ("plugins", "resources")
ARCHIVE_FILES = ("metadata.json",)
#: Never packaged: caches, editor and OS litter, hidden files.
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".orig", ".rej", ".swp"}

#: The PCM schema's version pattern (PackageVersion.version).
VERSION_PATTERN = re.compile(r"^\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?$")
#: ZIP cannot date a file before 1980.
ZIP_EPOCH = 315532800


class ReleaseError(Exception):
    """A release that must not be built; the message says why."""


# ── Versions ─────────────────────────────────────────────────────────────────


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def metadata_version(root: Path = ROOT) -> dict:
    """The one version entry of metadata.json (the version being released)."""
    versions = load_json(root / "metadata.json").get("versions") or []
    if len(versions) != 1:
        raise ReleaseError(
            f"metadata.json must list exactly one version (the one being released), "
            f"not {len(versions)}."
        )
    return versions[0]


def package_version(root: Path = ROOT) -> str:
    """``rftools_kicad.__version__``, read from the source without importing it."""
    tree = ast.parse((root / PACKAGE_INIT).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ReleaseError(f"{PACKAGE_INIT} sets no __version__")


def check_tag(tag: str, root: Path = ROOT) -> str:
    """The version *tag* names, when the tag, metadata.json and the package agree."""
    tag = tag.removeprefix("refs/tags/")
    if not tag.startswith("v"):
        raise ReleaseError(f"A release tag is v<version>, such as v0.1.0; got {tag!r}.")
    version = tag[1:]
    meta = metadata_version(root).get("version")
    package = package_version(root)
    if version != meta or version != package:
        raise ReleaseError(
            f"The tag {tag} names version {version}, but metadata.json says {meta} and "
            f"rftools_kicad.__version__ says {package}. They must all agree; nothing was "
            "built or published."
        )
    if not VERSION_PATTERN.match(version):
        raise ReleaseError(
            f"{version} is not a version the PCM accepts (major[.minor[.patch]], digits only)."
        )
    return version


# ── The archive ──────────────────────────────────────────────────────────────


def archive_name(version: str) -> str:
    return f"rftools-kicad-{version}.zip"


def archive_files(root: Path = ROOT) -> list:
    """``(archive name, path)`` for every file the archive holds, sorted."""
    files = []
    for top in ARCHIVE_DIRS:
        for path in sorted((root / top).rglob("*")):
            relative = path.relative_to(root)
            if not path.is_file() or _excluded(relative):
                continue
            files.append((relative.as_posix(), path))
    for name in ARCHIVE_FILES:
        files.append((name, root / name))
    return sorted(files)


def _excluded(relative: Path) -> bool:
    return (
        any(part in EXCLUDED_PARTS or part.startswith(".") for part in relative.parts)
        or relative.suffix in EXCLUDED_SUFFIXES
    )


def zip_bytes(entries: list, timestamp: int, with_directories: bool = True) -> bytes:
    """A deterministic ZIP of ``(name, bytes)``: sorted, dated *timestamp*, mode 0644.

    With *with_directories*, directory entries are written too (KiCad ignores them
    in a package; unzip tools like them).
    """
    date = datetime.fromtimestamp(max(timestamp, ZIP_EPOCH), UTC).timetuple()[:6]
    directories = sorted({
        "/".join(name.split("/")[:i]) + "/"
        for name, _ in entries
        for i in range(1, name.count("/") + 1)
    }) if with_directories else []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in directories:
            info = zipfile.ZipInfo(name, date_time=date)
            info.create_system = 3
            info.external_attr = (0o40755 << 16) | 0x10
            archive.writestr(info, b"")
        for name, data in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=date)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


def build_archive(root: Path, timestamp: int) -> tuple:
    """``(archive bytes, install size)``: the size is the files' total, unpacked."""
    entries = [(name, path.read_bytes()) for name, path in archive_files(root)]
    return zip_bytes(entries, timestamp), sum(len(data) for _, data in entries)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── The PCM repository ───────────────────────────────────────────────────────


def pages_base(repo: str) -> str:
    """``https://<owner>.github.io/<name>``: where GitHub Pages serves the repository."""
    owner, name = _split_repo(repo)
    return f"https://{owner.lower()}.github.io/{name}"


def download_url(repo: str, tag: str, version: str) -> str:
    owner, name = _split_repo(repo)
    return f"https://github.com/{owner}/{name}/releases/download/{tag}/{archive_name(version)}"


def _split_repo(repo: str) -> tuple:
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ReleaseError(f"--repo is owner/name, not {repo!r}")
    return parts[0], parts[1]


def packages_json(metadata: dict, version_entry: dict, previous: dict | None = None) -> dict:
    """The package list: this package with the new version first, then earlier ones."""
    package = {k: copy.deepcopy(v) for k, v in metadata.items() if k != "$schema"}
    versions = [version_entry]
    for old in _previous_versions(previous, metadata["identifier"]):
        if old.get("version") != version_entry["version"]:
            versions.append(old)
    versions.sort(key=lambda v: _version_key(v.get("version", "0")), reverse=True)
    package["versions"] = versions
    return {"packages": [package]}


def _previous_versions(previous: dict | None, identifier: str) -> list:
    for package in (previous or {}).get("packages") or []:
        if isinstance(package, dict) and package.get("identifier") == identifier:
            return [copy.deepcopy(v) for v in package.get("versions") or []
                    if isinstance(v, dict) and v.get("download_url")]
    return []


def _version_key(version: str) -> tuple:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def resource(url: str, data: bytes, timestamp: int) -> dict:
    return {
        "url": url,
        "sha256": sha256(data),
        "update_timestamp": timestamp,
        "update_time_utc": datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S"),
    }


def repository_json(base: str, packages: bytes, resources: bytes, timestamp: int) -> dict:
    return {
        "$schema": "https://go.kicad.org/pcm/schemas/v2#/definitions/Repository",
        "name": REPOSITORY_NAME,
        "maintainer": MAINTAINER,
        "packages": resource(f"{base}/packages.json", packages, timestamp),
        "resources": resource(f"{base}/resources.zip", resources, timestamp),
        "schema_version": 2,
    }


def dumps(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def validate(data: dict, definition: str, root: Path = ROOT) -> None:
    """Validate *data* against a definition of the pinned PCM v2 schema."""
    import jsonschema

    schema = load_json(root / SCHEMA)
    schema = dict(schema, **{"$ref": f"#/definitions/{definition}"})
    errors = sorted(jsonschema.Draft7Validator(schema).iter_errors(data), key=str)
    if errors:
        detail = "; ".join(f"{'/'.join(map(str, e.path)) or '(top)'}: {e.message}" for e in errors)
        raise ReleaseError(f"{definition} does not validate against the pinned PCM v2 schema: "
                           f"{detail}")


# ── Build ────────────────────────────────────────────────────────────────────


def build(
    tag: str,
    out: Path,
    *,
    repo: str = DEFAULT_REPO,
    previous: dict | None = None,
    timestamp: int | None = None,
    root: Path = ROOT,
) -> dict:
    """Check, then write the archive and the repository files into *out*; returns release.json."""
    version = check_tag(tag, root)  # before anything is written
    timestamp = int(timestamp if timestamp is not None else datetime.now(UTC).timestamp())
    metadata = load_json(root / "metadata.json")
    archive, install_size = build_archive(root, timestamp)
    name = archive_name(version)
    entry = dict(metadata_version(root))
    entry.update({
        "download_url": download_url(repo, tag.removeprefix("refs/tags/"), version),
        "download_sha256": sha256(archive),
        "download_size": len(archive),
        "install_size": install_size,
    })
    packages = packages_json(metadata, entry, previous)
    validate(packages, "PackageArray", root)
    packages_bytes = dumps(packages)
    identifier = metadata["identifier"]
    # KiCad reads a repository's icons as <identifier>/icon.png (kicad/pcm/pcm.cpp).
    resources = zip_bytes([(f"{identifier}/icon.png",
                            (root / "resources" / "icon.png").read_bytes())], timestamp,
                          with_directories=False)
    base = pages_base(repo)
    repository = repository_json(base, packages_bytes, resources, timestamp)
    validate(repository, "Repository", root)

    out = Path(out)
    site = out / "site"
    site.mkdir(parents=True, exist_ok=True)
    (out / name).write_bytes(archive)
    (site / "packages.json").write_bytes(packages_bytes)
    (site / "repository.json").write_bytes(dumps(repository))
    (site / "resources.zip").write_bytes(resources)
    release = {
        "version": version,
        "tag": tag.removeprefix("refs/tags/"),
        "status": entry.get("status"),
        "archive": name,
        "sha256": entry["download_sha256"],
        "download_size": entry["download_size"],
        "install_size": install_size,
        "download_url": entry["download_url"],
        "repository_url": f"{base}/repository.json",
    }
    (out / "release.json").write_bytes(dumps(release))
    return release


def verify(archive: Path, packages: dict, version: str) -> None:
    """The archive is the one the package list names for *version*: its SHA-256 and size."""
    data = Path(archive).read_bytes()
    entries = [v for p in packages.get("packages") or [] for v in p.get("versions") or []
               if v.get("version") == version]
    if len(entries) != 1:
        raise ReleaseError(f"packages.json lists version {version} {len(entries)} times, not once")
    [entry] = entries
    if sha256(data) != entry.get("download_sha256"):
        raise ReleaseError(
            f"{archive} has SHA-256 {sha256(data)}, but packages.json says "
            f"{entry.get('download_sha256')}"
        )
    if len(data) != entry.get("download_size"):
        raise ReleaseError(
            f"{archive} is {len(data)} bytes, but packages.json says {entry.get('download_size')}"
        )


# ── Command line ─────────────────────────────────────────────────────────────


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="the tag, metadata.json and the package agree")
    check.add_argument("--tag", required=True)
    make = commands.add_parser("build", help="write the archive and the repository files")
    make.add_argument("--tag", required=True)
    make.add_argument("--out", type=Path, required=True)
    make.add_argument("--repo", default=DEFAULT_REPO, help="owner/name on GitHub")
    make.add_argument("--previous", type=Path, help="the packages.json published now")
    make.add_argument("--timestamp", type=int, help="seconds since the epoch (the commit's)")
    check_archive = commands.add_parser("verify", help="an archive matches packages.json")
    check_archive.add_argument("--archive", type=Path, required=True)
    check_archive.add_argument("--packages", type=Path, required=True)
    check_archive.add_argument("--version", required=True)
    url = commands.add_parser("pages-url", help="print where GitHub Pages serves the files")
    url.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args(argv)

    try:
        if args.command == "check":
            version = check_tag(args.tag, args.root)
            print(f"{args.tag}: metadata.json and rftools_kicad.__version__ are {version}.")
        elif args.command == "build":
            previous = None
            if args.previous is not None and args.previous.is_file():
                try:
                    previous = load_json(args.previous)
                except ValueError:
                    print(f"Ignoring an unreadable {args.previous}", file=sys.stderr)
            release = build(args.tag, args.out, repo=args.repo, previous=previous,
                            timestamp=args.timestamp, root=args.root)
            print(json.dumps(release, indent=2))
        elif args.command == "verify":
            verify(args.archive, load_json(args.packages), args.version)
            print(f"{args.archive} matches packages.json for {args.version}.")
        else:
            print(pages_base(args.repo))
    except ReleaseError as exc:
        print(f"::error::{exc}" if _in_actions() else f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _in_actions() -> bool:
    import os

    return os.environ.get("GITHUB_ACTIONS") == "true"


if __name__ == "__main__":
    sys.exit(main())
