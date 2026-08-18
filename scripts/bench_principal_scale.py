#!/usr/bin/env python
"""How does the entitlement filter behave when a caller holds MANY principals?

A real enterprise directory routinely puts one person in hundreds or thousands of
groups. Two things in this engine scale with that number, and they scale
differently:

    property cut   c.coverageTeam IN $principals          one value vs the list
    ACL model      any(p IN row.acl WHERE p IN $principals)   list vs list

The ACL test is a nested scan, so its cost is roughly
|rows examined| x |acl| x |principals|. The property cut compares a single value,
so it is |rows| x |principals|. Both are linear in the principal count, which is
the thing to check before promising a deployment where callers hold thousands.

A third variant is measured because it is the obvious fix if the list scan bites:
pass the principals as a MAP and do a key lookup instead of a scan.

    map lookup     $principalMap[c.coverageTeam] IS NOT NULL

VISIBILITY IS HELD CONSTANT. The caller's real entitlements do not change; the
principal list is padded with names that match nothing. That isolates the cost of
carrying a long list from the cost of being entitled to more rows — otherwise the
two move together and neither is measurable.

    uv run python scripts/generate_scale_data.py --database benchdb --wipe
    uv run python scripts/bench_principal_scale.py --database benchdb
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

SIZES = [1, 10, 100, 1_000, 5_000]

VARIANTS = {
    "ACL (list vs list)": """
MATCH (t:Trade)-[:FOR_CLIENT]->(c:Client)
WHERE any(p IN coalesce(t.`Permissions.Read`, []) WHERE p IN $principals)
RETURN count(t) AS trades
""",
    "property cut (value vs list)": """
MATCH (t:Trade)-[:FOR_CLIENT]->(c:Client)
WHERE c.coverageTeam IN $principals
RETURN count(t) AS trades
""",
    "property cut (map lookup)": """
MATCH (t:Trade)-[:FOR_CLIENT]->(c:Client)
WHERE $principalMap[c.coverageTeam] IS NOT NULL
RETURN count(t) AS trades
""",
    "anchored + property cut": """
MATCH (c:Client) WHERE c.coverageTeam IN $principals
CALL { WITH c MATCH (t:Trade)-[:FOR_CLIENT]->(c) RETURN t }
RETURN count(t) AS trades
""",
}


def _time(session, query: str, params: dict) -> tuple[float, int]:
    start = time.perf_counter()
    record = session.run(query, params).single()
    return (time.perf_counter() - start) * 1000.0, record["trades"]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database")
    ap.add_argument("--principal")
    ap.add_argument("--runs", type=int, default=15)
    ap.add_argument("--warmup", type=int, default=4)
    args = ap.parse_args(argv)

    config = Config.from_env()
    database = args.database or config.neo4j_database
    driver = neo4j.GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password),
        notifications_min_severity="OFF")

    with driver.session(database=database) as session:
        principal = args.principal or session.run(
            "MATCH (u:User) WHERE u.email IS NOT NULL RETURN u.email AS e LIMIT 1").single()["e"]
        real = session.run(
            "MATCH (u:User {email:$p})-[:MEMBER_OF*1..]->(g:AdGroup) "
            "RETURN collect(DISTINCT g.name) AS gs", {"p": principal}).single()["gs"]
        counts = session.run(
            "MATCH (t:Trade) WITH count(t) AS trades MATCH (c:Client) "
            "RETURN trades, count(c) AS clients").single()

        print(f"database: {database}   {counts['trades']:,} trades / {counts['clients']:,} clients")
        print(f"caller:   {principal}   genuinely holds {len(real)} group(s)")
        print("\nvisibility is CONSTANT; only the length of the principal list changes.\n")

        header = f"  {'principals':>11}"
        for name in VARIANTS:
            header += f" {name:>30}"
        print(header)

        baseline: dict[str, float] = {}
        for size in SIZES:
            # Decoys FIRST, real groups last: `p IN list` short-circuits on a
            # match, so putting the caller's real groups at the front would
            # measure a best case no directory actually produces. This is the
            # honest worst case and it is deterministic.
            decoys = [f"__decoy-group-{i}" for i in range(max(0, size - len(real)))]
            principals = decoys + list(real)
            params = {
                "principals": principals,
                "principalMap": {p: True for p in principals},
            }
            for _ in range(args.warmup):
                for q in VARIANTS.values():
                    _time(session, q, params)

            row = f"  {len(principals):>11,}"
            rows_seen = set()
            for name, q in VARIANTS.items():
                samples = []
                for _ in range(args.runs):
                    elapsed, trades = _time(session, q, params)
                    samples.append(elapsed)
                    rows_seen.add((name, trades))
                p50 = statistics.median(samples)
                baseline.setdefault(name, p50)
                row += f" {p50:>21.1f} ms {p50 / baseline[name]:>5.1f}x"
            print(row)
            answers = {t for _, t in rows_seen}
            if len(answers) != 1:
                print(f"    !! variants disagree on row count: {answers}")

        print("\n  the multiplier is growth against that variant's own 1-principal cost,")
        print("  so each column shows how ITS OWN cost degrades as the caller holds more.")
    driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
