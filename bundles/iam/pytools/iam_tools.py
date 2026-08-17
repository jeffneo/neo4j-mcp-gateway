"""IAM code-backed tools: resolve-identity and secure-read-cypher.

Adapted from the IAM-aware Neo4j MCP fork (Go) to this gateway's Python pattern.
The engine calls ``build_tools(ctx)`` and registers the returned tools verbatim
(no ``usecase_`` prefix), so they appear as ``resolve-identity`` and
``secure-read-cypher`` — the names the fork and its prompts expect.

Security model: a user's effective principals are their username, ``'everyone'``,
their ``AdGroupList``, and any groups reachable via ``MEMBER_OF*``. A row is
readable only if one of its ``Permissions.Read`` entries is in that set. Raw
``read-cypher`` is hidden by the bundle (see bundle.yaml) so it can't bypass this.
"""

from __future__ import annotations

import json
import os
import re

from fastmcp.exceptions import ToolError
from fastmcp.tools.function_tool import FunctionTool

from gateway.pytools import ToolContext

SECURE_AUTH_PRINCIPAL_PARAM = "__secure_auth_principal"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Blocked in a generated fragment (it must be a read-only MATCH fragment, no RETURN).
_FRAGMENT_BLOCKED = re.compile(
    r"\b(return|create|merge|delete|detach\s+delete|set|remove|drop|alter|grant|deny|"
    r"revoke|load\s+csv|periodic\s+commit|use|profile)\b"
)
_FRAGMENT_BLOCKED_CALL = re.compile(r"\bcall\s+(dbms|db\.|apoc|gds)\.")
# Blocked in finalReturn (must be a bare RETURN projection/aggregate).
_FINAL_BLOCKED = re.compile(
    r"\b(match|with|call|create|merge|delete|detach\s+delete|set|remove|drop|alter|"
    r"grant|deny|revoke|load\s+csv|periodic\s+commit|use|profile|unwind)\b"
)

# Identity resolution query (same shape as the fork's resolveIdentityQuery).
_RESOLVE_IDENTITY_QUERY = """
MATCH (u)
WHERE u.schemaId IS NULL
  AND any(label IN labels(u) WHERE label IN ['User', 'Principal'])
  AND any(key IN ['username', 'email', 'mail', 'userPrincipalName', 'upn', 'name', 'id']
          WHERE u[key] IS NOT NULL AND toLower(toString(u[key])) = toLower($principal))
OPTIONAL MATCH (u)-[:MEMBER_OF|MEMBER_OF_GROUP|IN_GROUP|HAS_GROUP*1..]->(g)
WHERE g.schemaId IS NULL
WITH u, collect(DISTINCT g) AS groups
RETURN
  labels(u) AS principalLabels,
  properties(u) AS principalProperties,
  coalesce(u.AdGroupList, []) AS adGroupList,
  [g IN groups WHERE g IS NOT NULL | {
    labels: labels(g),
    properties: properties(g),
    name: coalesce(g.name, g.group, g.displayName, g.email, g.mail, g.id)
  }] AS groups
LIMIT 1
"""

# Authorization wrapper composed around the generated fragment (token-substituted
# to avoid f-string/format brace escaping in a Cypher body full of {} and []).
_SECURE_TEMPLATE = """
CALL {
  MATCH (u)
  WHERE u.schemaId IS NULL
    AND any(label IN labels(u) WHERE label IN ['User', 'Principal'])
    AND any(key IN ['username', 'email', 'mail', 'userPrincipalName', 'upn', 'name', 'id']
            WHERE u[key] IS NOT NULL AND toLower(toString(u[key])) = toLower($@@AUTHPARAM@@))
  OPTIONAL MATCH (u)-[:MEMBER_OF|MEMBER_OF_GROUP|IN_GROUP|HAS_GROUP*1..]->(g)
  WHERE g.schemaId IS NULL
  WITH u,
       collect(DISTINCT coalesce(g.name, g.group, g.displayName, g.email, g.mail, g.id)) AS groupPrincipals
  RETURN {
    principalId: coalesce(u.id, u.email, u.userPrincipalName, u.upn, u.name),
    tenantId: u.tenantId,
    authzPrincipals: [p IN groupPrincipals + coalesce(u.AdGroupList, []) + [
      coalesce(u.username, u.email, u.userPrincipalName, u.upn, u.name, u.id),
      'everyone'
    ] WHERE p IS NOT NULL]
  } AS authz
}
CALL {
  WITH authz
  @@FRAGMENT@@
  RETURN @@SCOPE_JOIN@@
}
WITH @@SCOPE_WITH@@
WHERE all(resource IN [@@PROTECTED@@] WHERE resource IS NULL OR (
  (authz.tenantId IS NULL OR resource.tenantId IS NULL OR resource.tenantId = authz.tenantId)
  AND any(principal IN coalesce(resource['Permissions.Read'], [])
          WHERE principal IN authz.authzPrincipals)
))
@@FINAL@@"""


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _strip_line_comments(query: str) -> str:
    out = []
    for line in query.split("\n"):
        idx = line.find("//")
        out.append(line[:idx] if idx >= 0 else line)
    return "\n".join(out)


