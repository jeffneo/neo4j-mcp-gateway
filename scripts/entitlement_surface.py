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
denial, disables; and the same type can do both — IN_UNIT reaches a desk in a
denial here and a booking desk in a grant. So the graph cannot be coloured red and
green: you have to name the rule. That is why "authorised iff there is an enabling
path and no disabling path" operationalises as "matches a grant pattern and no
denial pattern".

WHY NOT JUST BUILD PARALLEL RAILS?
----------------------------------
The obvious response to the dual-purpose category is to stop traversing business
facts: derive a private ENTITLES_* edge beside each one and let entitlement run on
exclusive rails. It is the right instinct and it is worth being precise about when
it pays, because most of the time it does not.

A rail is a SECOND RECORDING of a fact, and two recordings can disagree. So:

  A PURE COPY IS STRICTLY WORSE. If the rail is derived mechanically from the
  business edge, then a CRM edit still moves access — one derivation step later.
  The risk has been renamed, not reduced, and a new failure mode has been added:
  the copy going stale, which nothing in the query can detect. `AUTHORED_BY` is
  this case. "The author may read what they authored" IS the business fact.

  A RAIL EARNS ITS KEEP WHEN THE ENTITLEMENT IS NOT THE FACT. If only *primary*
  coverage entitles, or only coverage inside a window, or only desks above a
  threshold, then the rail encodes a policy decision that is nowhere in the
  business edge. But note that a `where:` predicate on the rule expresses the same
  restriction with no second recording at all — which is why this bundle uses
  `where` for windows and notional limits rather than minting rails for them.

  THE ONE THING A RAIL BUYS THAT A PREDICATE CANNOT: WRITE-PATH GOVERNANCE. A
  distinct relationship type is a distinct privilege target, so the business feed's
  database role can be denied the ability to write it. A `where` clause cannot do
  that — it constrains reads, and the feed still owns the edge. That is a real,
  enforceable control, and `--write-guard` below generates it.

So the rule of thumb this report is built around: promote to a policy rail when
you need to REVOKE SOMEONE'S ABILITY TO WRITE IT, not when you want the diagram
tidier. And never derive a DENIAL onto a rail — a grant rail that misses an edge
under-grants and someone complains, while a denial rail that misses an edge fails
OPEN and no per-caller test notices.

