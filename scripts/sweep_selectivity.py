#!/usr/bin/env python
"""How does entitlement filtering cost vary with how much the caller can see?

Anchoring a query on what the caller is entitled to — rather than scanning
everything and discarding — wins when the caller sees a small slice of a large
population. It should win less as that slice grows, and at 100% it cannot help
at all. This sweep measures where the crossover actually is, so the speedup is
quoted with the selectivity it depends on rather than as a bare number.

    uv run python scripts/sweep_selectivity.py iam --database scaledb

For each ratio the script provisions a caller entitled to that fraction of
clients, then runs the same question two ways:

  scan      prelude -> match everything -> entitlement filter   (what ships today)
  anchored  prelude -> traverse to the caller's clients -> match -> filter
            (the proposed form; the filter is retained, so a wrong anchor would
            cost speed but never correctness)

Requires a dataset from scripts/generate_scale_data.py.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import neo4j

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.config import Config, active_bundle_names

SWEEP_TAG = "iam-sweep"

# Both variants share this prelude. It returns the caller node as well as the
# principal set, because an anchored query has to traverse from the caller.
PRELUDE = """
CALL {
  MATCH (u:User|Principal)
  WHERE any(k IN ['email','username','name']
            WHERE u[k] IS NOT NULL AND toLower(toString(u[k])) = toLower($p))
  OPTIONAL MATCH (u)-[:MEMBER_OF*1..]->(g)
  WITH u, collect(DISTINCT g.name) AS gp
  RETURN u AS caller, {authzPrincipals: gp + ['everyone', $p]} AS authz
}"""

FILTER_AND_RETURN = """
WITH authz, caller, t, cl
WHERE any(x IN coalesce(t.`Permissions.Read`, []) WHERE x IN authz.authzPrincipals)
RETURN count(t) AS trades, count(DISTINCT cl) AS clients"""

SCAN = PRELUDE + """
CALL { WITH authz MATCH (t:Trade)-[:FOR_CLIENT]->(cl:Client) RETURN t, cl }""" + FILTER_AND_RETURN

ANCHORED = PRELUDE + """
MATCH (caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(cl:Client)
CALL { WITH cl MATCH (t:Trade)-[:FOR_CLIENT]->(cl) RETURN t }""" + FILTER_AND_RETURN


def _db_hits(profile: dict | None) -> int:
    if not profile:
        return 0
    total = int(profile.get("dbHits", 0) or 0)
    for child in profile.get("children") or []:
        total += _db_hits(child)
    return total


def _measure(session, query: str, principal: str, runs: int) -> tuple[float, int, dict]:
    for _ in range(3):
        session.run(query, {"p": principal}).consume()
    samples, rows = [], []
    for _ in range(runs):
        start = time.perf_counter()
        rows = list(session.run(query, {"p": principal}))
        samples.append((time.perf_counter() - start) * 1000)
    profile = session.run("PROFILE " + query, {"p": principal}).consume().profile
    return statistics.median(samples), _db_hits(profile), (dict(rows[0]) if rows else {})


def _provision(session, teams_held: int, total_teams: int) -> str:
    """Create (or refresh) a caller entitled to `teams_held` coverage teams."""
    email = f"sweep-{teams_held}@bank.com"
    session.run("MATCH (u:User {email: $e}) DETACH DELETE u", {"e": email}).consume()
    session.run("""
        CREATE (u:User {email: $e, name: 'Sweep ' + $e, role: 'Sales', source: $tag})
        WITH u
        UNWIND range(0, $held - 1) AS k
        MATCH (g:AdGroup {name: 'coverage-' + toString(k % $total)})
        MERGE (u)-[:MEMBER_OF]->(g)
    """, {"e": email, "held": teams_held, "total": total_teams, "tag": SWEEP_TAG}).consume()
    return email


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Sweep entitlement selectivity.")
    ap.add_argument("bundle", nargs="?", help="bundle whose connection to use")
    ap.add_argument("--database", help="override the target database")
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--ratios", default="0.5,1,5,10,25,50,100",
                    help="percentages of the client population the caller may see")
    ap.add_argument("--keep", action="store_true", help="leave the sweep users in place")
    args = ap.parse_args(argv)

    config = Config.from_env(active_bundle=args.bundle or active_bundle_names()[0])
    database = args.database or config.neo4j_database
    driver = neo4j.GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password),
        notifications_min_severity="OFF")

    try:
        with driver.session(database=database) as session:
            totals = session.run("""
                MATCH (c:Client) WITH count(c) AS clients
                MATCH (g:AdGroup) WITH clients, count(g) AS teams
                MATCH (t:Trade) RETURN clients, teams, count(t) AS trades
            """).single()
            if not totals or not totals["clients"]:
                print("no dataset found — run scripts/generate_scale_data.py first", file=sys.stderr)
                return 1
            clients, teams, trades = totals["clients"], totals["teams"], totals["trades"]
            print(f"dataset: {clients:,} clients, {trades:,} trades, {teams} coverage teams "
                  f"(db: {database})\n")

            header = (f"  {'visible':>9} {'clients':>9} | {'scan ms':>9} {'anchored':>9} "
                      f"{'speedup':>8} | {'scan hits':>11} {'anch hits':>10} {'ratio':>7}")
            print(header)
            print("  " + "-" * (len(header) - 2))

            ratios = [float(r) for r in args.ratios.split(",")]

            # Pre-warm across EVERY ratio before measuring any of them. Without
            # this the JVM warms up as the sweep progresses and later (larger)
            # ratios look artificially fast, which inverts the trend.
            provisioned = {}
            for ratio in ratios:
                held = max(1, round(teams * ratio / 100))
                email = _provision(session, held, teams)
                provisioned[ratio] = email
                for query in (SCAN, ANCHORED):
                    for _ in range(2):
                        session.run(query, {"p": email}).consume()

            rows = []
            for ratio in ratios:
                email = provisioned[ratio]
                scan_ms, scan_hits, scan_res = _measure(session, SCAN, email, args.runs)
                anch_ms, anch_hits, anch_res = _measure(session, ANCHORED, email, args.runs)

                if scan_res != anch_res:
                    print(f"  !! MISMATCH at {ratio}%: scan={scan_res} anchored={anch_res}")

                visible = scan_res.get("clients", 0)
                speed = scan_ms / anch_ms if anch_ms else 0
                hit_ratio = scan_hits / anch_hits if anch_hits else 0
                rows.append((ratio, visible, speed))
                print(f"  {ratio:8.1f}% {visible:9,} | {scan_ms:9.1f} {anch_ms:9.1f} "
                      f"{speed:7.1f}x | {scan_hits:11,} {anch_hits:10,} {hit_ratio:6.1f}x")

            best = max(rows, key=lambda r: r[2])
            crossover = next((r for r in rows if r[2] < 1.0), None)
            print(f"\n  best {best[2]:.0f}x at {best[0]}% visibility ({best[1]:,} clients)")
            if crossover:
                print(f"  anchoring stops paying at {crossover[0]}% visibility "
                      f"({crossover[2]:.2f}x — slower than scanning)")
            else:
                print("  anchoring still ahead at every ratio measured")
            print("\n  Anchoring's advantage is a function of selectivity: it avoids reading what\n"
                  "  the caller cannot see, so it wins big on a narrow slice and converges on\n"
                  "  parity as visibility approaches the whole population. Quote the ratio with\n"
                  "  the speedup.")

            if not args.keep:
                session.run("MATCH (u:User {source: $tag}) DETACH DELETE u",
                            {"tag": SWEEP_TAG}).consume()
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
