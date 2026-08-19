#!/usr/bin/env python
"""Project the `business_hierarchy` view into the entitlement graph.

    uv run python scripts/ingest_business_hierarchy.py \
        bundles/asset_platform/data/views/business_hierarchy.csv
    uv run python scripts/ingest_business_hierarchy.py --dry-run <file>

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
The authoritative source for who reports to whom, and which unit somebody sits
in, is a relational VIEW. Applications entitled off it today by JOINing the view
to whatever data they were guarding — the JOIN being written once per
application, in application code, differently each time.

This script is the whole replacement for that JOIN, and it is a PROJECTION rather
than a transformation. Every output edge is one column pair of one input row:

    unit_id, parent_unit_id      ->  (:OrgUnit)-[:PART_OF]->(:OrgUnit)
    employee_id, unit_id         ->  (:Employee)-[:IN_UNIT]->(:OrgUnit)
    employee_id, manager_id      ->  (:Employee)-[:REPORTS_TO]->(:Employee)
    rank_level                   ->  Employee.rankLevel      (a caller attribute)

That property matters more than it looks. It means:

* No business logic lives here. The script cannot decide anything, so it cannot
  decide anything WRONG. All the entitlement logic is in bundle.yaml, where it is
  declared, computable (scripts/entitlement_surface.py) and tested
  (scripts/check_entitlements.py).
* Swapping the real view in is editing COLUMNS below — a column remap, not a
  rewrite. If a name differs, change the right-hand side and nothing else.
* The messiness upstream stays upstream. Whatever tangle of logic produces the
  authoritative view, this consumes its OUTPUT. We are not reimplementing the
  lineage, and we should not offer to.

WHAT REPLACES THE JOIN. "Everyone sees their own compensation; managing directors
see the unit's" is two rules over these edges plus one threshold on rankLevel —
declared once, enforced for every query, rather than re-implemented per
application. That is the trade this whole design makes.

REFRESH. The view is a snapshot of current state, so this run is idempotent and
last-write-wins: rows are MERGEd, and edges that the view no longer contains are
removed for the employees and units it does contain. A person who vanishes from
the extract entirely is REPORTED, not deleted — a truncated extract must not
silently strip entitlement structure. See --prune to override deliberately.

FAILURE DIRECTION. Every projection here feeds GRANTS, so a missing row
under-grants: somebody sees less, a conformance case notices. The dangerous
direction is a missing row that feeds a DENIAL, and there are none in this view —
which is a reason to keep barriers out of the HR feed.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.config import Config, active_bundle_names  # noqa: E402
from gateway.yaml_tools import Neo4jExecutor  # noqa: E402

# --------------------------------------------------------------------------- #
# THE ONLY THING THAT CHANGES when the real view arrives: left = what we need,
# right = what the extract calls it.
# --------------------------------------------------------------------------- #
COLUMNS = {
    "employee_id": "employee_id",
    "email": "work_email",
    "name": "full_name",
    "title": "job_title",
    "rank": "rank_level",
    "unit_id": "unit_id",
    "unit_name": "unit_name",
    "unit_kind": "unit_kind",
    "parent_unit_id": "parent_unit_id",
    "manager_id": "manager_employee_id",
}

# A unit's kind decides a second, semantic label alongside :OrgUnit, so rules can
# say "the desk it was booked on" as well as "the unit tree above it". The map is
# a WHITELIST because the label is interpolated into Cypher; an unknown kind is an
# error rather than a silently unlabelled node.
UNIT_LABELS = {
    "desk": "Desk",
    "business-unit": "BusinessUnit",
    "division": "Division",
}

SOURCE = "bh-view"


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


def project(rows: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Split the denormalised view into units, people, and complaints about it."""
    units: dict[str, dict] = {}
    people: dict[str, dict] = {}
    problems: list[str] = []

    for n, row in enumerate(rows, start=2):   # 2 = first data line, with a header
        unit_id = _get(row, "unit_id")
        if unit_id:
            kind = _get(row, "unit_kind").lower()
            if kind not in UNIT_LABELS:
                problems.append(f"line {n}: unit {unit_id!r} has unknown kind {kind!r} "
                                f"(known: {sorted(UNIT_LABELS)})")
                continue
            record = {"unitId": unit_id, "name": _get(row, "unit_name"),
                      "kind": kind, "parent": _get(row, "parent_unit_id") or None}
            prior = units.setdefault(unit_id, record)
            if prior != record:
                problems.append(
                    f"line {n}: unit {unit_id!r} appears twice with different attributes "
                    f"({prior} then {record}) — the view is internally inconsistent")

        emp_id = _get(row, "employee_id")
        if not emp_id:
            continue                            # a unit-only row: an outer join with no staff
        email = _get(row, "email").lower()
        if not email:
            problems.append(f"line {n}: employee {emp_id!r} has no email, which is the key "
                            "identity resolution matches on — the row is unusable")
            continue
        rank = _get(row, "rank")
        if not rank.isdigit():
            problems.append(f"line {n}: employee {emp_id!r} has non-numeric rank {rank!r}; "
                            "a threshold on it would be NULL and never fire")
        people[emp_id] = {
            "employeeId": emp_id, "email": email, "name": _get(row, "name"),
            "title": _get(row, "title"),
            "rankLevel": int(rank) if rank.isdigit() else None,
            "unitId": unit_id or None,
            "managerId": _get(row, "manager_id") or None,
        }

    known = set(units)
    for unit in units.values():
        if unit["parent"] and unit["parent"] not in known:
            problems.append(f"unit {unit['unitId']!r} names parent {unit['parent']!r}, which "
                            "the extract does not contain — the unit tree is cut here, and "
                            "every rule that walks up it stops short")
    for person in people.values():
        if person["unitId"] not in known:
            problems.append(f"employee {person['employeeId']!r} sits in unit "
                            f"{person['unitId']!r}, which the extract does not contain")
        mgr = person["managerId"]
        if mgr and mgr not in people:
            problems.append(f"employee {person['employeeId']!r} reports to {mgr!r}, who is "
                            "not in the extract — the management line stops here")
    return list(units.values()), list(people.values()), problems


