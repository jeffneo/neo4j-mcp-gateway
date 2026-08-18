#!/usr/bin/env python
"""What does separating identity from the data it protects cost at scale?

Anchoring is the single largest performance lever in this engine (measured at
~20x), and a separated identity source cannot express an anchor today because it
binds no caller node. The obvious conclusion is that separation costs you that
20x. This script exists to check whether that conclusion is actually true, and it
turns out to need qualifying: the anchor can be SPLIT exactly like a grant, at a
proxy node, so the loss is an engine gap rather than a property of separation.

Six compositions, same dataset, same answer from every one:

    co-located    scan + ACL          the pre-anchoring baseline
    co-located    scan + path         path grant, unanchored
    co-located    anchored + path     what the co-located engine emits today
    split         scan + split-path   what a separated source emits today
    split         anchored-split      the anchor re-rooted at a proxy node
    split         anchored + ACL      anchoring's benefit without path grants

Run against a scale dataset (scripts/generate_scale_data.py), which models the
shape that makes this measurable: a large population where any one caller is
entitled to a small slice.

    uv run python scripts/generate_scale_data.py --database benchdb --wipe
    uv run python scripts/bench_separation.py --database benchdb

The split variants are measured on the SAME database as the co-located ones, with
the caller's principals supplied as a parameter instead of resolved in-statement.
That isolates the composition being measured from network and deployment
differences — the point here is the shape of the query, not where it is hosted.
Cross-database round trips are measured separately by scripts/bench_mediation.py.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import neo4j

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.config import Config  # noqa: E402

RUNS = 15
WARMUP = 3


def _time(session, query: str, params: dict) -> tuple[float, int]:
    start = time.perf_counter()
    result = session.run(query, params)
    rows = sum(1 for _ in result)
    return (time.perf_counter() - start) * 1000.0, rows


def _hits(session, query: str, params: dict) -> int:
    result = session.run("PROFILE " + query, params)
    result.consume()
    plan = result.consume().profile if hasattr(result, "consume") else None
    return plan.get("dbHits", 0) if plan else 0


def _profile_hits(session, query: str, params: dict) -> int:
    summary = session.run("PROFILE " + query, params).consume()
    total, stack = 0, [summary.profile]
    while stack:
        node = stack.pop()
        if not node:
            continue
        total += node.get("dbHits", 0)
        stack.extend(node.get("children", []))
    return total


# --------------------------------------------------------------------------- #
# The six compositions
# --------------------------------------------------------------------------- #
# Every variant returns the identical answer. The prelude is factored out: these
# measure the FILTER and the MATCH, which is where the topology shows up.

CO_PRELUDE = """
CALL {
  MATCH (u:User {email: $principal})
  OPTIONAL MATCH (u)-[:MEMBER_OF*1..]->(g:AdGroup)
  WITH u, collect(DISTINCT g.name) AS gn
  RETURN u AS caller,
         {principalId: u.email,
          authzPrincipals: [x IN gn WHERE x IS NOT NULL] + [u.email, 'everyone']} AS authz
}
"""

# A separated source has already resolved the caller elsewhere, so authz arrives
# as a parameter and there is no caller node.
SPLIT_PRELUDE = "WITH $authz AS authz\n"

VARIANTS = {
    # ---- co-located ------------------------------------------------------- #
    "co: scan + ACL": CO_PRELUDE + """
MATCH (t:Trade)-[:FOR_CLIENT]->(c:Client)
WITH authz, t, c
WHERE any(p IN coalesce(t.`Permissions.Read`, []) WHERE p IN authz.authzPrincipals)
RETURN count(t) AS trades, count(DISTINCT c) AS clients
""",
    "co: scan + path": CO_PRELUDE + """
