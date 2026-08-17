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


# Fail-closed data-quality guard. In a mediated bundle, a business record that is
# MISSING its permissions property is treated as reference data and flows to every
# caller. That silent fail-open is exactly the bug you cannot afford, so we assert
# here — at validation/CI time — rather than paying a label check on every query.
_MISSING_ACL_QUERY = """
UNWIND $labels AS label
CALL {
  WITH label
  MATCH (n) WHERE label IN labels(n) AND n[$prop] IS NULL
  RETURN count(n) AS missing
}
RETURN label, missing
ORDER BY label
"""


def _check_protected_labels(config, executor) -> int:
    """Assert every node with a protected label carries the permissions property."""
    policy = config.security
    if not policy.mediated:
        return 0
    if not policy.protected_labels:
        print("  WARN  security.mode=mediated but no protected_labels declared —"
              " unpermissioned records would flow as reference data")
        return 0

    print(f"  entitlement data check ({policy.permissions_property} on "
          f"{', '.join(policy.protected_labels)}):")
    rows = executor.run(_MISSING_ACL_QUERY,
                        {"labels": policy.protected_labels, "prop": policy.permissions_property},
                        read_only=True)
    failures = 0
    for row in rows:
        if row["missing"]:
            failures += 1
            print(f"    FAIL  {row['label']:20} {row['missing']} node(s) missing "
                  f"{policy.permissions_property} (would be readable by everyone)")
        else:
            print(f"    OK    {row['label']:20} all nodes carry an ACL")
    return failures


def main(argv: list[str]) -> int:
    bundle = argv[0] if argv and not argv[0].startswith("-") else None
    config = Config.from_env(active_bundle=bundle)

    print(f"validating bundle '{config.active_bundle}'  "
          f"(database: {config.neo4j_database}, security: {config.security.mode})")
    try:
        specs = load_tool_specs(config.tools_dir)
    except Exception as exc:
        print(f"FAIL: could not load tools: {exc}", file=sys.stderr)
        return 1

    executor = Neo4jExecutor(config)
    failures = 0
    try:
        failures += _check_protected_labels(config, executor)

        if not specs:
            print(f"no YAML tools in {config.tools_dir}")

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
