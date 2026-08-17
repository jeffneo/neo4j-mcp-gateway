"""Tools the engine registers for any bundle running in ``mediated`` mode.

* ``resolve-identity``    — who is the caller, and what principals do they hold?
* ``secure-read-cypher``  — run a model-generated MATCH fragment inside the
  authorization wrapper. Optional: a bundle can set
  ``security.expose_open_query_tool: false`` to publish only curated tools.

These live in the engine rather than in a bundle because entitlement mediation is
a cross-cutting capability, not a use case. A bundle opts in with config.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool

from . import mediation
from .config import Config
from .yaml_tools import Neo4jExecutor


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


def _impersonation_param(config: Config) -> dict:
    """Expose a `principal` argument only when impersonation is actually enabled."""
    if not mediation.impersonation_allowed(config.security):
        return {}
    return {
        "principal": {
            "type": "string",
            "description": "Run as this principal (test impersonation; enabled for this deployment).",
        }
    }


def build_resolve_identity(config: Config, executor: Neo4jExecutor) -> FunctionTool:
    policy = config.security
    query = mediation.resolve_identity_query(policy)

    async def handler(**kwargs) -> dict:
        principal, source = mediation.resolve_principal(policy, kwargs.get("principal"))
        params = mediation.security_params(policy, principal)
        rows = executor.run(query, params, read_only=True)

        authz = [principal, policy.principal.everyone]
        result: dict = {
            "principal": principal,
            "source": source,
            "identityFound": bool(rows),
            "groups": [],
            "notes": [],
        }
        if rows:
            row = rows[0]
            result["principalLabels"] = row.get("principalLabels")
            result["principalProperties"] = row.get("principalProperties")
            groups = row.get("groups") or []
            result["groups"] = groups
            authz.extend(g.get("name") for g in groups if isinstance(g, dict) and g.get("name"))
            authz.extend(row.get("inlineGroups") or [])
        else:
            result["notes"].append(
                "No matching identity node was found; the caller holds only their own "
                "identity and the everyone principal."
            )
        result["authzPrincipals"] = _unique(authz)
        return result

    return FunctionTool(
        name="resolve-identity",
        description=(
            "Resolve the current caller and expand their entitlement groups into the set of "
            "principals used to filter every read. Call this first to establish who you are "
            "acting as and what you are entitled to see."
        ),
        parameters={
            "type": "object",
            "properties": {**_impersonation_param(config)},
            "additionalProperties": False,
        },
        fn=handler,
    )


def build_secure_read_cypher(config: Config, executor: Neo4jExecutor) -> FunctionTool:
    policy = config.security

    async def handler(**kwargs) -> dict:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            raise ToolError("query is required and cannot be empty")
        mediation.validate_fragment(query)

        # protectedVariables is advisory (strict opt-in); scope is what gets filtered.
        protect = mediation.normalize_identifiers(kwargs.get("protectedVariables"), "protectedVariables")
        returns = mediation.normalize_identifiers(kwargs.get("returnVariables") or [], "returnVariables")
        scope = _unique(protect + returns)
        if not scope:
            raise ToolError(
                "declare the variables your fragment produces via returnVariables "
                "(and optionally protectedVariables for strict checking)"
            )

        final_return = mediation.validate_final_return(kwargs.get("finalReturn"), scope)
        principal, _ = mediation.resolve_principal(policy, kwargs.get("principal"))

        user_params = kwargs.get("params") or {}
        for reserved in mediation.RESERVED_PARAMS:
            if reserved in user_params:
                raise ToolError(f"params cannot include reserved key {reserved!r}")
        params = {**user_params, **mediation.security_params(policy, principal)}

        composed = mediation.compose(policy, query, scope, final_return, protect)
        try:
            rows = executor.run(composed, params, read_only=True)
        except Exception as exc:  # noqa: BLE001 - surface DB/Cypher errors cleanly
            raise ToolError(f"mediated read failed: {exc}") from exc
        return {"principal": principal, "count": len(rows), "records": rows}

    return FunctionTool(
        name="secure-read-cypher",
        description=(
            "Run a read-only Cypher MATCH fragment inside the entitlement authorization "
            "wrapper. The fragment must NOT contain RETURN and may not use "
            "EXISTS/COUNT/COLLECT/CALL subqueries. List the variables it produces in "
            "returnVariables; every one is filtered against the caller's entitlements. Put any "
            "projection or aggregate in finalReturn — it runs AFTER filtering, so counts and "
            "sums reflect only rows the caller may see."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Read-only MATCH fragment; no RETURN."},
                "params": {"type": "object", "description": "Parameters for the fragment.",
                           "additionalProperties": True},
                "returnVariables": {"type": "array", "items": {"type": "string"},
                                    "description": "Variables the fragment produces that must stay in scope."},
                "protectedVariables": {"type": "array", "items": {"type": "string"},
                                       "description": "Optional. Variables to check strictly: each must carry an "
                                                      "access-control list or the row is dropped. Every variable in "
                                                      "scope is filtered regardless of this list."},
                "finalReturn": {"type": "string",
                                "description": "RETURN clause applied after filtering, e.g. "
                                               "RETURN count(DISTINCT t) AS trades."},
                **_impersonation_param(config),
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        fn=handler,
    )


def build_security_tools(config: Config, executor: Neo4jExecutor) -> list[FunctionTool]:
    """Engine tools for a mediated bundle (empty list when the bundle is open)."""
    if not config.security.mediated:
        return []
    tools = [build_resolve_identity(config, executor)]
    if config.security.expose_open_query_tool:
        tools.append(build_secure_read_cypher(config, executor))
    return tools
