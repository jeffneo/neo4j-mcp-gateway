#!/usr/bin/env python
"""Scaffold a new use-case bundle from bundles/_template.

Usage:
    uv run python scripts/new_bundle.py <name>

Creates bundles/<name>/ with a starter bundle.yaml, one example tool, a data
generator stub, and a README — with {{BUNDLE_NAME}} replaced by <name>.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "bundles" / "_template"


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    name = argv[0]
    if not all(c.isalnum() or c in "_-" for c in name) or name.startswith("_"):
        print(f"invalid bundle name {name!r} (use letters/digits/_/-, not starting with _)", file=sys.stderr)
        return 1

    dest = ROOT / "bundles" / name
    if dest.exists():
        print(f"bundle already exists: {dest}", file=sys.stderr)
        return 1
    if not TEMPLATE.exists():
        print(f"template missing: {TEMPLATE}", file=sys.stderr)
        return 1

    shutil.copytree(TEMPLATE, dest)

    # Substitute the {{BUNDLE_NAME}} token in every text file.
    for p in dest.rglob("*"):
        if p.is_file():
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue
            if "{{BUNDLE_NAME}}" in text:
                p.write_text(text.replace("{{BUNDLE_NAME}}", name))

    print(f"created bundles/{name}/")
    print("next:")
    print(f"  1. edit bundles/{name}/bundle.yaml (description + instructions)")
    print(f"  2. write bundles/{name}/data/demo.cypher and load it")
    print(f"  3. add tools, then:  ACTIVE_BUNDLE={name} uv run python scripts/try_tool.py --list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
