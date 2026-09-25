#!/usr/bin/env python3
"""Vendor the rfhub monorepo's golden cases into golden/kicad-golden.json.

The three golden cases (design Decision 8) are generated in the monorepo from
the web calculators themselves, by scripts/generate_kicad_golden.ts, into
shared/kicad-golden.json. rftools-kicad sits next to shared/ in a checkout of
the monorepo but is its own repository, so it commits a byte-for-byte copy:

    python3 scripts/vendor_golden.py            # copy ../shared/kicad-golden.json
    python3 scripts/vendor_golden.py --check    # exit 1 if the copy is stale

tests/test_vendored_golden.py compares the copy with ../shared when that file
is present, and skips in a standalone clone, as rftools-py's
tests/test_vendored_schemas.py does for the job schemas.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT.parent / "shared" / "kicad-golden.json"
DEST = ROOT / "golden" / "kicad-golden.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare, do not copy")
    args = parser.parse_args(argv)
    if not SOURCE.is_file():
        print(f"Source not found: {SOURCE}", file=sys.stderr)
        print("Run this from a checkout of the rfhub monorepo "
              "(rftools-kicad must sit next to shared/).", file=sys.stderr)
        return 1
    if args.check:
        if not DEST.is_file() or DEST.read_bytes() != SOURCE.read_bytes():
            print(f"{DEST.relative_to(ROOT)} is stale; run scripts/vendor_golden.py",
                  file=sys.stderr)
            return 1
        return 0
    DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE, DEST)
    print(f"Vendored {SOURCE} into {DEST.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
