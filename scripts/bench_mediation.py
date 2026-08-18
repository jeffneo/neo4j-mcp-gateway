#!/usr/bin/env python
"""Measure what entitlement mediation costs: open vs mediated, same query.

For each mediated tool the script runs three variants and compares them:

  open      the tool's match + return with NO prelude and NO filter — exactly what
            the tool would be if the bundle declared security.mode: open
  mediated  the composed query the gateway actually runs
  prelude   the authorization prelude alone (identity resolution). This is the
            fixed overhead per call and the closest analogue to a policy-decision
            lookup, so it is usually the number worth quoting.

Reports wall-clock percentiles AND total database hits from PROFILE. On a small
demo dataset wall-clock is dominated by network round-trip and JIT warm-up, so
**db hits is the more honest signal** — it measures work done inside the engine
and does not depend on your laptop or the link to Aura.

Usage:
    uv run python scripts/bench_mediation.py                      # active bundle, all tools
    uv run python scripts/bench_mediation.py iam --runs 50
    uv run python scripts/bench_mediation.py iam --tool client_activity \
        --principal maria.chen@bank.com

Notes:
  * Mediated results are filtered, so they often return FEWER rows than open.
    Row counts are printed alongside: a mediated query can be *faster* simply
    because it carries less data back, which is not a fair win. Compare db hits.
  * Requires a bundle with security.mode: mediated and data already loaded.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import neo4j

from gateway import mediation
from gateway.config import Config, active_bundle_names
from gateway.yaml_tools import ToolSpec, load_tool_specs


def _profile_db_hits(profile: dict | None) -> int:
    """Sum DbHits across every operator in a PROFILE plan."""
    if not profile:
        return 0
    total = int(profile.get("dbHits", 0) or 0)
    args = profile.get("args") or {}
    if not total and isinstance(args, dict):
        total = int(args.get("DbHits", 0) or 0)
    for child in profile.get("children") or []:
        total += _profile_db_hits(child)
    return total


def _variants(policy, spec: ToolSpec) -> dict[str, str]:
    """The three queries to compare for one tool."""
    final_return = spec.return_clause.strip()
    open_query = f"{spec.match_clause.strip()}\n{final_return}"
    mediated_query = mediation.compose(
        policy, spec.match_clause, spec.scope, final_return, spec.protect
    )
    prelude_query = mediation.prelude_only_query(policy)
    return {"open": open_query, "mediated": mediated_query, "prelude": prelude_query}


def _time_query(session, query: str, params: dict) -> tuple[float, int]:
    """Run once; return (elapsed_ms, row_count)."""
    start = time.perf_counter()
    result = session.run(query, params)
    rows = len(list(result))
    result.consume()
    return (time.perf_counter() - start) * 1000.0, rows


def _db_hits(session, query: str, params: dict) -> int:
    summary = session.run(f"PROFILE {query}", params).consume()
    return _profile_db_hits(summary.profile)


def _stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))],
        "mean": statistics.fmean(ordered),
        "min": ordered[0],
    }


def _pct(new: float, base: float) -> str:
    if base <= 0:
        return "n/a"
    return f"{(new - base) / base * 100:+.0f}%"


def bench_tool(session, policy, spec: ToolSpec, principal: str, runs: int, warmup: int) -> None:
    args = {p.name: p.default for p in spec.parameters if p.has_default}
    args.update(spec.sample_args)
    missing = [p.name for p in spec.parameters
               if p.required and p.name not in args]
    if missing:
        print(f"\n{spec.name}: SKIP (needs args: {', '.join(missing)}; add sample_args to the tool)")
        return

    queries = _variants(policy, spec)
    sec = mediation.security_params(policy, principal)
    params = {**args, **sec}

    print(f"\n{spec.name}   (principal: {principal})")

    for _ in range(warmup):
        for q in queries.values():
            _time_query(session, q, params)

    # Interleave variants so machine/network drift hits all of them equally.
    samples: dict[str, list[float]] = {k: [] for k in queries}
    rows: dict[str, int] = {}
    for _ in range(runs):
        for name, q in queries.items():
            elapsed, n = _time_query(session, q, params)
            samples[name].append(elapsed)
            rows[name] = n

    hits = {name: _db_hits(session, q, params) for name, q in queries.items()}
    base = _stats(samples["open"])

    print(f"  {'variant':10} {'p50 ms':>9} {'p95 ms':>9} {'mean ms':>9} "
          f"{'vs open':>9} {'db hits':>9} {'rows':>6}")
    for name in ("open", "mediated", "prelude"):
        s = _stats(samples[name])
        delta = "—" if name == "open" else _pct(s["p50"], base["p50"])
        print(f"  {name:10} {s['p50']:9.2f} {s['p95']:9.2f} {s['mean']:9.2f} "
              f"{delta:>9} {hits[name]:9,} {rows[name]:6,}")

    overhead = _stats(samples["mediated"])["p50"] - base["p50"]
    hit_overhead = hits["mediated"] - hits["open"]
    # Split the cost: identity resolution is a constant per call; the row filter
    # scales with rows examined. Only the second grows with your data.
    fixed = hits["prelude"]
    variable = max(0, hit_overhead - fixed)
    per_row = variable / max(rows["open"], 1)
    print(f"  → mediation adds {overhead:+.2f} ms (p50), {hit_overhead:+,} db hits")
    print(f"    of which  fixed (identity prelude): {fixed:,} db hits, "
          f"{_stats(samples['prelude'])['p50']:.2f} ms")
    print(f"              variable (row filter):    {variable:,} db hits "
          f"(~{per_row:.1f} per row examined)")
    if rows["mediated"] != rows["open"]:
        print(f"  ! mediated returned {rows['mediated']} of {rows['open']} rows — "
              "some of the time difference is simply less data")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Benchmark entitlement mediation overhead.")
    ap.add_argument("bundle", nargs="?", help="bundle name (default: active bundle)")
    ap.add_argument("--tool", help="benchmark only this tool")
    ap.add_argument("--principal", help="run as this principal (default: first identity found)")
    ap.add_argument("--runs", type=int, default=25, help="timed runs per variant (default 25)")
    ap.add_argument("--warmup", type=int, default=5, help="untimed warm-up runs (default 5)")
    args = ap.parse_args(argv)

    config = Config.from_env(active_bundle=args.bundle or active_bundle_names()[0])
    policy = config.security
    if not policy.mediated:
        print(f"bundle '{config.active_bundle}' is security.mode: {policy.mode} — "
              "there is nothing to compare. Point this at a mediated bundle.", file=sys.stderr)
        return 1

    specs = [s for s in load_tool_specs(config.tools_dir) if s.is_mediated_form]
    if args.tool:
        specs = [s for s in specs if s.name == args.tool]
        if not specs:
            print(f"no mediated tool named {args.tool!r}", file=sys.stderr)
            return 1
    if not specs:
        print(f"no mediated-form tools in {config.tools_dir}", file=sys.stderr)
        return 1

    driver = neo4j.GraphDatabase.driver(
        config.neo4j_uri,
        auth=(config.neo4j_username, config.neo4j_password),
        notifications_min_severity="OFF",
    )
    try:
        with driver.session(database=config.neo4j_database) as session:
            principal = args.principal
            if not principal:
                found = session.run(
                    "MATCH (u) WHERE any(l IN labels(u) WHERE l IN $labels) "
                    "RETURN head([k IN $keys WHERE u[k] IS NOT NULL | u[k]]) AS p LIMIT 1",
                    {"labels": policy.identity.labels, "keys": policy.identity.match_keys},
                ).single()
                principal = (found and found["p"]) or "unknown@example.com"

            print(f"bundle '{config.active_bundle}'  db={config.neo4j_database}  "
                  f"runs={args.runs} (+{args.warmup} warm-up)")
            for spec in specs:
                bench_tool(session, policy, spec, principal, args.runs, args.warmup)

            print("\nCaveats: a demo-sized graph understates the filter's cost and overstates "
                  "\nthe prelude's, since the prelude is a fixed per-call traversal while the "
                  "\nfilter scales with rows examined. Compare db hits before wall-clock, and "
                  "\nre-run against production-scale data before quoting numbers.")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
