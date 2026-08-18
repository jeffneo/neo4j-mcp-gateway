#!/usr/bin/env python
"""Generate an entitlement dataset at realistic scale.

Defaults match the shape described by the prospect: ~100,000 institutional
clients, with any one salesperson entitled to roughly 1,000 of them. That
selectivity — a small slice of a large population — is what makes the
entitlement filter's cost meaningful; the hand-built demo dataset is far too
small to say anything about performance.

    uv run python scripts/generate_scale_data.py --wipe
    uv run python scripts/generate_scale_data.py --clients 250000 --teams 500

Shape (mirrors the IAM bundle's model, so mediated tools work unchanged):

    (:User)-[:MEMBER_OF]->(:AdGroup)          salespeople in coverage teams
    (:Client {coverageTeam})                  each client covered by one team
    (:Trade)-[:FOR_CLIENT]->(:Client)         business records carrying an ACL
    Trade.`Permissions.Read` = [coverage team, desk, compliance, booker]

Everything is tagged `source:'iam-scale'` so it can be removed independently of
the narrative demo data. Generating into a separate database keeps benchmark
numbers clean — set NEO4J_DATABASE, or pass --database.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import neo4j

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.config import Config, active_bundle_names

BATCH = 10_000

CONSTRAINTS = [
    "CREATE CONSTRAINT scale_client_id IF NOT EXISTS FOR (c:Client) REQUIRE c.clientId IS UNIQUE",
    "CREATE CONSTRAINT scale_trade_id  IF NOT EXISTS FOR (t:Trade)  REQUIRE t.tradeId  IS UNIQUE",
    "CREATE INDEX scale_user_email     IF NOT EXISTS FOR (u:User)    ON (u.email)",
    "CREATE INDEX scale_group_name     IF NOT EXISTS FOR (g:AdGroup) ON (g.name)",
    "CREATE INDEX scale_client_team    IF NOT EXISTS FOR (c:Client)  ON (c.coverageTeam)",
]


def _run(session, query: str, params: dict | None = None, label: str = "") -> float:
    start = time.perf_counter()
    session.run(query, params or {}).consume()
    elapsed = time.perf_counter() - start
    if label:
        print(f"  {label:38} {elapsed:6.1f}s")
    return elapsed


def generate(session, clients: int, teams: int, salespeople: int,
             teams_per_sales: int, trades_per_client: int) -> None:
    print("creating constraints and indexes")
    for stmt in CONSTRAINTS:
        session.run(stmt).consume()

    print(f"\ngenerating: {clients:,} clients, {teams} coverage teams, "
          f"{salespeople:,} salespeople ({teams_per_sales} teams each), "
          f"{trades_per_client} trade(s) per client")

    _run(session, """
        UNWIND range(0, $teams - 1) AS i
        CREATE (:AdGroup {name: 'coverage-' + toString(i), kind: 'coverage', source: 'iam-scale'})
    """, {"teams": teams}, "coverage teams")

    # Each salesperson belongs to `teams_per_sales` teams, spread across the
    # population, so one user is entitled to roughly clients/teams*teams_per_sales.
    _run(session, f"""
        UNWIND range(0, $salespeople - 1) AS i
        CALL {{
          WITH i
          WITH i, [k IN range(0, $per - 1) | 'coverage-' + toString((i * $per + k) % $teams)] AS myTeams
          CREATE (u:User {{
            email: 'sales-' + toString(i) + '@bank.com',
            name: 'Sales User ' + toString(i),
            role: 'Sales',
            AdGroupList: ['everyone'] + myTeams,
            source: 'iam-scale'
          }})
          WITH u, myTeams
          UNWIND myTeams AS teamName
          MATCH (g:AdGroup {{name: teamName}})
          MERGE (u)-[:MEMBER_OF]->(g)
        }} IN TRANSACTIONS OF {BATCH} ROWS
    """, {"salespeople": salespeople, "teams": teams, "per": teams_per_sales}, "salespeople + memberships")

    _run(session, f"""
        UNWIND range(0, $clients - 1) AS i
        CALL {{
          WITH i
          CREATE (:Client {{
            clientId: 'CL-' + toString(i),
            name: 'Client ' + toString(i),
            coverageTeam: 'coverage-' + toString(i % $teams),
            source: 'iam-scale'
          }})
        }} IN TRANSACTIONS OF {BATCH} ROWS
    """, {"clients": clients, "teams": teams}, "clients")

    # Business records carrying the ACL. Entitlement mirrors the demo model:
    # the covering team, the owning desk, settlements, and supervision.
    _run(session, f"""
        UNWIND range(0, $clients - 1) AS i
        CALL {{
          WITH i
          MATCH (c:Client {{clientId: 'CL-' + toString(i)}})
          UNWIND range(0, $per - 1) AS k
          CREATE (t:Trade {{
            tradeId: 'TRD-' + toString(i) + '-' + toString(k),
            product: 'Product ' + toString(k),
            notional: toFloat(100000 + (i % 900) * 1000),
            currency: 'USD',
            source: 'iam-scale'
          }})
          SET t.`Permissions.Read` = [
            c.coverageTeam,
            'desk-' + toString(i % 8),
            'ops-settlements',
            'compliance-supervision'
          ]
          CREATE (t)-[:FOR_CLIENT]->(c)
        }} IN TRANSACTIONS OF {BATCH} ROWS
    """, {"clients": clients, "per": trades_per_client}, "trades + ACLs")


def report(session) -> None:
    print("\nresulting graph:")
    rows = session.run("""
        MATCH (n {source: 'iam-scale'})
        RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC
    """).data()
    for row in rows:
        print(f"  {row['label']:12} {row['count']:>10,}")

    sample = session.run("""
        MATCH (u:User {source: 'iam-scale'}) WITH u LIMIT 1
        MATCH (u)-[:MEMBER_OF]->(g:AdGroup)
        WITH u, collect(g.name) AS teams
        MATCH (c:Client) WHERE c.coverageTeam IN teams
        WITH u, teams, count(c) AS covered
        MATCH (t:Trade) WHERE any(p IN t.`Permissions.Read` WHERE p IN teams)
        RETURN u.email AS email, size(teams) AS teams, covered, count(t) AS entitledTrades
    """).single()
    if sample:
        print(f"\nselectivity check — {sample['email']} is in {sample['teams']} team(s):")
        print(f"  entitled to {sample['covered']:,} clients and {sample['entitledTrades']:,} trades")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Generate entitlement data at scale.")
    ap.add_argument("bundle", nargs="?", help="bundle whose connection to use (default: active)")
    ap.add_argument("--clients", type=int, default=100_000)
    ap.add_argument("--teams", type=int, default=200)
    ap.add_argument("--salespeople", type=int, default=500)
    ap.add_argument("--teams-per-sales", type=int, default=2)
    ap.add_argument("--trades-per-client", type=int, default=1)
    ap.add_argument("--database", help="override the target database")
    ap.add_argument("--wipe", action="store_true", help="remove existing source:'iam-scale' data first")
    args = ap.parse_args(argv)

    config = Config.from_env(active_bundle=args.bundle or active_bundle_names()[0])
    database = args.database or config.neo4j_database

    expected = args.clients // args.teams * args.teams_per_sales
    print(f"target: {config.neo4j_uri} db={database}")
    print(f"each salesperson will be entitled to ~{expected:,} clients\n")

    driver = neo4j.GraphDatabase.driver(
        config.neo4j_uri,
        auth=(config.neo4j_username, config.neo4j_password),
        notifications_min_severity="OFF",
    )
    try:
        with driver.session(database=database) as session:
            if args.wipe:
                print("wiping existing scale data")
                _run(session, f"""
                    MATCH (n {{source: 'iam-scale'}})
                    CALL {{ WITH n DETACH DELETE n }} IN TRANSACTIONS OF {BATCH} ROWS
                """, {}, "wipe")
            started = time.perf_counter()
            generate(session, args.clients, args.teams, args.salespeople,
                     args.teams_per_sales, args.trades_per_client)
            print(f"\ntotal {time.perf_counter() - started:.1f}s")
            report(session)
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
