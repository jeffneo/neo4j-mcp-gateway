#!/usr/bin/env python
"""Entitlement conformance harness — assert who can and cannot see what.

Reads declarative cases from ``bundles/<bundle>/entitlement_tests.yaml`` and runs
each one through the real mediation path, as the named principal. Exits non-zero
on any failure, so it can gate CI and stand as evidence in a security review.

    uv run python scripts/check_entitlements.py iam
    uv run python scripts/check_entitlements.py iam --case "private"   # substring filter
    uv run python scripts/check_entitlements.py iam --verbose

Case shape (see the bundle's entitlement_tests.yaml for worked examples):

    - name: coverage colleague cannot see a private 1:1 chat
      principal: joe.hart@bank.com        # or: same_for: [a@x, b@x]
      # the query, either a curated tool ...
      tool: client_activity
      args: {client: Acme Corp}
      # ... or an inline mediated query
      match: |
        MATCH (x:Communication)-[:WITH_CLIENT]->(:Client {name: 'Acme Corp'})
      scope: [x]
      return: "RETURN x.commId AS id"
      # assertions
      id_field: id                        # column identifying a row
      must_see: [COMM-1002]               # every one of these must appear
      must_not_see: [COMM-1001]           # none of these may appear
      expect_count: 1                     # exact row count (optional)

``same_for`` asserts several principals get an IDENTICAL result set — the
coverage-parity requirement — and can be combined with must_see / must_not_see.

The harness composes queries directly rather than going through a tool's
``principal`` argument, so it does not require impersonation to be enabled and
tests the filter itself rather than the impersonation gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import mediation
from gateway.config import Config, active_bundle_names
from gateway.yaml_tools import Neo4jExecutor, load_tool_specs

TESTS_FILENAME = "entitlement_tests.yaml"


class CaseError(ValueError):
    """A malformed case. The message names the case."""


def _load_cases(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(
            f"no {TESTS_FILENAME} in {path.parent}\n"
            f"Create one to declare who must and must not see what."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"{path}: expected a non-empty 'cases:' list")
    return cases


def _anchored_pair(case: dict, specs: dict, policy) -> tuple[str, str, dict] | None:
    """For an anchored tool, return (anchored, unanchored, args) for comparison.

    An anchor restricts what the match examines, so a too-narrow one silently
    hides rows the caller is entitled to — the filter cannot restore them. Running
    the tool both ways and comparing is what catches that.
    """
    spec = specs.get(case.get("tool") or "")
    if spec is None or not getattr(spec, "anchor", None):
        return None
    args = {p.name: p.default for p in spec.parameters if p.has_default}
    args.update(case.get("args") or {})
    common = (policy, spec.match_clause, spec.scope, spec.return_clause.strip(), spec.protect)
    return (mediation.compose(*common, anchor=spec.anchor),
            mediation.compose(*common, anchor=None),
            args)


def _resolve_query(case: dict, specs: dict, policy) -> tuple[str, dict]:
    """Build (query, args) for a case from either a named tool or an inline query."""
    name = case.get("name", "<unnamed>")
    tool_name = case.get("tool")

    if tool_name:
        spec = specs.get(tool_name)
        if spec is None:
            raise CaseError(f"{name}: no tool named {tool_name!r} in this bundle")
        if not spec.is_mediated_form:
            raise CaseError(f"{name}: tool {tool_name!r} is not a mediated-form tool")
        args = {p.name: p.default for p in spec.parameters if p.has_default}
        args.update(case.get("args") or {})
        missing = [p.name for p in spec.parameters if p.required and p.name not in args]
        if missing:
            raise CaseError(f"{name}: tool {tool_name!r} needs args: {', '.join(missing)}")
        query = mediation.compose(
            policy, spec.match_clause, spec.scope, spec.return_clause.strip(), spec.protect
        )
        return query, args

    match_clause = case.get("match")
    scope = case.get("scope")
    return_clause = case.get("return")
    if not (match_clause and scope and return_clause):
        raise CaseError(f"{name}: needs either 'tool' or all of 'match', 'scope' and 'return'")
    query = mediation.compose(
        policy, match_clause, [str(v) for v in scope], return_clause.strip(), case.get("protect") or []
    )
    return query, dict(case.get("args") or {})


def _seen(executor, policy, query: str, args: dict, principal: str, id_field: str) -> list:
    params = {**args, **mediation.security_params(policy, principal)}
    rows = executor.run(query, params, read_only=True)
    for row in rows:
        if id_field not in row:
            raise CaseError(
                f"id_field {id_field!r} is not in the returned columns {sorted(row)}"
            )
    return [row[id_field] for row in rows]


def _check_assertions(case: dict, principal: str, seen: list) -> list[str]:
    """Return a list of failure messages (empty when the case passes)."""
    failures: list[str] = []
    seen_set = set(seen)

    for expected in case.get("must_see") or []:
        if expected not in seen_set:
            failures.append(f"{principal} should see {expected!r} but did not")

    for forbidden in case.get("must_not_see") or []:
        if forbidden in seen_set:
            failures.append(f"LEAK: {principal} must NOT see {forbidden!r} but did")

    expected_count = case.get("expect_count")
    if expected_count is not None and len(seen) != expected_count:
        failures.append(
            f"{principal} returned {len(seen)} row(s), expected {expected_count}"
        )
    return failures


def run_case(executor, policy, specs: dict, case: dict, verbose: bool) -> tuple[bool, list[str]]:
    name = case.get("name", "<unnamed>")
    id_field = case.get("id_field")
    if not id_field:
        raise CaseError(f"{name}: 'id_field' is required (the column identifying a row)")

    query, args = _resolve_query(case, specs, policy)
    anchored_pair = _anchored_pair(case, specs, policy)
    principals = case.get("same_for") or ([case["principal"]] if case.get("principal") else [])
    if not principals:
        raise CaseError(f"{name}: needs 'principal' or 'same_for'")

    failures: list[str] = []
    results: dict[str, list] = {}
    for principal in principals:
        seen = _seen(executor, policy, query, args, principal, id_field)
        results[principal] = seen
        failures.extend(_check_assertions(case, principal, seen))

        # An anchored tool must return exactly what it would unanchored.
        if anchored_pair:
            anchored_q, plain_q, a_args = anchored_pair
            a_seen = _seen(executor, policy, anchored_q, a_args, principal, id_field)
            p_seen = _seen(executor, policy, plain_q, a_args, principal, id_field)
            if sorted(map(str, a_seen)) != sorted(map(str, p_seen)):
                missing = set(map(str, p_seen)) - set(map(str, a_seen))
                extra = set(map(str, a_seen)) - set(map(str, p_seen))
                detail = []
                if missing:
                    detail.append(f"anchor HID {sorted(missing)} from {principal}")
                if extra:
                    detail.append(f"anchor added {sorted(extra)}")
                failures.append("ANCHOR MISMATCH: " + "; ".join(detail))
        if verbose:
            print(f"      {principal:28} -> {seen}")

    # same_for: every principal must get an identical result set.
    if case.get("same_for"):
        reference = principals[0]
        for other in principals[1:]:
            if set(results[other]) != set(results[reference]):
                failures.append(
                    f"parity broken: {reference} saw {sorted(map(str, results[reference]))} "
                    f"but {other} saw {sorted(map(str, results[other]))}"
                )
    return (not failures), failures


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Run entitlement conformance cases.")
    ap.add_argument("bundle", nargs="?", help="bundle name (default: active bundle)")
    ap.add_argument("--case", help="only run cases whose name contains this substring")
    ap.add_argument("--verbose", action="store_true", help="print what each principal saw")
    args = ap.parse_args(argv)

    config = Config.from_env(active_bundle=args.bundle or active_bundle_names()[0])
    policy = config.security
    if not policy.mediated:
        print(f"bundle '{config.active_bundle}' is security.mode: {policy.mode} — "
              "entitlement conformance only applies to a mediated bundle.", file=sys.stderr)
        return 1

    bundle_dir = config.tools_dir.parent
    cases = _load_cases(bundle_dir / TESTS_FILENAME)
    if args.case:
        cases = [c for c in cases if args.case.lower() in str(c.get("name", "")).lower()]
        if not cases:
            print(f"no case name contains {args.case!r}", file=sys.stderr)
            return 1

    specs = {s.name: s for s in load_tool_specs(config.tools_dir)}
    executor = Neo4jExecutor(config)

    passed = failed = 0
    leaks = 0
    try:
        print(f"entitlement conformance — bundle '{config.active_bundle}' "
              f"(db: {config.neo4j_database}, {len(cases)} case(s))\n")
        for case in cases:
            name = case.get("name", "<unnamed>")
            try:
                ok, failures = run_case(executor, policy, specs, case, args.verbose)
            except CaseError as exc:
                ok, failures = False, [str(exc)]
            except Exception as exc:  # noqa: BLE001 - a broken query is a failed case
                ok, failures = False, [f"error: {exc}"]

            if ok:
                passed += 1
                print(f"  PASS  {name}")
            else:
                failed += 1
                print(f"  FAIL  {name}")
                for message in failures:
                    if message.startswith("LEAK"):
                        leaks += 1
                    print(f"        {message}")
    finally:
        executor.close()

    print(f"\n{passed} passed, {failed} failed"
          + (f"  ({leaks} of the failures are data leaks)" if leaks else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
