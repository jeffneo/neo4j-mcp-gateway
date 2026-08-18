"""Entitlement mediation — an engine capability, not a use case.

When a bundle declares ``security.mode: mediated``, every read it exposes is
composed as::

    authorization prelude   (resolve caller -> authzPrincipals)
  + the query's MATCH part  (curated YAML, or model-generated fragment)
  + entitlement filter      (every variable in scope checked against the caller)
  + final RETURN            (projection/aggregate — runs AFTER filtering)

Two properties matter and are deliberate:

* **Derived protection.** The filter applies to *every* variable in scope, not to
  a list the caller supplied. Security cannot depend on what the model declared.
* **Config-driven.** Labels, relationship types and property names come from the
  bundle's :class:`~gateway.bundles.SecurityPolicy`, so any domain graph can be
  mediated without touching this module.

The single expression where graph-derived identity meets row-level grants is the
``any(principal IN resource[...] WHERE principal IN authz.authzPrincipals)`` test
in :data:`_FILTER`. That is the seam a future path-based grant model would replace.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from fastmcp.exceptions import ToolError

from .bundles import SecurityPolicy

# Reserved query parameters. Callers may not supply these.
P_PRINCIPAL = "__secure_auth_principal"
P_PERM_PROP = "__secure_perm_property"
P_ID_LABELS = "__secure_identity_labels"
P_MATCH_KEYS = "__secure_match_keys"
P_GROUP_KEYS = "__secure_group_name_keys"
P_INLINE_GROUPS = "__secure_inline_group_prop"
P_EVERYONE = "__secure_everyone"
RESERVED_PARAMS = (
    P_PRINCIPAL, P_PERM_PROP, P_ID_LABELS, P_MATCH_KEYS,
    P_GROUP_KEYS, P_INLINE_GROUPS, P_EVERYONE,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# A model-generated fragment must be a read-only MATCH part with no RETURN.
_BLOCKED = re.compile(
    r"\b(return|create|merge|delete|detach\s+delete|set|remove|drop|alter|grant|deny|"
    r"revoke|load\s+csv|periodic\s+commit|use|profile)\b"
)
_BLOCKED_CALL = re.compile(r"\bcall\s+(dbms|db\.|apoc|gds)\.")
# Subquery expressions are an INFERENCE CHANNEL: they test for data that never
# binds to a scope variable, so the entitlement filter never sees it.
_BLOCKED_SUBQUERY = re.compile(r"\b(exists|count|collect|call)\s*\{|\bexists\s*\(")
# A final RETURN must be a bare projection/aggregate.
_BLOCKED_FINAL = re.compile(
    r"\b(match|with|call|create|merge|delete|detach\s+delete|set|remove|drop|alter|"
    r"grant|deny|revoke|load\s+csv|periodic\s+commit|use|profile|unwind)\b"
)

# Resolve the caller into {principalId, tenantId, authzPrincipals}. Everything is
# parameterised except the relationship types, which Cypher cannot parameterise.
_PRELUDE = """CALL {
  MATCH (u:@@ID_LABEL_EXPR@@)
  WHERE u.schemaId IS NULL
    AND any(key IN $@@P_MATCH_KEYS@@
            WHERE u[key] IS NOT NULL
              AND toLower(toString(u[key])) = toLower($@@P_PRINCIPAL@@))
  OPTIONAL MATCH (u)-[:@@GROUP_RELS@@*1..]->(g)
  WHERE g.schemaId IS NULL
  WITH u, collect(DISTINCT head([k IN $@@P_GROUP_KEYS@@ WHERE g[k] IS NOT NULL | g[k]])) AS groupPrincipals
  RETURN {
    principalId: head([k IN $@@P_MATCH_KEYS@@ WHERE u[k] IS NOT NULL | u[k]]),
    tenantId: u.tenantId,
    authzPrincipals: [p IN groupPrincipals
        + coalesce(u[$@@P_INLINE_GROUPS@@], [])
        + [ head([k IN $@@P_MATCH_KEYS@@ WHERE u[k] IS NOT NULL | u[k]]), $@@P_EVERYONE@@ ]
      WHERE p IS NOT NULL]
  } AS authz, u AS caller
}"""

# (a) explicitly protected -> STRICT: must carry an ACL and match.
# (b) everything else in scope -> DERIVED: match if it has an ACL; flow if it does
#     not (reference data). This is what makes filtering independent of the caller.
_FILTER = """WITH @@SCOPE_WITH@@
WHERE all(resource IN [@@PROTECTED@@] WHERE resource IS NULL OR (
    (authz.tenantId IS NULL OR resource.tenantId IS NULL OR resource.tenantId = authz.tenantId)
    AND any(principal IN coalesce(resource[$@@P_PERM_PROP@@], [])
            WHERE principal IN authz.authzPrincipals)
  ))
  AND all(resource IN [@@DERIVED@@] WHERE resource IS NULL
    OR resource[$@@P_PERM_PROP@@] IS NULL
    OR (
      (authz.tenantId IS NULL OR resource.tenantId IS NULL OR resource.tenantId = authz.tenantId)
      AND any(principal IN resource[$@@P_PERM_PROP@@]
              WHERE principal IN authz.authzPrincipals)
    ))"""

# ANCHOR_SAFETY
# -------------
# An anchor RESTRICTS what the tool's match examines, and the entitlement filter
# that follows can only remove rows further. The two error directions are NOT
# symmetric:
#
#   anchor too BROAD   -> extra rows examined, filter removes them.
#                         Correct, merely slower. Cannot leak.
#   anchor too NARROW  -> rows the caller IS entitled to are never matched, and
#                         nothing downstream can restore them. FALSE NEGATIVES.
#
# So an anchor cannot cause a disclosure, but it can silently hide data. The rule
# is therefore: the anchor must reach EVERY route by which a caller may be
# entitled to that variable. In practice this means anchoring belongs on tools
# whose question *is* the anchor ("trades for clients I cover"), not on general
# tools serving several roles with different entitlement routes.
#
# scripts/check_entitlements.py runs every anchored tool a second time WITHOUT the
# anchor and fails if the result sets differ, which is what catches a too-narrow
# anchor in CI.
_ANCHOR_BLOCKED = re.compile(
    r"\b(return|create|merge|delete|set|remove|drop|detach|call|union|with)\b|;"
)


def validate_anchor(variable: str, pattern: str, scope: list[str]) -> None:
    """Check an author-declared anchor before it is interpolated into Cypher."""
    if variable not in scope:
        raise ToolError(f"anchor variable {variable!r} is not in scope {scope}")
    if variable == "caller":
        raise ToolError("'caller' is reserved for the authenticated principal")
    if not _IDENTIFIER.match(variable):
        raise ToolError(f"anchor variable {variable!r} is not a valid identifier")
    if "caller" not in pattern:
        raise ToolError("the anchor pattern must start from (caller)")
    if variable not in pattern:
        raise ToolError(f"the anchor pattern must bind {variable!r}")
    if _ANCHOR_BLOCKED.search(_strip_comments(pattern).lower()):
        raise ToolError("the anchor pattern must be a bare MATCH pattern (no clauses)")


# Standalone identity lookup used by the resolve-identity tool.
RESOLVE_IDENTITY_QUERY = """MATCH (u:@@ID_LABEL_EXPR@@)
WHERE u.schemaId IS NULL
  AND any(key IN $@@P_MATCH_KEYS@@
          WHERE u[key] IS NOT NULL AND toLower(toString(u[key])) = toLower($@@P_PRINCIPAL@@))
