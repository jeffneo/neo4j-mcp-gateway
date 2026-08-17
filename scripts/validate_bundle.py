#!/usr/bin/env python
"""Validate that every tool in a bundle runs against a live Neo4j — no MCP.

Runs each tool with its default parameters through the real loader/executor and
reports OK / FAIL / SKIP (a tool with a required parameter and no default is
skipped, since we can't guess a value). Exits non-zero if any tool FAILs — so it
doubles as a CI check.

Usage:
    uv run python scripts/validate_bundle.py [bundle_name]

Data must already be loaded into the target database (e.g. via cypher-shell with
the bundle's data/*.cypher). Connection comes from the resolved config for the
bundle (root .env, then bundle .env, then bundle.yaml).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.config import Config
from gateway.yaml_tools import Neo4jExecutor, load_tool_specs


def main(argv: list[str]) -> int:
    bundle = argv[0] if argv and not argv[0].startswith("-") else None
    config = Config.from_env(active_bundle=bundle)

    print(f"validating bundle '{config.active_bundle}'  (database: {config.neo4j_database})")
    try:
        specs = load_tool_specs(config.tools_dir)
    except Exception as exc:
        print(f"FAIL: could not load tools: {exc}", file=sys.stderr)
        return 1

    if not specs:
        print(f"no tools found in {config.tools_dir}")
        return 0

    executor = Neo4jExecutor(config)
    failures = 0
    try:
        for spec in sorted(specs, key=lambda s: s.name):
            missing = [p.name for p in spec.parameters if p.required and not p.has_default]
            if missing:
                print(f"  SKIP  {spec.name:28} (needs args: {', '.join(missing)})")
                continue
            params = {p.name: p.default for p in spec.parameters if p.has_default}
            try:
                rows = executor.run(spec.cypher, params, spec.read_only)
                print(f"  OK    {spec.name:28} {len(rows)} row(s)")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {spec.name:28} {exc}")
    finally:
        executor.close()

    print(f"\n{'FAILED' if failures else 'PASSED'}: "
          f"{len(specs)} tool(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
