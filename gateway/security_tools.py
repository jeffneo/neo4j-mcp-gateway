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
    if not mediation.impersonation_allowed(config.security, config.env_snapshot):
        return {}
    return {
        "principal": {
            "type": "string",
            "description": "Run as this principal (test impersonation; enabled for this deployment).",
        }
    }


def build_resolve_identity(config: Config, executor: Neo4jExecutor, prefix: str = "") -> FunctionTool:
    policy = config.security
    query = mediation.resolve_identity_query(policy)

    async def handler(**kwargs) -> dict:
        principal, source = mediation.resolve_principal(
            policy, kwargs.get("principal"), config.env_snapshot)
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
        name=f"{prefix}resolve-identity",
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


def build_secure_read_cypher(config: Config, executor: Neo4jExecutor, prefix: str = "") -> FunctionTool:
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
        principal, _ = mediation.resolve_principal(
            policy, kwargs.get("principal"), config.env_snapshot)

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
        name=f"{prefix}secure-read-cypher",
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


def _render(nodes: list, rels: list) -> str:
    """A readable one-line rendering of a granting path."""
    parts = []
    for i, node in enumerate(nodes):
        labels = node.get("labels") or []
        name = node.get("name") or "?"
        parts.append(f"({labels[0] if labels else '?'}:{name})")
        if i < len(rels):
            parts.append(f"-[:{rels[i]}]-")
    return "".join(parts)


def build_explain_access(config: Config, executor: Neo4jExecutor, prefix: str = "") -> FunctionTool:
    policy = config.security

    async def handler(**kwargs) -> dict:
        resource_id = str(kwargs.get("resource") or "").strip()
        if not resource_id:
            raise ToolError("resource is required")
        principal, _ = mediation.resolve_principal(
            policy, kwargs.get("principal"), config.env_snapshot)

        wanted = str(kwargs.get("label") or "").strip()
        labels = [wanted] if wanted else list(policy.resource_keys)
        unknown = [x for x in labels if x not in policy.resource_keys]
        if unknown:
            raise ToolError(
                f"no resource_keys entry for {unknown[0]!r}; declare it in bundle.yaml "
                f"(known: {', '.join(policy.resource_keys) or 'none'})")

        for label in labels:
            params = {
                **mediation.security_params(policy, principal),
                mediation.P_RESOURCE_ID: resource_id,
            }
            rows = executor.run(
                mediation.explain_query(policy, label, policy.resource_keys[label]),
                params, read_only=True)
            if not rows:
                continue

            row = rows[0]
            reasons = []
            for m in row.get("matched") or []:
                grant = policy.grants[m["idx"]]
                reasons.append({
                    "reason": grant.reason or f"matched a declared grant on {grant.label}",
                    "path": _render(m.get("nodes") or [], m.get("rels") or []),
                })
            acl = row.get("aclMatches") or []
            granted = bool(reasons) if policy.grant_model == "path" else bool(reasons or acl)

            if not granted:
                break  # fall through to the indistinguishable answer below

            result = {
                "principal": principal, "resource": resource_id, "label": label,
                "granted": True, "grantedBy": reasons,
            }
            if acl and policy.grant_model != "path":
                result["accessControlList"] = {
                    "matchedEntries": acl,
                    "note": "granted by a materialised access-control entry, not a path",
                }
            if not reasons and acl:
                result["note"] = ("This row is reachable only through a materialised entry. "
                                  "Role-based routes are not expressible as paths.")
            return result

        # Deliberately identical whether the row is absent or merely unreadable.
        return {
            "principal": principal, "resource": resource_id, "granted": False,
            "grantedBy": [],
            "note": ("No grant reaches this resource for this caller. This answer is the "
                     "same whether the resource does not exist or exists and is not "
                     "readable — an explanation must not confirm the existence of "
                     "something the caller may not see."),
        }

    return FunctionTool(
        name=f"{prefix}explain-access",
        description=(
            "Explain WHY the caller can read a specific record: which entitlement rule "
            "granted it and the path through the graph that satisfies it. Answers "
            "\"why can I see this?\" for audit and support. If no rule grants access the "
            "answer is the same whether the record is absent or merely unreadable."
        ),
        parameters={
            "type": "object",
            "properties": {
                "resource": {"type": "string",
                             "description": "Identifier of the record, e.g. a tradeId."},
                "label": {"type": "string",
                          "description": "Optional record type to narrow the search."},
                **_impersonation_param(config),
            },
            "required": ["resource"],
            "additionalProperties": False,
        },
        fn=handler,
    )


def build_security_tools(
    config: Config, executor: Neo4jExecutor, prefix: str = ""
) -> list[FunctionTool]:
    """Engine tools for a mediated bundle (empty list when the bundle is open).

    ``prefix`` namespaces the tools when several bundles share one gateway.
    """
    if not config.security.mediated:
        return []
    tools = [build_resolve_identity(config, executor, prefix)]
    if config.security.resource_keys:
        tools.append(build_explain_access(config, executor, prefix))
    if config.security.expose_open_query_tool:
        tools.append(build_secure_read_cypher(config, executor, prefix))
    return tools