OPTIONAL MATCH (u)-[:@@GROUP_RELS@@*1..]->(g)
WHERE g.schemaId IS NULL
WITH u, collect(DISTINCT g) AS groups
RETURN
  labels(u) AS principalLabels,
  properties(u) AS principalProperties,
  coalesce(u[$@@P_INLINE_GROUPS@@], []) AS inlineGroups,
  [g IN groups WHERE g IS NOT NULL | {
    labels: labels(g),
    properties: properties(g),
    name: head([k IN $@@P_GROUP_KEYS@@ WHERE g[k] IS NOT NULL | g[k]])
  }] AS groups
LIMIT 1"""


def _subst(template: str, policy: SecurityPolicy) -> str:
    return (
        template
        .replace("@@P_ID_LABELS@@", P_ID_LABELS)
        .replace("@@P_MATCH_KEYS@@", P_MATCH_KEYS)
        .replace("@@P_PRINCIPAL@@", P_PRINCIPAL)
        .replace("@@P_GROUP_KEYS@@", P_GROUP_KEYS)
        .replace("@@P_INLINE_GROUPS@@", P_INLINE_GROUPS)
        .replace("@@P_EVERYONE@@", P_EVERYONE)
        .replace("@@P_PERM_PROP@@", P_PERM_PROP)
        .replace("@@GROUP_RELS@@", "|".join(policy.identity.group_rels))
        # Label expression, not a parameter: Cypher cannot parameterise labels, and
        # an unlabelled MATCH forces an AllNodesScan that grows with the whole graph.
        .replace("@@ID_LABEL_EXPR@@", "|".join(policy.identity.labels))
    )


def security_params(policy: SecurityPolicy, principal: str) -> dict[str, object]:
    """The reserved parameters every mediated query needs."""
    return {
        P_PRINCIPAL: principal,
        P_PERM_PROP: policy.permissions_property,
        P_ID_LABELS: policy.identity.labels,
        P_MATCH_KEYS: policy.identity.match_keys,
        P_GROUP_KEYS: policy.identity.group_name_keys,
        P_INLINE_GROUPS: policy.identity.inline_group_list,
        P_EVERYONE: policy.principal.everyone,
    }


# --------------------------------------------------------------------------- #
# Principal resolution
# --------------------------------------------------------------------------- #

def impersonation_allowed(policy: SecurityPolicy, env: Mapping[str, str] | None = None) -> bool:
    """Impersonation needs the bundle to allow it, or the env flag to enable it.

    ``env`` is the owning bundle's environment snapshot. With several bundles in
    one process, os.environ only reflects whichever was resolved last, so callers
    should pass ``config.env_snapshot``.
    """
    source = os.environ if env is None else env
    flag = str(source.get("NEO4J_MCP_ALLOW_IMPERSONATION", "")).strip().lower()
    return policy.principal.allow_impersonation or flag in {"true", "1", "yes"}


def resolve_principal(
    policy: SecurityPolicy,
    requested: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Resolve the caller. Returns ``(principal, source)``; raises if unknown.

    A caller-supplied ``requested`` principal is honoured only when impersonation
    is enabled — otherwise anyone could choose whose data they see.
    """
    lookup = os.environ if env is None else env
    principal = source = ""
    for key in policy.principal.env:
        value = str(lookup.get(key, "")).strip()
        if value:
            principal, source = value, f"env:{key}"
            break

    requested = (requested or "").strip()
    if requested:
        if not impersonation_allowed(policy, env):
            raise ToolError(
                "principal override requires security.principal.allow_impersonation "
                "or NEO4J_MCP_ALLOW_IMPERSONATION=true"
            )
        return requested, "impersonation-request"

    if not principal:
        raise ToolError(
            "Unable to resolve the caller's principal. For STDIO clients set one of: "
            + ", ".join(policy.principal.env)
        )
    return principal, source


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #

