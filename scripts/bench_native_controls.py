#!/usr/bin/env python
"""What do Neo4j's own access controls cost, and how do those costs scale?

The gateway's entitlement filter is one layer; the database has its own. Before
advising anyone to push rules down into it, it is worth knowing what each native
mechanism costs — and they are three different mechanisms with three different
cost shapes:

  RBAC (label-scoped)   privileges by label/type. The baseline.
  PBAC (property-based) privileges with a property predicate, evaluated
                        PER ROW as the query runs -> cost scales with rows read.
  ABAC (attribute-based) auth rules that decide which ROLES a user holds,
                        evaluated once when the TRANSACTION BEGINS -> a fixed
                        cost per query, independent of how much data is touched.

Because the shapes differ, the script measures at several data sizes and with a
trivial query as well as a large one. A per-row cost shows up as a widening gap;
a per-transaction cost shows up as a constant that dominates only when the query
itself is small.

    uv run python scripts/bench_native_controls.py --database benchdb
    uv run python scripts/bench_native_controls.py --sizes 10000,100000,500000

Requires Neo4j Enterprise (or AuraDB Business Critical / Virtual Dedicated Cloud)
and an account able to manage users, roles and privileges. ABAC additionally
needs 2026.03+, and the native-tag form used here needs 2026.06+ with
`dbms.security.abac.authorization_providers` including `native`; the script
detects support and skips that section cleanly rather than failing.

Everything it creates is named with a `nb_` prefix and removed on exit.
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

PREFIX = "nb_"
TAG = "native-bench"
PASSWORD = "benchpassword1"

# Big query: touches every node, so per-row costs dominate.
BIG = "MATCH (n:BenchNode) WHERE n.bucket >= 0 RETURN count(n) AS n"
# Tiny query: touches NO graph data at all, so the only thing left to measure is
# the fixed per-transaction cost — round trip plus whatever authorization work
# happens when the transaction begins. A query that still scans (even one whose
# own predicate matches nothing) is not tiny for this purpose: a property rule is
# evaluated during the scan, before the query's own WHERE can reject the row.
TINY = "RETURN 1 AS n"


def _hits(profile: dict | None) -> int:
    if not profile:
        return 0
    total = int(profile.get("dbHits", 0) or 0)
    for child in profile.get("children") or []:
        total += _hits(child)
    return total


def _measure(uri, user, password, database, query, runs, impersonate=None) -> float:
    driver = neo4j.GraphDatabase.driver(uri, auth=(user, password),
                                        notifications_min_severity="OFF")
    try:
        kw = {"database": database}
        if impersonate:
            kw["impersonated_user"] = impersonate
        with driver.session(**kw) as session:
            for _ in range(3):
                session.run(query).consume()
            samples = []
            for _ in range(runs):
                start = time.perf_counter()
                list(session.run(query))
                samples.append((time.perf_counter() - start) * 1000)
            return statistics.median(samples)
    finally:
        driver.close()


def _server_version(session) -> tuple[int, int]:
    row = session.run(
        "CALL dbms.components() YIELD versions RETURN versions[0] AS v").single()
    raw = str(row["v"]) if row else "0.0"
    parts = raw.split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return (0, 0)


def _abac_supported(system, version: tuple[int, int]) -> tuple[bool, str]:
    """ABAC with native user tags needs 2026.06+ and the provider configured."""
    if version < (2026, 6):
        return False, f"server is {version[0]}.{version[1]}; native-tag ABAC needs 2026.06+"
    try:
        system.run(f"CREATE AUTH RULE {PREFIX}probe "
                   f"SET CONDITION 'probe' IN abac.native.user_tags()").consume()
        system.run(f"DROP AUTH RULE {PREFIX}probe").consume()
        return True, ""
    except Exception as exc:  # noqa: BLE001 - report why, do not fail the run
        return False, str(exc).split("\n")[0][:120]


def _seed(session, size: int) -> None:
    session.run("MATCH (n:BenchNode) CALL { WITH n DETACH DELETE n } "
                "IN TRANSACTIONS OF 10000 ROWS").consume()
    session.run(f"""
        UNWIND range(0, $size - 1) AS i
        CALL {{ WITH i CREATE (:BenchNode {{source: '{TAG}', bucket: i % 10,
                                            payload: 'x' + toString(i)}}) }}
        IN TRANSACTIONS OF 10000 ROWS
    """, {"size": size}).consume()


def _provision(system, database: str, abac: bool) -> None:
    grants = (f"GRANT ACCESS ON DATABASE {database} TO {{role}}; "
              f"GRANT MATCH {{{{*}}}} ON GRAPH {database} RELATIONSHIPS * TO {{role}};")

    # Baseline: privileges scoped by label.
    system.run(f"CREATE ROLE {PREFIX}label IF NOT EXISTS").consume()
    for stmt in grants.format(role=f"{PREFIX}label").split(";"):
        if stmt.strip():
            system.run(stmt).consume()
    system.run(f"GRANT MATCH {{*}} ON GRAPH {database} NODES * TO {PREFIX}label").consume()

    # Property-based: same visible data, but a predicate evaluated per row.
    system.run(f"CREATE ROLE {PREFIX}prop IF NOT EXISTS").consume()
    for stmt in grants.format(role=f"{PREFIX}prop").split(";"):
        if stmt.strip():
            system.run(stmt).consume()
    system.run(f"GRANT MATCH {{*}} ON GRAPH {database} "
               f"FOR (n) WHERE n.source = '{TAG}' TO {PREFIX}prop").consume()

    for name, role in ((f"{PREFIX}u_label", f"{PREFIX}label"),
                       (f"{PREFIX}u_prop", f"{PREFIX}prop")):
        system.run(f"CREATE USER {name} IF NOT EXISTS SET PASSWORD '{PASSWORD}' "
                   f"CHANGE NOT REQUIRED").consume()
        system.run(f"GRANT ROLE {role} TO {name}").consume()

    # A service account that impersonates, which is how a shared connection can
    # still have the END USER's native rules applied.
    system.run(f"CREATE ROLE {PREFIX}svc IF NOT EXISTS").consume()
    for stmt in grants.format(role=f"{PREFIX}svc").split(";"):
        if stmt.strip():
            system.run(stmt).consume()
    system.run(f"GRANT MATCH {{*}} ON GRAPH {database} NODES * TO {PREFIX}svc").consume()
    system.run(f"GRANT IMPERSONATE ({PREFIX}u_label, {PREFIX}u_prop) ON DBMS "
               f"TO {PREFIX}svc").consume()
    system.run(f"CREATE USER {PREFIX}u_svc IF NOT EXISTS SET PASSWORD '{PASSWORD}' "
               f"CHANGE NOT REQUIRED").consume()
    system.run(f"GRANT ROLE {PREFIX}svc TO {PREFIX}u_svc").consume()

    if not abac:
        return

    # Attribute-based: the SAME label-scoped role, but reached through an auth
    # rule evaluated at transaction start rather than a static grant. Comparing
    # these two isolates the cost of rule evaluation from the cost of the
    # privileges themselves.
    system.run(f"CREATE AUTH RULE {PREFIX}rule IF NOT EXISTS "
               f"SET CONDITION '{TAG}' IN abac.native.user_tags()").consume()
    system.run(f"GRANT ROLE {PREFIX}label TO AUTH RULE {PREFIX}rule").consume()
    system.run(f"CREATE USER {PREFIX}u_abac IF NOT EXISTS SET PASSWORD '{PASSWORD}' "
               f"CHANGE NOT REQUIRED SET TAGS ['{TAG}']").consume()

    # A deliberately heavier condition, to show whether complexity matters.
    system.run(f"CREATE AUTH RULE {PREFIX}rule2 IF NOT EXISTS SET CONDITION "
               f"any(t IN abac.native.user_tags() WHERE toUpper(t) = toUpper('{TAG}')) "
               f"AND size([x IN range(0, 50) WHERE x >= 0]) > 0").consume()
    system.run(f"GRANT ROLE {PREFIX}label TO AUTH RULE {PREFIX}rule2").consume()
    system.run(f"CREATE USER {PREFIX}u_abac2 IF NOT EXISTS SET PASSWORD '{PASSWORD}' "
               f"CHANGE NOT REQUIRED SET TAGS ['{TAG}']").consume()


def _cleanup(system, abac: bool) -> None:
    for user in ("u_label", "u_prop", "u_svc", "u_abac", "u_abac2"):
        try:
            system.run(f"DROP USER {PREFIX}{user} IF EXISTS").consume()
        except Exception:  # noqa: BLE001
            pass
    if abac:
        for rule in ("rule", "rule2"):
            try:
                system.run(f"DROP AUTH RULE {PREFIX}{rule} IF EXISTS").consume()
            except Exception:  # noqa: BLE001
                pass
    for role in ("label", "prop", "svc"):
        try:
            system.run(f"DROP ROLE {PREFIX}{role} IF EXISTS").consume()
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Benchmark Neo4j's native access controls.")
    ap.add_argument("bundle", nargs="?", help="bundle whose connection to use")
    ap.add_argument("--database", default="benchdb",
                    help="database to create the benchmark data in (default benchdb)")
    ap.add_argument("--sizes", default="10000,100000",
                    help="node counts to measure at, comma separated")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--keep", action="store_true", help="leave users/roles/data in place")
    args = ap.parse_args(argv)

    config = Config.from_env(active_bundle=args.bundle or active_bundle_names()[0])
    sizes = [int(s) for s in args.sizes.split(",")]
    admin = neo4j.GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password),
        notifications_min_severity="OFF")

    try:
        with admin.session(database="system") as system:
            version = _server_version(system)
            abac, why = _abac_supported(system, version)
            print(f"server {version[0]}.{version[1]}  database {args.database}")
            print(f"ABAC: {'available' if abac else 'SKIPPED — ' + why}\n")
            system.run(f"CREATE DATABASE {args.database} IF NOT EXISTS WAIT").consume()
            _cleanup(system, abac=True)
            _provision(system, args.database, abac)

        uri, db, runs = config.neo4j_uri, args.database, args.runs
        results: dict[int, dict[str, float]] = {}

        for size in sizes:
            with admin.session(database=db) as session:
                _seed(session, size)

            row: dict[str, float] = {}
            row["rbac"] = _measure(uri, f"{PREFIX}u_label", PASSWORD, db, BIG, runs)
            row["pbac"] = _measure(uri, f"{PREFIX}u_prop", PASSWORD, db, BIG, runs)
            row["rbac_tiny"] = _measure(uri, f"{PREFIX}u_label", PASSWORD, db, TINY, runs)
            row["pbac_tiny"] = _measure(uri, f"{PREFIX}u_prop", PASSWORD, db, TINY, runs)
            row["imp"] = _measure(uri, f"{PREFIX}u_svc", PASSWORD, db, BIG, runs,
                                  impersonate=f"{PREFIX}u_label")
            if abac:
                row["abac"] = _measure(uri, f"{PREFIX}u_abac", PASSWORD, db, BIG, runs)
                row["abac_tiny"] = _measure(uri, f"{PREFIX}u_abac", PASSWORD, db, TINY, runs)
                row["abac2_tiny"] = _measure(uri, f"{PREFIX}u_abac2", PASSWORD, db, TINY, runs)
            results[size] = row

        # ---- report ----------------------------------------------------------
        print("LARGE query (scans every node) — per-ROW costs dominate\n")
        print(f"  {'nodes':>9} {'RBAC label':>11} {'PBAC prop':>11} {'PBAC/RBAC':>10}"
              + (f" {'ABAC':>9} {'ABAC/RBAC':>10}" if abac else ""))
        for size in sizes:
            r = results[size]
            line = (f"  {size:9,} {r['rbac']:10.1f}ms {r['pbac']:10.1f}ms "
                    f"{r['pbac'] / r['rbac']:9.2f}x")
            if abac:
                line += f" {r['abac']:8.1f}ms {r['abac'] / r['rbac']:9.2f}x"
            print(line)

        print("\nTINY query (touches almost nothing) — per-TRANSACTION costs dominate\n")
        print(f"  {'nodes':>9} {'RBAC label':>11} {'PBAC prop':>11}"
              + (f" {'ABAC':>9} {'ABAC heavy':>11}" if abac else ""))
        for size in sizes:
            r = results[size]
            line = f"  {size:9,} {r['rbac_tiny']:10.2f}ms {r['pbac_tiny']:10.2f}ms"
            if abac:
                line += f" {r['abac_tiny']:8.2f}ms {r['abac2_tiny']:10.2f}ms"
            print(line)

        print("\nIMPERSONATION (a shared service connection acting as the end user)\n")
        for size in sizes:
            r = results[size]
            print(f"  {size:9,} direct {r['rbac']:7.1f}ms   impersonated {r['imp']:7.1f}ms"
                  f"   {r['imp'] - r['rbac']:+.1f}ms")

        print("\nReading this:")
        print("  * A per-ROW cost widens as the node count grows — look at whether the")
        print("    PBAC/RBAC ratio holds or increases across sizes.")
        print("  * A per-TRANSACTION cost is a constant. It is invisible on a large query")
        print("    and dominant on a small one, so judge ABAC on the tiny-query table.")
        print("  * Impersonation is what makes native per-user rules apply at all when a")
        print("    gateway holds one shared connection.")

        if not args.keep:
            with admin.session(database=db) as session:
                session.run("MATCH (n:BenchNode) CALL { WITH n DETACH DELETE n } "
                            "IN TRANSACTIONS OF 10000 ROWS").consume()
            with admin.session(database="system") as system:
                _cleanup(system, abac)
            print("\ncleaned up users, roles, auth rules and benchmark data")
    finally:
        admin.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
