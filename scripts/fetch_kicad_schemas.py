#!/usr/bin/env python3
"""Pin KiCad's published JSON schemas under pcm/schemas/.

The plugin's two metadata files are validated against KiCad's own schemas in
CI (tests/test_package.py), from the copies pinned here, so a test run needs
no network and a schema change upstream is a reviewed commit, not a silent
CI failure.

    plugins/plugin.json   the IPC plugin schema, https://go.kicad.org/api/schemas/v1
                          (which redirects to the KiCad master branch), and the
                          same file on the 10.0 branch, which the current stable
                          KiCad ships: plugin.json must satisfy both.
    metadata.json         the PCM package schemas v1 and v2 from the 10.0 branch.
                          KiCad 10.0 reads v2; a package whose type v1 knows
                          ("plugin") is also served to older KiCad, so the
                          template satisfies both.

    python3 scripts/fetch_kicad_schemas.py           # refresh the pinned copies
    python3 scripts/fetch_kicad_schemas.py --check   # exit 1 if upstream differs
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "pcm" / "schemas"
RAW = "https://gitlab.com/kicad/code/kicad/-/raw"

SCHEMAS = {
    "api.v1.schema.json": "https://go.kicad.org/api/schemas/v1",
    "api.v1.schema.10.0.json": f"{RAW}/10.0/api/schemas/api.v1.schema.json",
    "pcm.v1.schema.json": f"{RAW}/10.0/kicad/pcm/schemas/pcm.v1.schema.json",
    "pcm.v2.schema.json": f"{RAW}/10.0/kicad/pcm/schemas/pcm.v2.schema.json",
}


def fetch(url: str) -> bytes:
    # urllib follows the go.kicad.org redirect to gitlab.com. GitLab refuses
    # urllib's default User-Agent with a 403, so name the script.
    request = urllib.request.Request(url, headers={"User-Agent": "rftools-kicad-schema-pin/1"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    args = parser.parse_args(argv)

    DEST.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, url in SCHEMAS.items():
        body = fetch(url)
        path = DEST / name
        if args.check:
            if not path.is_file() or path.read_bytes() != body:
                stale.append(name)
        else:
            path.write_bytes(body)
            print(f"{name} <- {url}")
    if stale:
        print(f"Pinned schema(s) differ from upstream: {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