# --------------------------------------------------------------------------- #
# Writes. One statement per output edge type, each an idempotent MERGE.
# --------------------------------------------------------------------------- #
_UNITS = """UNWIND $units AS u
MERGE (n:OrgUnit {unitId: u.unitId})
SET n.name = u.name, n.kind = u.kind, n.source = $source"""

# The semantic label is interpolated, so it comes from UNIT_LABELS and never
# from the row. One statement per kind rather than a dynamic SET, which keeps the
# whitelist visible at the call site.
_UNIT_LABEL = """UNWIND $ids AS id
MATCH (n:OrgUnit {unitId: id})
SET n:@@LABEL@@"""

_UNIT_TREE = """UNWIND [u IN $units WHERE u.parent IS NOT NULL] AS u
MATCH (child:OrgUnit {unitId: u.unitId}), (parent:OrgUnit {unitId: u.parent})
MERGE (child)-[:PART_OF]->(parent)"""

_PEOPLE = """UNWIND $people AS p
MERGE (e:Employee {email: p.email})
SET e.employeeId = p.employeeId, e.name = p.name, e.title = p.title,
    e.rankLevel = p.rankLevel, e.source = $source"""

# Placement and reporting are REPLACED for every employee the extract names, so a
# desk move in the view is a desk move in the graph. Employees the extract does
# not name are untouched — see REFRESH in the module docstring.
_PLACEMENT = """UNWIND $people AS p
MATCH (e:Employee {email: p.email})
OPTIONAL MATCH (e)-[old:IN_UNIT]->(:OrgUnit)
DELETE old
WITH e, p
MATCH (u:OrgUnit {unitId: p.unitId})
MERGE (e)-[:IN_UNIT]->(u)"""

_REPORTING = """UNWIND $people AS p
MATCH (e:Employee {email: p.email})
OPTIONAL MATCH (e)-[old:REPORTS_TO]->(:Employee)
DELETE old
WITH e, p WHERE p.managerId IS NOT NULL
MATCH (m:Employee {employeeId: p.managerId})
MERGE (e)-[:REPORTS_TO]->(m)"""