def _impersonation_allowed() -> bool:
    return os.getenv("NEO4J_MCP_ALLOW_IMPERSONATION", "").strip().lower() in {"true", "1", "yes"}


def _resolve_env_principal() -> tuple[str, str]:
    """Resolve the principal from the environment (stdio clients). Returns (principal, source)."""
    for key in ("NEO4J_MCP_PRINCIPAL", "NEO4J_MCP_AUTH_SUBJECT", "USER_EMAIL"):
        value = os.getenv(key, "").strip()
        if value:
            return value, f"env:{key}"
    return "", ""


def _apply_impersonation(base: str, source: str, requested: str | None) -> tuple[str, str]:
    """Apply an optional principal override, enforcing the impersonation flag."""
    requested = (requested or "").strip()
    if not requested:
        return base, source
    if not _impersonation_allowed():
        raise ToolError("principal override requires NEO4J_MCP_ALLOW_IMPERSONATION=true")
    return requested, "impersonation-request"


def _normalize_identifiers(values, field: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        value = str(value).strip()
        if not value:
            continue
        if not _IDENTIFIER.match(value):
            raise ToolError(f"{field} contains invalid Cypher identifier {value!r}")
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _unique(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = (value or "").strip() if isinstance(value, str) else value
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


# --------------------------------------------------------------------------- #
# resolve-identity
# --------------------------------------------------------------------------- #

def _build_resolve_identity(ctx: ToolContext) -> FunctionTool:
    async def handler(**kwargs) -> dict:
        base, source = _resolve_env_principal()
        if not base:
            raise ToolError(
                "Unable to resolve principal. In STDIO clients (e.g. Claude Desktop), "
                "set NEO4J_MCP_PRINCIPAL to the user's stable identity."
            )
        principal, source = _apply_impersonation(base, source, kwargs.get("principal"))

        rows = ctx.executor.run(_RESOLVE_IDENTITY_QUERY, {"principal": principal}, read_only=True)

        authz = [principal, "everyone"]
        result: dict = {
            "principal": principal,
            "source": source,
            "iamNodeFound": bool(rows),
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
            authz.extend(row.get("adGroupList") or [])
        else:
            result["notes"].append("No matching :User or :Principal node was found in the IAM graph.")
        result["authzPrincipals"] = _unique(authz)
        return result

    return FunctionTool(
        name="resolve-identity",
        description=(
            "Resolve the current authenticated principal and expand its IAM group "
            "memberships (via MEMBER_OF and AdGroupList) into the authzPrincipals set "
            "used by secure-read-cypher. A principal override requires "
            "NEO4J_MCP_ALLOW_IMPERSONATION=true."
        ),
        parameters={
            "type": "object",
            "properties": {
                "principal": {
                    "type": "string",
                    "description": "Optional principal to resolve for test impersonation.",
                }
            },
            "additionalProperties": False,
        },
        fn=handler,
    )


# --------------------------------------------------------------------------- #
# secure-read-cypher
# --------------------------------------------------------------------------- #

def _validate_fragment(query: str) -> None:
    normalized = _strip_line_comments(query).lower()
    if ";" in normalized:
        raise ToolError("secure-read-cypher accepts exactly one fragment; semicolons are not allowed")
    if _FRAGMENT_BLOCKED.search(normalized) or _FRAGMENT_BLOCKED_CALL.search(normalized):
        raise ToolError("generated query fragment contains a clause or procedure that is not allowed")


def _secure_final_return(final_return: str | None, return_vars: list[str]) -> str:
    final_return = (final_return or "").strip()
    if not final_return:
        return "RETURN " + ", ".join(return_vars)
    normalized = _strip_line_comments(final_return).lower()
    if ";" in normalized:
        raise ToolError("finalReturn must not contain semicolons")
    if not (normalized.startswith("return ") or normalized == "return"):
        raise ToolError("finalReturn must be a RETURN clause")
    if _FINAL_BLOCKED.search(normalized):
        raise ToolError("finalReturn contains a clause that is not allowed")
    return final_return


def _build_secure_query(fragment: str, protected: list[str], scope: list[str], final_return: str) -> str:
    return (
        _SECURE_TEMPLATE
        .replace("@@AUTHPARAM@@", SECURE_AUTH_PRINCIPAL_PARAM)
        .replace("@@FRAGMENT@@", fragment)
        .replace("@@SCOPE_JOIN@@", ", ".join(scope))
        .replace("@@SCOPE_WITH@@", ", ".join(["authz"] + scope))
        .replace("@@PROTECTED@@", ", ".join(protected))
        .replace("@@FINAL@@", final_return)
    )


def _build_secure_read_cypher(ctx: ToolContext) -> FunctionTool:
    async def handler(**kwargs) -> dict:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            raise ToolError("query is required and cannot be empty")
        _validate_fragment(query)

        protected = _normalize_identifiers(kwargs.get("protectedVariables"), "protectedVariables")
        if not protected:
            raise ToolError("protectedVariables must list at least one returned node variable to authorize")

        return_vars = kwargs.get("returnVariables") or protected
        return_vars = _normalize_identifiers(return_vars, "returnVariables")
        scope = _unique(protected + return_vars)

        final_return = _secure_final_return(kwargs.get("finalReturn"), return_vars)

        base, source = _resolve_env_principal()
        if not base:
            raise ToolError(
                "Unable to resolve principal. Set NEO4J_MCP_PRINCIPAL for STDIO clients."
            )
        principal, _ = _apply_impersonation(base, source, kwargs.get("principal"))

        user_params = kwargs.get("params") or {}
        if SECURE_AUTH_PRINCIPAL_PARAM in user_params:
            raise ToolError(f"params cannot include reserved key {SECURE_AUTH_PRINCIPAL_PARAM!r}")
        params = dict(user_params)
        params[SECURE_AUTH_PRINCIPAL_PARAM] = principal

        wrapped = _build_secure_query(query, protected, scope, final_return)
        try:
            rows = ctx.executor.run(wrapped, params, read_only=True)
        except Exception as exc:  # noqa: BLE001 - surface DB/Cypher errors cleanly
            raise ToolError(f"secure read failed: {exc}") from exc

        return {"principal": principal, "count": len(rows), "records": rows}

    return FunctionTool(
        name="secure-read-cypher",
        description=(
            "Run a generated read-only Cypher MATCH fragment inside an IAM authorization "
            "wrapper. The fragment must NOT contain RETURN. Pass protectedVariables (node "
            "variables to authorize, e.g. [\"j\"]) and an optional finalReturn for the "
            "projection/aggregate, which runs AFTER IAM filtering."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Read-only MATCH fragment; no RETURN."},
                "params": {"type": "object", "description": "Parameters for the fragment.", "additionalProperties": True},
                "protectedVariables": {"type": "array", "items": {"type": "string"},
                                       "description": "Node variables that must pass IAM checks."},
                "returnVariables": {"type": "array", "items": {"type": "string"},
                                    "description": "Variables kept in scope for the final return. Defaults to protectedVariables."},
                "finalReturn": {"type": "string",
                                "description": "RETURN clause applied after IAM filtering, e.g. RETURN count(DISTINCT j) AS n."},
                "principal": {"type": "string", "description": "Optional impersonation principal (requires NEO4J_MCP_ALLOW_IMPERSONATION=true)."},
            },
            "required": ["query", "protectedVariables"],
            "additionalProperties": False,
        },
        fn=handler,
    )


def build_tools(ctx: ToolContext) -> list[FunctionTool]:
    return [_build_resolve_identity(ctx), _build_secure_read_cypher(ctx)]
