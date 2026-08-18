"""Pluggable identity sources — where the caller's principals come from.

Mediation needs one thing before it can filter anything: the set of principals
the caller holds. This module is the seam that decides *where* that set is
computed, without changing how it is used.

Three sources ship. All three produce the same :class:`IdentityResult`, so the
filter, the conformance harness and ``explain-access`` are unchanged by the
choice::

    graph      identity lives beside the data; the prelude traverses it in the
               same statement. One round trip, caller node in reach.
    composite  identity and data are separate databases behind a composite
               database. Still one statement, joined with USE.
    remote     identity is resolved against a second Neo4j connection before the
               data query runs, and the principals travel as a parameter.

Adding a fourth (an HTTP entitlement service, LDAP, a token introspection
endpoint) means implementing :class:`IdentitySource` and calling
:func:`register_identity_source`. Nothing else in the engine changes.

SEPARATION_TRADEOFFS
--------------------
Neither separated source gives the data query a caller NODE: a composite database
refuses to import entity values across a ``USE`` boundary (``22N16``), and a
remote source has no caller node in the data database at all.

**Path grants survive both anyway**, because a grant does not actually need the
caller node — it needs a VALUE derived from the caller. A relationship cannot
span two graphs, but a traversal can be CUT at a node present in both: Neo4j's
documented **proxy node** pattern. The engine cuts each grant at the
identity/data boundary and re-roots the data-side half at that proxy, matched
against ``authz.authzPrincipals`` or ``authz.principalId``. See GRANT_SPLITTING
in gateway/mediation.py. Under ``composite`` that value is bound in-statement;
under ``remote`` it arrives as a query parameter. The emitted predicate is
otherwise identical, and the conformance suite asserts all three topologies
return the same rows for the same callers.

Anchoring splits the same way, for the same reason: an anchor is also a
traversal from the caller. Measured on 100,000 trades at 1% visibility
(scripts/bench_separation.py, Neo4j 2025.10.1):

    co-located    scan + ACL           76.9 ms      304,018 db hits
    co-located    scan + path          98.4 ms    1,999,518
    co-located    anchored + path       4.4 ms       22,540
    split         scan + split-path    68.7 ms    1,302,001
    split         anchored-split        4.7 ms       17,008
    split         anchored + ACL        3.1 ms        9,008

Identical answers from every variant. The anchor is worth ~17x either side of
the split, and the split forms are not slower than their co-located equivalents —
splitting the grant is marginally CHEAPER, because the data-side half starts at
the row and tests a name against a small list rather than expanding from the
caller on every row.

What separation actually costs:

1. **Replication surface** — *unless the boundary is already a property.* A
   grant cut at a proxy NODE needs that node in the data database. But where the
   boundary is recorded as a property (the covering team on the Client, the
   author on the Interaction), ``identity.boundary_properties`` cuts there
   instead and no proxies are needed at all. The proxy is the fallback, not the
   requirement.
2. **No tools scoping to ``caller``.** Tools get no caller binding, so "the
   clients I cover" must be expressed with an anchor or a parameter rather than
   by referencing ``caller`` in the match.
3. **Reference data must be local.** The filter runs in the data database, so
   anything it reads — ACLs, tenant ids — has to be there.

``remote`` additionally has two round trips and a consistency window: principals
are read at T0 and the data query runs at T1. ``composite`` keeps both in one
statement and one transaction, so it has neither. That, plus the fact that
``remote`` needs no composite database, is the whole difference between them.

What survives in all three sources: per-caller row filtering, path grants,
anchoring, aggregates computed after filtering, provenance via
``explain-access``, the curated-tool posture, and the conformance harness.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

import neo4j
from fastmcp.exceptions import ToolError

from .bundles import SOURCE_COMPOSITE, SOURCE_GRAPH, SOURCE_REMOTE, SecurityPolicy


@dataclass
class IdentityResult:
    """The answer every identity source must produce."""

    found: bool
    authz_principals: list[str]
    principal_id: str | None = None
    tenant_id: str | None = None
    groups: list[dict] = field(default_factory=list)
    principal_labels: list[str] = field(default_factory=list)
    principal_properties: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_authz(self) -> dict:
        """The shape the composed query expects as its ``authz`` binding."""
        return {
            "principalId": self.principal_id,
            "tenantId": self.tenant_id,
            "authzPrincipals": self.authz_principals,
        }


class IdentitySource(Protocol):
    """Resolve a principal string into the principals it holds."""

    def resolve(self, principal: str) -> IdentityResult: ...

    def sample_principals(self, limit: int) -> list[str]:
        """A few real principals, for validation and persona diffing.

        Wherever identity lives, that is where the list of people lives too, so
        the validator has to ask the source rather than query the data
        connection. A source with no enumeration (an entitlement API that only
        answers per-subject questions) may return an empty list.
        """
        ...

    def close(self) -> None: ...


_SAMPLE_QUERY = (
    "MATCH (u:@@LABELS@@) "
    "RETURN head([k IN $keys WHERE u[k] IS NOT NULL | u[k]]) AS p LIMIT $limit"
)


def _sample_query(policy: SecurityPolicy, use_graph: str = "") -> str:
    query = _SAMPLE_QUERY.replace("@@LABELS@@", "|".join(policy.identity.labels))
    return f"USE {use_graph}\n{query}" if use_graph else query


def _unique(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, str):
            value = value.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _from_row(policy: SecurityPolicy, principal: str, row: dict | None) -> IdentityResult:
    """Shared shaping so every graph-backed source returns identical structure."""
    authz = [principal, policy.principal.everyone]
    if row is None:
        return IdentityResult(
            found=False,
            authz_principals=_unique(authz),
            principal_id=principal,
            notes=["No matching identity node was found; the caller holds only their own "
                   "identity and the everyone principal."],
        )
    groups = row.get("groups") or []
    authz.extend(g.get("name") for g in groups if isinstance(g, dict) and g.get("name"))
    authz.extend(row.get("inlineGroups") or [])
    props = row.get("principalProperties") or {}
    return IdentityResult(
        found=True,
        authz_principals=_unique(authz),
        principal_id=principal,
        tenant_id=props.get("tenantId"),
        groups=groups,
        principal_labels=row.get("principalLabels") or [],
        principal_properties=props,
    )


class GraphIdentitySource:
    """Resolve against the bundle's own connection.

    Used by ``source: graph`` and ``source: composite``. For composite the query
    carries a ``USE`` clause naming the identity constituent, which is the only
    difference — the connection is the same one the data query uses.
    """

    def __init__(self, policy: SecurityPolicy, executor):
        from . import mediation
        self._policy = policy
        self._executor = executor
        self._query = mediation.resolve_identity_query(policy)

    def resolve(self, principal: str) -> IdentityResult:
        from . import mediation
        rows = self._executor.run(
            self._query, mediation.security_params(self._policy, principal), read_only=True)
        return _from_row(self._policy, principal, rows[0] if rows else None)

    def sample_principals(self, limit: int = 6) -> list[str]:
        use = (self._policy.identity.identity_graph
               if self._policy.identity.source == SOURCE_COMPOSITE else "")
        rows = self._executor.run(
            _sample_query(self._policy, use),
            {"keys": self._policy.identity.match_keys, "limit": limit}, read_only=True)
        return [r["p"] for r in rows if r.get("p")]

    def close(self) -> None:  # the executor is owned by the bundle
        return


class RemoteGraphIdentitySource:
    """Resolve against a SEPARATE Neo4j connection.

    This is the source that makes the identity graph genuinely relocatable: it
    can be a different Aura instance, a different region, or a directory-owned
    database with its own lifecycle and its own credentials.

    Connection comes from environment variables only, matching the rule that no
    credential appears in a committed file. With the default prefix::

        IDENTITY_NEO4J_URI=neo4j+s://<identity-instance>.databases.neo4j.io
        IDENTITY_NEO4J_USERNAME=neo4j
        IDENTITY_NEO4J_PASSWORD=...
        IDENTITY_NEO4J_DATABASE=neo4j
    """

    def __init__(self, policy: SecurityPolicy, env: Mapping[str, str] | None = None):
        from . import mediation
        self._policy = policy
        self._query = mediation.resolve_identity_query(policy)
        source = os.environ if env is None else env
        prefix = policy.identity.remote_env_prefix.rstrip("_")

        def _read(name: str, default: str = "") -> str:
            return str(source.get(f"{prefix}_{name}", default)).strip()

        self._uri = _read("NEO4J_URI")
        self._username = _read("NEO4J_USERNAME", "neo4j")
        self._password = _read("NEO4J_PASSWORD")
        self._database = _read("NEO4J_DATABASE", "neo4j") or "neo4j"
        if not self._uri:
            raise ToolError(
                f"security.identity.source=remote needs {prefix}_NEO4J_URI (plus "
                f"{prefix}_NEO4J_USERNAME / {prefix}_NEO4J_PASSWORD) in the environment. "
                "Put them in a git-ignored .env, never in bundle.yaml.")
        self._driver: neo4j.Driver | None = None

    def _get_driver(self) -> neo4j.Driver:
        if self._driver is None:
            self._driver = neo4j.GraphDatabase.driver(
                self._uri, auth=(self._username, self._password),
                notifications_min_severity="OFF")
        return self._driver

    def _run(self, query: str, params: dict) -> list[dict]:
        from .yaml_tools import _to_jsonable

        def _work(tx: neo4j.ManagedTransaction):
            result = tx.run(query, params)
            return [{k: _to_jsonable(v) for k, v in record.items()} for record in result]

        try:
            with self._get_driver().session(database=self._database) as session:
                return session.execute_read(_work)
        except neo4j.exceptions.Neo4jError as exc:
            raise ToolError(f"identity source error [{exc.code}]: {exc.message}") from exc
        except neo4j.exceptions.DriverError as exc:
            raise ToolError(f"identity source unreachable at {self._uri}: {exc}") from exc

    def resolve(self, principal: str) -> IdentityResult:
        from . import mediation
        rows = self._run(self._query, mediation.security_params(self._policy, principal))
        return _from_row(self._policy, principal, rows[0] if rows else None)

    def sample_principals(self, limit: int = 6) -> list[str]:
        rows = self._run(_sample_query(self._policy),
                         {"keys": self._policy.identity.match_keys, "limit": limit})
        return [r["p"] for r in rows if r.get("p")]

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None


# --------------------------------------------------------------------------- #
# Registry — the extension point
# --------------------------------------------------------------------------- #

_FACTORIES: dict[str, Callable[[SecurityPolicy, object, Mapping[str, str] | None], IdentitySource]] = {
    SOURCE_GRAPH: lambda policy, executor, env: GraphIdentitySource(policy, executor),
    SOURCE_COMPOSITE: lambda policy, executor, env: GraphIdentitySource(policy, executor),
    SOURCE_REMOTE: lambda policy, executor, env: RemoteGraphIdentitySource(policy, env),
}


def register_identity_source(name: str, factory) -> None:
    """Register a custom identity source.

    ``factory(policy, executor, env) -> IdentitySource``. A source that resolves
    against an external entitlement service would be registered here and selected
    with ``security.identity.source: <name>``; declare it in
    :data:`gateway.bundles.VALID_IDENTITY_SOURCES` as well so manifests validate.
    """
    _FACTORIES[name] = factory


_CACHE: dict[tuple, IdentitySource] = {}


def get_identity_source(config, executor) -> IdentitySource:
    """Build (and memoise) the identity source for a bundle's config."""
    policy = config.security
    key = (config.active_bundle, policy.identity.source, policy.identity.remote_env_prefix)
    if key not in _CACHE:
        factory = _FACTORIES.get(policy.identity.source)
        if factory is None:
            raise ToolError(f"unknown security.identity.source {policy.identity.source!r}")
        _CACHE[key] = factory(policy, executor, config.env_snapshot)
    return _CACHE[key]


def close_identity_sources() -> None:
    for source in _CACHE.values():
        source.close()
    _CACHE.clear()