_CONSTRAINTS = [
    "CREATE CONSTRAINT bh_unit_id IF NOT EXISTS FOR (u:OrgUnit) REQUIRE u.unitId IS UNIQUE",
    "CREATE CONSTRAINT bh_emp_email IF NOT EXISTS FOR (e:Employee) REQUIRE e.email IS UNIQUE",
    "CREATE INDEX bh_emp_id IF NOT EXISTS FOR (e:Employee) ON (e.employeeId)",
    # Rules compare rankLevel; without this the threshold is a property scan.
    "CREATE INDEX bh_emp_rank IF NOT EXISTS FOR (e:Employee) ON (e.rankLevel)",
]


def load(executor, units: list[dict], people: list[dict]) -> None:
    for ddl in _CONSTRAINTS:
        executor.run(ddl, {}, read_only=False)
    executor.run(_UNITS, {"units": units, "source": SOURCE}, read_only=False)
    by_kind: dict[str, list[str]] = defaultdict(list)
    for unit in units:
        by_kind[unit["kind"]].append(unit["unitId"])
    for kind, ids in by_kind.items():
        executor.run(_UNIT_LABEL.replace("@@LABEL@@", UNIT_LABELS[kind]),
                     {"ids": ids}, read_only=False)
    executor.run(_UNIT_TREE, {"units": units}, read_only=False)
    executor.run(_PEOPLE, {"people": people, "source": SOURCE}, read_only=False)
    executor.run(_PLACEMENT, {"people": people}, read_only=False)
    executor.run(_REPORTING, {"people": people}, read_only=False)


_ORPHANS = """MATCH (e:Employee) WHERE e.employeeId IS NOT NULL
  AND NOT e.employeeId IN $ids
RETURN e.email AS email, e.employeeId AS employeeId"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path", nargs="?",
                    default="bundles/asset_platform/data/views/business_hierarchy.csv")
    ap.add_argument("--bundle")
    ap.add_argument("--dry-run", action="store_true",
                    help="project and validate, write nothing")
    ap.add_argument("--prune", action="store_true",
                    help="also detach employees absent from the extract (destructive)")
    args = ap.parse_args(argv)

    rows = _read(Path(args.csv_path))
    units, people, problems = project(rows)

    print(f"business_hierarchy: {len(rows)} rows -> {len(units)} units, {len(people)} people")
    for problem in problems:
        print(f"  !! {problem}")
    if problems:
        # Every problem above cuts a traversal short, which under-grants silently.
        print("\n  refusing to load an extract that would produce a broken hierarchy")
        return 1
    if args.dry_run:
        print("  (dry run — nothing written)")
        return 0

    config = Config.from_env(active_bundle=args.bundle or active_bundle_names()[0])
    executor = Neo4jExecutor(config)
    try:
        load(executor, units, people)
        ids = [p["employeeId"] for p in people]
        orphans = executor.run(_ORPHANS, {"ids": ids}, read_only=True)
        if orphans:
            print(f"\n  {len(orphans)} employee(s) in the graph are absent from this extract:")
            for row in orphans:
                print(f"    {row['employeeId']}  {row['email']}")
            if args.prune:
                executor.run(
                    "MATCH (e:Employee) WHERE e.employeeId IS NOT NULL AND NOT e.employeeId IN $ids "
                    "DETACH DELETE e", {"ids": ids}, read_only=False)
                print("    pruned (--prune)")
            else:
                print("    left in place — a truncated extract must not silently strip "
                      "entitlement structure. Re-run with --prune if the absence is real.")
        counts = executor.run(
            "MATCH (u:OrgUnit) WITH count(u) AS units "
            "MATCH (e:Employee) WITH units, count(e) AS people "
            "OPTIONAL MATCH ()-[r:PART_OF]->() WITH units, people, count(r) AS partOf "
            "OPTIONAL MATCH ()-[r:IN_UNIT]->() WITH units, people, partOf, count(r) AS inUnit "
            "OPTIONAL MATCH ()-[r:REPORTS_TO]->() "
            "RETURN units, people, partOf, inUnit, count(r) AS reportsTo", {}, read_only=True)
        print(f"\n  loaded: {counts[0]}")
    finally:
        executor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