The POLICY / DUAL-PURPOSE split is therefore derived rather than declared: a
deciding relationship type is DUAL-PURPOSE exactly when some tool's own query
traverses it as business data, and POLICY when no tool touches it. That makes the
list a fact about the deployment instead of an intention about it.
"""

from __future__ import annotations

import argparse
import re
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


# Property references in a `where` clause, attributed to what they hang off. The
# variable decides how the reference is governed, so the three buckets are not
# cosmetic: a row property is written by the record's own feed, an edge property by
# whoever owns that relationship, and a caller attribute by the identity feed.
_ROW_PROP = re.compile(r"\bresource\.(?:`([^`]+)`|([A-Za-z_]\w*))")
_CALLER_ATTR = re.compile(r"\bauthz\.attrs\.([A-Za-z_]\w*)")
_ANY_PROP = re.compile(r"\b(\w+)\.(?:`([^`]+)`|([A-Za-z_]\w*))")


def _named(matches) -> set[str]:
    return {a or b for a, b in matches}


def _props(expr: str) -> tuple[set[str], set[str], set[str]]:
    """``(row, edge, caller)`` property names referenced by one predicate."""
    expr = expr or ""
    row = _named(_ROW_PROP.findall(expr))
    caller = set(_CALLER_ATTR.findall(expr))
    edge = set()
    for var, quoted, plain in _ANY_PROP.findall(expr):
        name = quoted or plain
        if var in ("resource", "authz", "attrs", "caller") or name in ("attrs",):
            continue
        edge.add(name)
    return row, edge, caller


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
            row, edge, caller = _props(rule.where)
            row_props |= row
            edge_props |= edge
            caller_props |= caller

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


def queried_types(specs) -> dict[str, list[str]]:
    """Relationship types the TOOLS traverse as business data, per tool.

    Read coupling, not write ownership — the two are different facts and conflating
    them is a mistake worth naming, because it produces a plausible-looking
    privilege script that breaks ingestion. A tool READING an edge tells you the
    edge has a second consumer; it says nothing about who writes it.

    Read off the authored `match`/`return` text — the tool's own Cypher, not the
    composed query, so the prelude and filter do not contaminate the answer.
    """
    out: dict[str, list[str]] = {}
    for spec in specs:
        text = " ".join([spec.match_clause or "", spec.return_clause or "",
                         spec.cypher or "", (spec.anchor[1] if spec.anchor else "")])
        for rel in _rel_types(text):
            out.setdefault(rel, []).append(spec.name)
    return out


# Neo4j Enterprise fine-grained privileges. An AUTHORED deciding edge — one no feed
# writes — is a distinct privilege target, and this is the one control a `where:`
# predicate cannot give you: a predicate constrains reads while the feed still owns
# the edge. Feed-written edges are excluded, because denying those breaks the
# pipeline that creates them.
_WRITE_GUARD = """// Write-path governance for bundle '{bundle}'.
//
// These are the deciding relationship types that NO declared feed writes
// (security.ingested_rels). Nothing automated needs to create them, so a role can
// be denied write on them outright — the strongest control available over an
// entitlement model, and the only one that does not depend on testing.
//
// DENY beats GRANT in Neo4j, so these are absolute for the named role, including
// over any GRANT it holds today. Review before running.
{statements}
// DELIBERATELY EXCLUDED — these decide access AND are written by a feed:
{excluded}//
// Those cannot be locked down without breaking ingestion, and a parallel
// entitlement-only copy of them would not help: a derived edge moves the same
// upstream edit one step downstream and adds a staleness failure that nothing in
// the query can detect. The controls for them are the list above, conformance in
// CI after every load, and keeping barriers off them entirely.
"""


def write_guard(bundle: str, authored: list[str], feed_written: dict[str, str],
                role: str, graph: str) -> str:
    lines = []
    for rel in authored:
        lines.append(f"DENY CREATE ON GRAPH {graph} RELATIONSHIP {rel} TO {role};")
        lines.append(f"DENY DELETE ON GRAPH {graph} RELATIONSHIP {rel} TO {role};")
        lines.append(f"DENY SET PROPERTY {{*}} ON GRAPH {graph} RELATIONSHIP {rel} TO {role};")
    excluded = "".join(f"//   {rel:20} written by {owner}\n"
                       for rel, owner in sorted(feed_written.items()))
    return _WRITE_GUARD.format(
        bundle=bundle,
        statements="\n".join(lines) or "// (every deciding edge is feed-written — "
                                       "nothing can be denied)",
        excluded=excluded or "//   (none)\n")


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
    ap.add_argument("--write-guard", metavar="ROLE",
                    help="emit DENY statements withholding write on the AUTHORED "
                         "deciding edges from ROLE")
    args = ap.parse_args(argv)

    config = Config.from_env(active_bundle=args.bundle or active_bundle_names()[0])
    policy = config.security
    if not policy.mediated:
        print(f"bundle '{config.active_bundle}' is security.mode: {policy.mode} — "
              "no entitlement surface to compute")
        return 0

    s = surface(policy)
    specs = load_tool_specs(config.tools_dir)
    present = observed_types(config)
    queried = queried_types(specs)
    decisive = set(s["enabling"]) | set(s["disabling"])
    feeds = policy.ingested_rels
    feed_written = {rel: feeds[rel] for rel in sorted(decisive) if rel in feeds}
    authored = sorted(decisive - set(feed_written))

    print(f"entitlement surface — bundle '{config.active_bundle}'\n")

    print(f"  DECIDING RELATIONSHIP TYPES ({len(decisive)})")
    print("  every one of these changes who can read what when it is written\n")
    for rel in sorted(decisive):
        marks = []
        if rel in s["enabling"]:
            marks.append("enables")
        if rel in s["disabling"]:
            marks.append("DISABLES")
        owner = feed_written.get(rel)
        kind = f"feed: {owner}" if owner else "AUTHORED"
        print(f"    {rel:20} {'/'.join(marks):18} [{kind}]")
        for why in s["enabling"].get(rel, []) + s["disabling"].get(rel, []):
            print(f"        {why}")
        if rel in queried:
            print(f"        also read as business data by: {', '.join(sorted(set(queried[rel])))}")
    print()

    # WRITE OWNERSHIP is what decides how an edge can be governed, and it is the one
    # part of the surface that config must declare rather than the rules imply.
    print(f"  AUTHORED DECIDING EDGES ({len(authored)} of {len(decisive)})")
    print("  no declared feed writes these, so a database role can be denied write")
    print("  on them outright — the only control here that does not rely on testing.")
    print("      scripts/entitlement_surface.py --write-guard <role>\n")
    print("    " + (", ".join(authored) or "(none)"))
    print()
    print(f"  FEED-WRITTEN DECIDING EDGES ({len(feed_written)} of {len(decisive)})")
    print("  a routine upstream edit moves access. The privilege cannot be taken")
    print("  away without breaking ingestion, and a parallel entitlement-only copy")
    print("  would only move the same edit one step downstream while adding a")
    print("  staleness failure of its own. Control: this list, plus conformance in")
    print("  CI after every load.\n")
    for owner in sorted(set(feed_written.values())):
        rels = [r for r, o in feed_written.items() if o == owner]
        print(f"    {owner:22} {', '.join(sorted(rels))}")
    if not feed_written:
        print("    (none)")
    print()

    # THE FAIL-OPEN CASE, and the reason write ownership is worth declaring at all.
    at_risk = sorted(set(s["disabling"]) & set(feed_written))
    if at_risk:
        print(f"  !! BARRIERS THAT DEPEND ON A FEED-WRITTEN EDGE ({len(at_risk)})")
        print("     A denial traverses these, and an upstream feed writes them. A")
        print("     missing GRANT edge under-grants and someone complains; a missing")
        print("     DENIAL edge lifts a barrier and FAILS OPEN, because nothing is")
        print("     absent from anyone's results. No per-caller test can catch it —")
        print("     only an invariant asserting the edge is still there.\n")
        for rel in at_risk:
            print(f"     {rel:20} written by {feed_written[rel]}")
            for why in s["disabling"].get(rel, []):
                print(f"         {why}")
        print()
    else:
        print("  BARRIERS REST ONLY ON AUTHORED EDGES — no denial depends on a feed.\n")

    # Declared but never traversed by a rule: not a risk, just noise to be sure of.
    stale = sorted(set(feeds) - decisive)
    if stale:
        print(f"  DECLARED AS INGESTED BUT NOT DECIDING ({len(stale)})")
        print("  listed in security.ingested_rels and named by no rule. Harmless, but")
        print("  it means the declaration is broader than the surface.\n")
        print("    " + ", ".join(stale))
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
            print("     can never fire. In a GRANT that under-grants and a must_see case")
            print("     catches it; in a DENIAL the barrier stops applying and FAILS OPEN,")
            print("     which no per-caller test notices\n")
            for rel in missing:
                where = "denial — FAILS OPEN" if rel in s["disabling"] else "grant — under-grants"
                print(f"     {rel:20} {where}")
        print()

    print("  DECIDING PROPERTIES")
    print(f"    access-control list      {s['acl_property']}")
    if s["row_props"]:
        print(f"    conditions on the row    {', '.join(s['row_props'])}")
    if s["edge_props"]:
        print(f"    on a traversed edge      {', '.join(s['edge_props'])}  (validity windows live here)")
    if s["caller_props"]:
        print(f"    attributes of the caller {', '.join(s['caller_props'])}"
              "  (thresholds live here — an ordering, not a principal)")
    if s["boundary_properties"]:
        print(f"    identity/data boundary   "
              + ", ".join(f"{k}.{v}" for k, v in s["boundary_properties"].items()))
    print()

    # Tool surface: which questions can be asked at all (layer 4).
    print(f"  QUESTIONS THAT MAY BE ASKED ({len(specs)})")
    for spec in specs:
        print(f"    {spec.name}")

    if args.write_guard:
        print()
        print(write_guard(config.active_bundle, authored, feed_written,
                          args.write_guard, config.neo4j_database or "neo4j"))

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
