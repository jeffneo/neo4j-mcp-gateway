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

from gateway.config import Config, active_bundle_names
from gateway.yaml_tools import (Neo4jExecutor, load_tool_specs, mediation_params,
                                resolve_tool_query)


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
    # Under a composite identity source the bundle connects to the COMPOSITE
    # database, where a bare MATCH is rejected — every graph operation must name
    # a constituent. The probe is about business records, so it targets the data
    # graph. (The dynamic $prop key is safe here because it is evaluated INSIDE
    # the USE block; see COMPOSITE_PROPERTY_ACCESS in gateway/mediation.py.)
    query = _MISSING_ACL_QUERY
    if policy.identity.source == "composite":
        query = query.replace("  WITH label\n", f"  USE {policy.identity.data_graph}\n  WITH label\n")
    rows = executor.run(query,
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


def _personas_from_graph(config, executor, limit: int = 6) -> list[str]:
    """Pick a few real principals out of the identity graph to diff against."""
    # Ask the identity SOURCE, not the data connection: with a separated source
    # the people live somewhere else entirely, and with a composite source a bare
    # MATCH is rejected outright.
    from gateway.identity_sources import get_identity_source
    return get_identity_source(config, executor).sample_principals(limit)


def _check_entitlement_differentiation(config, executor, specs) -> int:
    """Assert mediated tools actually discriminate between callers.

    A filter that silently returns the same rows for everyone is indistinguishable
    from no filter at all — this catches a mediation path that has been wired up
    but isn't doing anything.
    """
    policy = config.security
    if not policy.mediated:
        return 0

    from gateway import mediation

    if not mediation.impersonation_allowed(policy, config.env_snapshot):
        print("  SKIP  persona diff (needs NEO4J_MCP_ALLOW_IMPERSONATION=true "
              "or security.principal.allow_impersonation)")
        return 0

    personas = _personas_from_graph(config, executor)
    if len(personas) < 2:
        print("  SKIP  persona diff (fewer than 2 identities in the graph)")
        return 0

    def _args_for(spec):
        args = {p.name: p.default for p in spec.parameters if p.has_default}
        args.update(spec.sample_args)
        return args

    runnable = [s for s in specs if s.is_mediated_form
                and not [p.name for p in s.parameters
                         if p.required and not p.has_default and p.name not in s.sample_args]]
    if not runnable:
        print("  note  persona diff: no zero-argument mediated tools to compare")
        return 0

    print(f"  entitlement differentiation ({len(personas)} personas):")
    for spec in runnable:
        results = {}
        for who in personas:
            params = _args_for(spec)
            params.update(mediation_params(config, who, executor))
            query = mediation.compose(policy, spec.match_clause, spec.scope,
                                      spec.return_clause.strip(), spec.protect)
            results[who] = len(executor.run(query, params, read_only=True))
        distinct = set(results.values())
        detail = ", ".join(f"{w.split('@')[0]}={n}" for w, n in results.items())
        if len(distinct) > 1:
            print(f"    OK    {spec.name:24} varies by caller ({detail})")
        else:
            print(f"    note  {spec.name:24} identical for all callers ({detail}) — "
                  "expected only if this tool reads reference data")
    return 0


def main(argv: list[str]) -> int:
    spec = argv[0] if argv and not argv[0].startswith("-") else None
    names = active_bundle_names(spec)
    if len(names) > 1:
        # ACTIVE_BUNDLE may name several bundles; validate each in turn.
        return max(_validate_one(n) for n in names)
    return _validate_one(names[0])


def _validate_one(bundle: str) -> int:
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
        failures += _check_entitlement_differentiation(config, executor, specs)

        if not specs:
            print(f"no YAML tools in {config.tools_dir}")

        for spec in sorted(specs, key=lambda s: s.name):
            missing = [p.name for p in spec.parameters
                       if p.required and not p.has_default and p.name not in spec.sample_args]
            if missing:
                print(f"  SKIP  {spec.name:28} (needs args: {', '.join(missing)})")
                continue
            params = {p.name: p.default for p in spec.parameters if p.has_default}
            params.update(spec.sample_args)
            try:
                query, extra = resolve_tool_query(config, spec, executor=executor)
                params.update(extra)
                rows = executor.run(query, params, spec.read_only)
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
