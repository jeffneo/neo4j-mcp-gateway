#!/usr/bin/env python
"""What is the entitlement graph? Compute it rather than assert it.

    uv run python scripts/entitlement_surface.py asset_platform
    uv run python scripts/entitlement_surface.py asset_platform --graph   # mermaid

THE POINT. "Entitlement edge" is not a property of an edge. `AUTHORED_BY` is a
business fact when you ask who wrote something and an entitlement route when a
grant traverses it — the same edge, in the same graph. What makes a relationship
type entitlement-bearing is that a DECLARED RULE names it.

So the entitlement graph is not a separate subgraph to be maintained. It is the
PROJECTION of the graph onto the relationship types and properties named in
`security.grants`, `security.denials` and `security.identity` — which means it can
be derived from configuration and printed, instead of being described in a
document that drifts.

That produces the artefact a security review actually wants: **the exact set of
relationship types and properties whose modification changes who can read what.**
No relational entitlement store can produce that list, because the JOINs live in
application code.

THREE CATEGORIES, and the middle one is where the risk is:

  POLICY        exists only to express entitlement — SCOPED_TO, RESTRICTED_FOR.
                Changing one changes access and nothing else. Tightest change
                control; these are the crown jewels.

  DUAL-PURPOSE  a business fact that a rule traverses — AUTHORED_BY, COVERS,
                CLASSIFIED_AS, IN_UNIT. Written by the business feed, and a
                routine edit silently changes entitlement. THIS IS THE DANGEROUS
                CATEGORY: someone reassigns coverage as a CRM action and access
                moves with it. The mitigation is knowing the list.

  DATA          appears in no rule. Normal data governance.

POLARITY IS PER RULE, NOT PER EDGE. A type traversed by a grant enables; by a
denial, disables; and the same type can do both — WORKS_FOR reaches a desk in a
denial here and could reach a unit in a grant. So the graph cannot be coloured
red and green: you have to name the rule. That is why "authorised iff there is an
enabling path and no disabling path" operationalises as "matches a grant pattern
and no denial pattern".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import mediation  # noqa: E402
from gateway.config import Config, active_bundle_names  # noqa: E402
from gateway.yaml_tools import Neo4jExecutor, load_tool_specs  # noqa: E402


def _rel_types(pattern: str) -> list[str]:
    out: list[str] = []
    for token in mediation._PATTERN_TOKEN.findall(pattern or ""):
        if token.startswith(("-", "<")):
            out.extend(mediation._rel_types(token))
    return out


def _props(expr: str) -> list[str]:
    """Property references in a `where` clause, as var.prop or var.`prop`."""
    import re
    out = re.findall(r"\b\w+\.`([^`]+)`", expr or "")
    out += re.findall(r"\b\w+\.([A-Za-z_]\w*)", expr or "")
    return sorted(set(out))


def surface(policy) -> dict:
    enabling: dict[str, list[str]] = {}
    disabling: dict[str, list[str]] = {}
    caller_props: set[str] = set()
    row_props: set[str] = set()
    edge_props: set[str] = set()

    for kind, rules, bucket in (("grant", policy.grants, enabling),
                               ("denial", policy.denials, disabling)):
        for rule in rules:
            label = f"{rule.label}: {rule.reason or '(no reason given)'}"
            for rel in _rel_types(rule.via):
                bucket.setdefault(rel, []).append(label)
            for prop in _props(rule.where):
                # Crude but honest attribution: which variable it hangs off.
                if "caller." in (rule.where or "") and prop in (rule.where or ""):
                    pass
                if f"resource.`{prop}`" in (rule.where or "") or f"resource.{prop}" in (rule.where or ""):
                    row_props.add(prop)
                elif f"caller.{prop}" in (rule.where or "") or f"authz." in (rule.where or ""):
                    caller_props.add(prop)
                else:
                    edge_props.add(prop)

    # The prelude's own traversal is entitlement-bearing by construction.
    for rel in policy.identity.group_rels:
        enabling.setdefault(rel, []).append("identity: resolves the caller's principals")

    return {
        "enabling": enabling, "disabling": disabling,
        "caller_props": sorted(caller_props),
        "row_props": sorted(row_props),
        "edge_props": sorted(edge_props),
        "acl_property": policy.permissions_property,
        "boundary_properties": dict(policy.identity.boundary_properties),
    }


def observed_types(config) -> set[str]:
    """Relationship types actually present in the database, if reachable."""
    try:
        ex = Neo4jExecutor(config)
        rows = ex.run("CALL db.relationshipTypes() YIELD relationshipType "
                      "RETURN collect(relationshipType) AS t", {}, read_only=True)
        ex.close()
        return set(rows[0]["t"]) if rows else set()
    except Exception:
        return set()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle", nargs="?")
    ap.add_argument("--graph", action="store_true", help="emit a mermaid diagram")
    args = ap.parse_args(argv)

    config = Config.from_env(active_bundle=args.bundle or active_bundle_names()[0])
    policy = config.security
    if not policy.mediated:
        print(f"bundle '{config.active_bundle}' is security.mode: {policy.mode} — "
              "no entitlement surface to compute")
        return 0

    s = surface(policy)
    present = observed_types(config)
    decisive = set(s["enabling"]) | set(s["disabling"])

    print(f"entitlement surface — bundle '{config.active_bundle}'\n")

    print(f"  DECIDING RELATIONSHIP TYPES ({len(decisive)})")
    print("  every one of these changes who can read what when it is written\n")
    for rel in sorted(decisive):
        marks = []
        if rel in s["enabling"]:
            marks.append("enables")
        if rel in s["disabling"]:
            marks.append("DISABLES")
        print(f"    {rel:18} {'/'.join(marks)}")
        for why in s["enabling"].get(rel, []) + s["disabling"].get(rel, []):
            print(f"        {why}")
    print()

    if present:
        data_only = sorted(present - decisive)
        print(f"  DATA-ONLY RELATIONSHIP TYPES ({len(data_only)})")
        print("  present in the graph, named by no rule — normal data governance\n")
        print("    " + (", ".join(data_only) if data_only else "(none)"))
        missing = sorted(decisive - present)
        if missing:
            print(f"\n  !! DECLARED BUT ABSENT FROM THE GRAPH ({len(missing)})")
            print("     a rule traverses these and no such edge exists, so that rule")
            print("     can never fire — it denies silently\n")
            print("     " + ", ".join(missing))
        print()

    print("  DECIDING PROPERTIES")
    print(f"    access-control list      {s['acl_property']}")
    if s["row_props"]:
        print(f"    conditions on the row    {', '.join(s['row_props'])}")
    if s["edge_props"]:
        print(f"    on a traversed edge      {', '.join(s['edge_props'])}  (validity windows live here)")
    if s["caller_props"]:
        print(f"    attributes of the caller {', '.join(s['caller_props'])}")
    if s["boundary_properties"]:
        print(f"    identity/data boundary   "
              + ", ".join(f"{k}.{v}" for k, v in s["boundary_properties"].items()))
    print()

    # Tool surface: which questions can be asked at all (layer 4).
    specs = load_tool_specs(config.tools_dir)
    print(f"  QUESTIONS THAT MAY BE ASKED ({len(specs)})")
    for spec in specs:
        print(f"    {spec.name}")

    if args.graph:
        print("\n```mermaid\nflowchart LR")
        for rel in sorted(set(s["enabling"]) - set(s["disabling"])):
            print(f"  E{abs(hash(rel))%9999}[\"{rel}\"]:::enable")
        for rel in sorted(s["disabling"]):
            print(f"  D{abs(hash(rel))%9999}[\"{rel}\"]:::deny")
        print("  classDef enable fill:#e6f4ea,stroke:#137333;")
        print("  classDef deny fill:#fce8e6,stroke:#c5221f;")
        print("```")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
