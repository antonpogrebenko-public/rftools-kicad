#!/usr/bin/env python3
"""Prove every dependency installs from a wheel on every interpreter KiCad runs the plugin on.

KiCad 10 builds the plugin's environment with

    <interpreter> -m venv --system-site-packages <env>
    pip install --no-input --isolated --only-binary :all: --require-virtualenv \\
        -r requirements.txt

so a dependency, or a dependency of one, that has no wheel for the user's
platform and Python fails the install and the action never appears. The
plugin runs on KiCad's default interpreter (design Decision 7a), so the
targets are those interpreters:

    macOS    KiCad's bundled Python 3.9, on arm64 and on x86_64 machines
    Windows  KiCad's bundled Python 3.11 (amd64)
    Linux    the system python3: 3.11 (Debian 12), 3.12 (Ubuntu 24.04),
             3.13 (Debian 13), on x86_64 and aarch64

This script runs ``pip download --only-binary :all:`` for
plugins/requirements.txt against each target, from any machine, and fails if
any target needs an sdist. wx is not a requirement: it comes with KiCad's
bundled Python on macOS and Windows and from python3-wxgtk4.0 on Linux.

KiCad's bundled macOS Python is a universal2 build, but pip in it installs the
wheels for the architecture it is running on (arm64, or x86_64 on an Intel Mac
or under Rosetta), taking a universal2 wheel only where that is what a project
publishes. cffi (kicad-python's pynng needs it) and rpds-py (its jsonschema
does) publish per-architecture macOS wheels only, so a universal2-only target
could never pass; the two per-architecture targets are the real ones.

Each requirement's own environment marker is evaluated for the *target*.
Markers on transitive dependencies are evaluated by pip for the machine
running the script, a limit of ``pip download --platform``.

    python3 scripts/check_wheels.py
    python3 scripts/check_wheels.py --target linux-x86_64 --python 3.13
    python3 scripts/check_wheels.py --relax-unpublished   # see below

``--relax-unpublished`` drops the lower bound of a requirement that no release
on PyPI satisfies yet, checks the newest published release instead, and says
so. Without it such a target fails.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "plugins" / "requirements.txt"

_DARWIN = {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix"}
_LINUX = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"}
_LINUX_PYTHONS = ("3.11", "3.12", "3.13")

#: name -> (pip --platform tags, marker environment, Python versions). pip
#: widens a macOS tag to older macOS releases, and an arm64 or x86_64 one to
#: universal2, by itself; it does not widen a PEP 600 manylinux tag, so the
#: 2014 alias is named too.
TARGETS = {
    "macos-arm64": (["macosx_12_0_arm64"], dict(_DARWIN, platform_machine="arm64"), ("3.9",)),
    "macos-x86_64": (["macosx_12_0_x86_64"], dict(_DARWIN, platform_machine="x86_64"), ("3.9",)),
    "windows-amd64": (
        ["win_amd64"],
        {"sys_platform": "win32", "platform_system": "Windows", "platform_machine": "AMD64",
         "os_name": "nt"},
        ("3.11",),
    ),
    "linux-x86_64": (
        ["manylinux_2_28_x86_64", "manylinux2014_x86_64"],
        dict(_LINUX, platform_machine="x86_64"), _LINUX_PYTHONS,
    ),
    "linux-aarch64": (
        ["manylinux_2_28_aarch64", "manylinux2014_aarch64"],
        dict(_LINUX, platform_machine="aarch64"), _LINUX_PYTHONS,
    ),
}

PYTHONS = tuple(sorted({v for *_, versions in TARGETS.values() for v in versions},
                       key=lambda v: tuple(map(int, v.split(".")))))


def published_versions(name: str) -> list[Version]:
    url = f"https://pypi.org/pypi/{name}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "rftools-kicad-wheel-check/1"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        releases = json.load(response)["releases"]
    return sorted(Version(v) for v, files in releases.items() if files)


def requirement_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def for_target(lines: list[str], marker_env: dict, python: str, relax: bool) -> tuple:
    """The requirement lines that apply to the target, and any relaxations made."""
    env = dict(default_environment())
    env.update(marker_env)
    env.update({"python_version": python, "python_full_version": f"{python}.0",
                "implementation_name": "cpython", "platform_python_implementation": "CPython"})
    kept, notes = [], []
    for line in lines:
        req = Requirement(line)
        if req.marker is not None and not req.marker.evaluate(env):
            continue
        spec = req.specifier
        if relax and spec and not any(spec.contains(v) for v in published_versions(req.name)):
            spec = SpecifierSet(",".join(str(s) for s in spec if s.operator not in (">=", ">")))
            notes.append(f"{req.name}{req.specifier} is not on PyPI yet; checked {req.name}{spec}")
        kept.append(f"{req.name}{spec}")
    return kept, notes


def check(target: str, python: str, relax: bool) -> tuple[bool, str, list[str]]:
    platforms, marker_env, _ = TARGETS[target]
    lines, notes = for_target(requirement_lines(REQUIREMENTS), marker_env, python, relax)
    with tempfile.TemporaryDirectory() as tmp:
        req_file = Path(tmp) / "requirements.txt"
        req_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [sys.executable, "-m", "pip", "download", "--quiet", "--only-binary", ":all:",
               "--implementation", "cp", "--python-version", python,
               "--abi", "cp" + python.replace(".", ""), "-r", str(req_file),
               "-d", str(Path(tmp) / "wheels")]
        for platform in platforms:
            cmd += ["--platform", platform]
        result = subprocess.run(cmd, capture_output=True, text=True)
        wheels = sorted(p.name for p in (Path(tmp) / "wheels").glob("*.whl"))
    if result.returncode != 0:
        return False, "\n".join(result.stderr.strip().splitlines()[-6:]), notes
    return True, f"{len(wheels)} wheels for {', '.join(lines)}", notes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--relax-unpublished", action="store_true")
    parser.add_argument("--target", action="append", choices=sorted(TARGETS))
    parser.add_argument("--python", action="append", choices=PYTHONS)
    args = parser.parse_args(argv)
    failed = []
    for target in args.target or TARGETS:
        for python in TARGETS[target][2]:
            if args.python and python not in args.python:
                continue
            ok, detail, notes = check(target, python, args.relax_unpublished)
            print(f"{'ok  ' if ok else 'FAIL'} {target} cp{python}: {detail}")
            for note in notes:
                print(f"::warning::{target} cp{python}: {note}")
            if not ok:
                failed.append(f"{target} cp{python}")
    if failed:
        print(f"No wheel for every dependency on: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