MATCH (t:Trade)-[:FOR_CLIENT]->(c:Client)
WITH authz, caller, t, c
WHERE EXISTS { MATCH (caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(t) }
RETURN count(t) AS trades, count(DISTINCT c) AS clients
""",
    "co: anchored + path": CO_PRELUDE + """
MATCH (caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(c:Client)
WITH DISTINCT authz, caller, c
CALL { WITH c MATCH (t:Trade)-[:FOR_CLIENT]->(c) RETURN t }
WITH authz, caller, t, c
WHERE EXISTS { MATCH (caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(t) }
RETURN count(t) AS trades, count(DISTINCT c) AS clients
""",
    # ---- separated identity ----------------------------------------------- #
    # The grant is cut at the AdGroup proxy: same traversal, re-rooted at a value.
    "split: scan + path": SPLIT_PRELUDE + """
MATCH (t:Trade)-[:FOR_CLIENT]->(c:Client)
WITH authz, t, c
WHERE EXISTS { MATCH (cut:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(t)
               WHERE cut.name IN authz.authzPrincipals }
RETURN count(t) AS trades, count(DISTINCT c) AS clients
""",
    # The anchor cut the same way. This is the variant the engine cannot emit
    # today, and the reason the "separation costs 20x" claim needs checking.
    "split: anchored + path": SPLIT_PRELUDE + """
MATCH (cut:AdGroup)<-[:COVERED_BY]-(c:Client)
WHERE cut.name IN authz.authzPrincipals
WITH DISTINCT authz, c
CALL { WITH c MATCH (t:Trade)-[:FOR_CLIENT]->(c) RETURN t }
WITH authz, t, c
WHERE EXISTS { MATCH (cut2:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(t)
               WHERE cut2.name IN authz.authzPrincipals }
RETURN count(t) AS trades, count(DISTINCT c) AS clients
""",
    "split: anchored + ACL": SPLIT_PRELUDE + """
MATCH (cut:AdGroup)<-[:COVERED_BY]-(c:Client)
WHERE cut.name IN authz.authzPrincipals
WITH DISTINCT authz, c
CALL { WITH c MATCH (t:Trade)-[:FOR_CLIENT]->(c) RETURN t }
WITH authz, t, c
WHERE any(p IN coalesce(t.`Permissions.Read`, []) WHERE p IN authz.authzPrincipals)
RETURN count(t) AS trades, count(DISTINCT c) AS clients
""",
}


def resolve_authz(session, principal: str) -> dict:
    row = session.run(
        """
        MATCH (u:User {email: $principal})
        OPTIONAL MATCH (u)-[:MEMBER_OF*1..]->(g:AdGroup)
        WITH u, collect(DISTINCT g.name) AS gn
        RETURN {principalId: u.email,
                authzPrincipals: [x IN gn WHERE x IS NOT NULL] + [u.email, 'everyone']} AS authz
        """, {"principal": principal}).single()
    if not row:
        raise SystemExit(f"no :User with email {principal!r} in this database")
    return row["authz"]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", help="database holding the scale dataset")
    ap.add_argument("--principal", help="run as this principal")
    ap.add_argument("--runs", type=int, default=RUNS)
    ap.add_argument("--warmup", type=int, default=WARMUP)
    args = ap.parse_args(argv)

    config = Config.from_env()
    database = args.database or config.neo4j_database
    driver = neo4j.GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password),
        notifications_min_severity="OFF")

    with driver.session(database=database) as session:
        principal = args.principal
        if not principal:
            row = session.run(
                "MATCH (u:User) WHERE u.email IS NOT NULL RETURN u.email AS e LIMIT 1").single()
            if not row:
                raise SystemExit("no :User nodes found — run generate_scale_data.py first")
            principal = row["e"]

        counts = session.run(
            "MATCH (t:Trade) WITH count(t) AS trades "
            "MATCH (c:Client) RETURN trades, count(c) AS clients").single()
        authz = resolve_authz(session, principal)
        params = {"principal": principal, "authz": authz}

        print(f"database: {database}   {counts['trades']:,} trades / "
              f"{counts['clients']:,} clients")
        print(f"caller:   {principal}   ({len(authz['authzPrincipals'])} principals)")

        # One global pre-warm so the JIT and page cache do not favour whichever
        # variant happens to run first.
        for _ in range(args.warmup):
            for q in VARIANTS.values():
                _time(session, q, params)

        samples: dict[str, list[float]] = {k: [] for k in VARIANTS}
        answers: dict[str, tuple] = {}
        for _ in range(args.runs):
            for name, q in VARIANTS.items():          # interleaved
                elapsed, _ = _time(session, q, params)
                samples[name].append(elapsed)
                rec = session.run(q, params).single()
                answers[name] = (rec["trades"], rec["clients"])

        hits = {name: _profile_hits(session, q, params) for name, q in VARIANTS.items()}

        distinct = set(answers.values())
        print(f"\nanswer:   {answers['co: scan + ACL']} "
              f"({'IDENTICAL across all variants' if len(distinct) == 1 else '!! DIVERGED: ' + str(distinct)})")

        base = statistics.median(samples["co: scan + ACL"])
        print(f"\n  {'composition':26} {'p50 ms':>9} {'p95 ms':>9} {'db hits':>12} {'vs scan+ACL':>12}")
        for name in VARIANTS:
            ordered = sorted(samples[name])
            p50 = statistics.median(ordered)
            p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
            ratio = f"{base / p50:.1f}x faster" if p50 < base else f"{p50 / base:.1f}x slower"
            print(f"  {name:26} {p50:9.2f} {p95:9.2f} {hits[name]:12,} {ratio:>12}")

        if len(distinct) != 1:
            print("\n!! variants disagree — the comparison is meaningless until they match")
            return 1
    driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
