#!/usr/bin/env python
"""Project the `coverage_teams` view into the entitlement graph.

    uv run python scripts/ingest_coverage_teams.py \
        bundles/asset_platform/data/views/coverage_teams.csv
    uv run python scripts/ingest_coverage_teams.py --dry-run <file>

Same contract as scripts/ingest_business_hierarchy.py: a mechanical projection,
no business logic, and the real view is swapped in by editing COLUMNS.

    team_id, employee_id     ->  (:Employee)-[:MEMBER_OF {role}]->(:CoverageTeam)
    team_id, account_id      ->  (:CoverageTeam)-[:COVERS {validFrom, validTo}]->(:ClientOrg)

WHY THE TEAM IS A NODE, rather than an (:Employee)-[:COVERS]->(:ClientOrg) edge
derived per row. Three reasons, and the second is the one that earns it:

1. It matches the source. The view is called coverage_TEAMS; the team is the thing
   the business names, staffs and audits. Flattening it away means the graph can no
   longer answer a question the business asks ("who is on this team?").

2. COVERAGE PARITY BECOMES STRUCTURAL. Two people on one team see the same book
   because they traverse the same node — not because two rows happened to agree.
   With the flattened edge, parity is a coincidence that a partial refresh can
   break silently; here it cannot be broken without deleting the team. There is a
   conformance case asserting exactly this, and note what it compares: a rank-4
   and a rank-2 on the same team, whose results must be identical.

3. It splits cheaply. A shared intermediate node is the natural cut point when
   identity is separated from data — the team name crosses the boundary as a value
   and the data-side suffix re-roots on it. A per-person edge has no such point.

THE WINDOW LIVES ON `COVERS`, not on membership, because coverage of an account
is what expires; a person's place on the team is current or absent. One row in the
sample extract is deliberately expired, so the window is load-bearing rather than
decorative.

DENORMALISATION. The view is a JOIN output, so the (team, account, window) fact
repeats once per member. That repetition is where an inconsistent view shows
itself, and this script REPORTS conflicting windows rather than picking one: two
rows disagreeing about when a team covers an account is a fact about the upstream
lineage that somebody needs to see.

FAILURE DIRECTION. COVERS and MEMBER_OF feed grants only, so a missing row
under-grants and a conformance case catches it. An account_name that matches no
:ClientOrg is a hard error here rather than a skipped row, because a silently
skipped coverage row is invisible in exactly the same way.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.config import Config, active_bundle_names  # noqa: E402
from gateway.yaml_tools import Neo4jExecutor  # noqa: E402

# The only thing that changes when the real view arrives.
COLUMNS = {
    "team_id": "team_id",
    "team_name": "team_name",
    "employee_id": "employee_id",
    "coverage_role": "coverage_role",
    "account_id": "account_id",
    "account_name": "account_name",
    "valid_from": "valid_from",
    "valid_to": "valid_to",
}

SOURCE = "ct-view"


def _read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit(f"{path}: no rows")
    missing = [want for want in COLUMNS.values() if want not in rows[0]]
    if missing:
        raise SystemExit(
            f"{path}: the extract is missing column(s) {missing}.\n"
            f"  Present: {sorted(rows[0])}\n"
            f"  Fix by remapping COLUMNS at the top of this script.")
    return rows


def _get(row: dict, key: str) -> str:
    return (row.get(COLUMNS[key]) or "").strip()


def project(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    teams: dict[str, dict] = {}
    members: dict[tuple[str, str], dict] = {}
    covers: dict[tuple[str, str], dict] = {}
    problems: list[str] = []

    for n, row in enumerate(rows, start=2):
        team_id = _get(row, "team_id")
        if not team_id:
            problems.append(f"line {n}: no team_id")
            continue
        record = {"teamId": team_id, "name": _get(row, "team_name")}
        prior = teams.setdefault(team_id, record)
        if prior != record:
            problems.append(f"line {n}: team {team_id!r} has two names "
                            f"({prior['name']!r}, {record['name']!r})")

        emp = _get(row, "employee_id")
        if emp:
            key = (team_id, emp)
            role = _get(row, "coverage_role")
            prior_m = members.setdefault(key, {"teamId": team_id, "employeeId": emp,
                                               "role": role})
            if prior_m["role"] != role:
                problems.append(f"line {n}: {emp!r} is on {team_id!r} as both "
                                f"{prior_m['role']!r} and {role!r}")

        account = _get(row, "account_id")
        if not account:
            continue
        cover = {"teamId": team_id, "accountId": account,
                 "accountName": _get(row, "account_name"),
                 "validFrom": _get(row, "valid_from") or None,
                 "validTo": _get(row, "valid_to") or None}
        prior_c = covers.setdefault((team_id, account), cover)
        if prior_c != cover:
            # The denormalised view disagreeing with itself. Do NOT pick a winner:
            # a validity window decides whether an entitlement is in force.
            problems.append(
                f"line {n}: {team_id!r} covers {account!r} with two different records — "
                f"{prior_c} then {cover}. A window decides whether coverage is in force, "
                "so this must be resolved upstream rather than here")
    return (list(teams.values()), list(members.values()), list(covers.values()), problems)


_CONSTRAINTS = [
    "CREATE CONSTRAINT ct_team_id IF NOT EXISTS FOR (t:CoverageTeam) REQUIRE t.teamId IS UNIQUE",
    "CREATE INDEX ct_org_account IF NOT EXISTS FOR (o:ClientOrg) ON (o.accountId)",
]

_TEAMS = """UNWIND $teams AS t
MERGE (n:CoverageTeam {teamId: t.teamId})
SET n.name = t.name, n.source = $source"""

# The account key is stamped onto the existing ClientOrg so the join back to the
# view is visible in the graph rather than only in this file.
_ACCOUNT_KEYS = """UNWIND $covers AS c
MATCH (o:ClientOrg {name: c.accountName})
SET o.accountId = c.accountId"""

_UNKNOWN_ACCOUNTS = """UNWIND $covers AS c
OPTIONAL MATCH (o:ClientOrg {name: c.accountName})
WITH c WHERE o IS NULL
RETURN DISTINCT c.accountId AS accountId, c.accountName AS accountName"""

_UNKNOWN_MEMBERS = """UNWIND $members AS m
OPTIONAL MATCH (e:Employee {employeeId: m.employeeId})
WITH m WHERE e IS NULL
RETURN DISTINCT m.employeeId AS employeeId"""

# Membership and coverage are both REPLACED for every team the extract names: the
# view is a snapshot, and a team that lost an account must lose the edge.
_MEMBERS = """UNWIND $teams AS t
MATCH (tm:CoverageTeam {teamId: t.teamId})
OPTIONAL MATCH (:Employee)-[old:MEMBER_OF]->(tm)
DELETE old
WITH count(*) AS _
UNWIND $members AS m
MATCH (tm:CoverageTeam {teamId: m.teamId}), (e:Employee {employeeId: m.employeeId})
MERGE (e)-[r:MEMBER_OF]->(tm)
SET r.role = m.role"""

_COVERS = """UNWIND $teams AS t
MATCH (tm:CoverageTeam {teamId: t.teamId})
OPTIONAL MATCH (tm)-[old:COVERS]->(:ClientOrg)
DELETE old
WITH count(*) AS _
UNWIND $covers AS c
MATCH (tm:CoverageTeam {teamId: c.teamId}), (o:ClientOrg {name: c.accountName})
MERGE (tm)-[r:COVERS]->(o)
SET r.validFrom = date(c.validFrom),
    r.validTo   = CASE WHEN c.validTo IS NULL THEN NULL ELSE date(c.validTo) END"""


def load(executor, teams, members, covers) -> list[str]:
    for ddl in _CONSTRAINTS:
        executor.run(ddl, {}, read_only=False)
    executor.run(_TEAMS, {"teams": teams, "source": SOURCE}, read_only=False)

    problems = []
    for row in executor.run(_UNKNOWN_ACCOUNTS, {"covers": covers}, read_only=True):
        problems.append(f"account {row['accountId']!r} ({row['accountName']!r}) matches no "
                        ":ClientOrg — load the business graph first")
    for row in executor.run(_UNKNOWN_MEMBERS, {"members": members}, read_only=True):
        problems.append(f"employee {row['employeeId']!r} matches no :Employee — load "
                        "business_hierarchy first")
    if problems:
        return problems

    executor.run(_ACCOUNT_KEYS, {"covers": covers}, read_only=False)
    executor.run(_MEMBERS, {"teams": teams, "members": members}, read_only=False)
    executor.run(_COVERS, {"teams": teams, "covers": covers}, read_only=False)
    return []


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path", nargs="?",
                    default="bundles/asset_platform/data/views/coverage_teams.csv")
    ap.add_argument("--bundle")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    rows = _read(Path(args.csv_path))
    teams, members, covers, problems = project(rows)
    print(f"coverage_teams: {len(rows)} rows -> {len(teams)} teams, "
          f"{len(members)} memberships, {len(covers)} covered accounts")
    for problem in problems:
        print(f"  !! {problem}")
    if problems:
        print("\n  refusing to load an extract that disagrees with itself about a "
              "validity window")
        return 1
    if args.dry_run:
        print("  (dry run — nothing written)")
        return 0

    config = Config.from_env(active_bundle=args.bundle or active_bundle_names()[0])
    executor = Neo4jExecutor(config)
    try:
        dangling = load(executor, teams, members, covers)
        for problem in dangling:
            print(f"  !! {problem}")
        if dangling:
            return 1
        counts = executor.run(
            "MATCH (t:CoverageTeam) WITH count(t) AS teams "
            "OPTIONAL MATCH ()-[r:MEMBER_OF]->(:CoverageTeam) WITH teams, count(r) AS members "
            "OPTIONAL MATCH (:CoverageTeam)-[r:COVERS]->() "
            "WITH teams, members, count(r) AS covers "
            "OPTIONAL MATCH (:CoverageTeam)-[r:COVERS]->() WHERE r.validTo < date() "
            "RETURN teams, members, covers, count(r) AS expired", {}, read_only=True)
        print(f"\n  loaded: {counts[0]}")
    finally:
        executor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