def normalize_identifiers(values, field: str) -> list[str]:
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


def _strip_comments(query: str) -> str:
    return "\n".join(line.split("//", 1)[0] for line in query.split("\n"))


def validate_fragment(query: str) -> None:
    """Reject anything a model-generated MATCH fragment must not contain."""
    normalized = _strip_comments(query).lower()
    if ";" in normalized:
        raise ToolError("a mediated fragment must be a single statement; semicolons are not allowed")
    if _BLOCKED.search(normalized) or _BLOCKED_CALL.search(normalized):
        raise ToolError("the fragment contains a clause or procedure that is not allowed")
    if _BLOCKED_SUBQUERY.search(normalized):
        raise ToolError(
            "subquery expressions (EXISTS/COUNT/COLLECT/CALL {...}) are not allowed in a mediated "
            "fragment: they can test for data that never enters the authorization filter"
        )


def validate_final_return(final_return: str | None, scope: list[str]) -> str:
    """Default to returning scope; otherwise require a bare RETURN clause."""
    final_return = (final_return or "").strip()
    if not final_return:
        return "RETURN " + ", ".join(scope)
    normalized = _strip_comments(final_return).lower()
    if ";" in normalized:
        raise ToolError("the final RETURN must not contain semicolons")
    if not (normalized.startswith("return ") or normalized == "return"):
        raise ToolError("the final clause must be a RETURN")
    if _BLOCKED_FINAL.search(normalized):
        raise ToolError("the final RETURN contains a clause that is not allowed")
    return final_return


def compose(
    policy: SecurityPolicy,
    match_clause: str,
    scope: list[str],
    final_return: str,
    protect: list[str] | None = None,
    anchor: tuple[str, str] | None = None,
) -> str:
    """Build the full mediated query: prelude + [anchor] + match + filter + return.

    ``anchor`` is an optional ``(variable, pattern)`` pair. When given, the engine
    traverses from the caller to that variable BEFORE running the tool's match, so
    the match starts from what the caller can reach instead of scanning everything
    and discarding. The entitlement filter still runs over the full scope
    afterwards, so an over-broad anchor cannot leak — see ANCHOR_SAFETY below.
    """
    protect = [v for v in (protect or []) if v in scope]
    derived = [v for v in scope if v not in protect]

    if anchor:
        anchor_var, anchor_pattern = anchor
        # DISTINCT matters: several paths may reach the same anchor node (a client
        # covered by two teams the caller belongs to), which would otherwise
        # multiply rows and corrupt any aggregate in the final return.
        inner_scope = [v for v in scope if v != anchor_var]
        body = f"MATCH {anchor_pattern.strip()}\nWITH DISTINCT authz, caller, {anchor_var}"
        if inner_scope:
            body += (
                "\nCALL {\n  WITH " + anchor_var + "\n  " + match_clause.strip()
                + "\n  RETURN " + ", ".join(inner_scope) + "\n}"
            )
    else:
        body = (
            "CALL {\n  WITH authz\n  " + match_clause.strip()
            + "\n  RETURN " + ", ".join(scope) + "\n}"
        )
    filt = (
        _FILTER
        .replace("@@SCOPE_WITH@@", ", ".join(["authz", "caller"] + scope))
        .replace("@@PROTECTED@@", ", ".join(protect))
        .replace("@@DERIVED@@", ", ".join(derived))
    )
    return _subst("\n".join([_PRELUDE, body, filt, final_return]), policy)


def resolve_identity_query(policy: SecurityPolicy) -> str:
    return _subst(RESOLVE_IDENTITY_QUERY, policy)


def prelude_only_query(policy: SecurityPolicy) -> str:
    """The authorization prelude with nothing after it.

    The fixed per-call cost of mediation: resolving the caller and expanding
    their principals. Used by scripts/bench_mediation.py to separate that
    constant overhead from the filter, which scales with rows examined.
    """
    return _subst(_PRELUDE, policy) + "\nRETURN authz.authzPrincipals AS principals"
