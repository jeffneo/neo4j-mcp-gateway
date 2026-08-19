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

from .bundles import SOURCE_COMPOSITE, SOURCE_GRAPH, SOURCE_REMOTE, SecurityPolicy

# Reserved query parameters. Callers may not supply these.
P_PRINCIPAL = "__secure_auth_principal"
P_PERM_PROP = "__secure_perm_property"
P_ID_LABELS = "__secure_identity_labels"
P_MATCH_KEYS = "__secure_match_keys"
P_GROUP_KEYS = "__secure_group_name_keys"
P_INLINE_GROUPS = "__secure_inline_group_prop"
P_EVERYONE = "__secure_everyone"
P_DISPLAY_KEYS = "__secure_display_keys"
P_RESOURCE_ID = "__secure_resource_id"
# Carries the pre-resolved authz map when identity is resolved out of band
# (security.identity.source: remote).
P_AUTHZ = "__secure_authz"
RESERVED_PARAMS = (
    P_PRINCIPAL, P_PERM_PROP, P_ID_LABELS, P_MATCH_KEYS,
    P_GROUP_KEYS, P_INLINE_GROUPS, P_EVERYONE, P_DISPLAY_KEYS, P_RESOURCE_ID,
    P_AUTHZ,
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
# parameterised except the identity labels and group relationship types, which are
# interpolated from bundle config — see DYNAMIC_TYPES below for why, since Cypher
# 5.26+ *can* express both dynamically and the obvious hardening is unsafe here.
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
    attrs: @@CALLER_ATTRS@@,
    authzPrincipals: [p IN groupPrincipals
        + coalesce(u[$@@P_INLINE_GROUPS@@], [])
        + [ head([k IN $@@P_MATCH_KEYS@@ WHERE u[k] IS NOT NULL | u[k]]), $@@P_EVERYONE@@ ]
      WHERE p IS NOT NULL]
  } AS authz, u AS caller
}"""

# SEPARATED IDENTITY
# ------------------
# Two preludes for the case where identity does NOT live beside the data. Neither
# binds `caller`: a composite database refuses to import entity values across a
# USE boundary (22N16), and a remote source has no caller node in this database at
# all. Manifest validation therefore forbids path grants and anchors for these
# sources — see SEPARATION_TRADEOFFS in gateway/identity_sources.py.
#
# What IS preserved is the part that matters: `authz` has the identical shape, so
# the entitlement filter, the final RETURN and the whole conformance harness are
# unchanged from the co-located case.

# composite — still ONE statement and one transaction, joined by USE. Values may
# cross a constituent boundary even though entities may not, and `authz` is a map
# of scalars and lists.
_PRELUDE_COMPOSITE = """CALL {
  USE @@IDENTITY_GRAPH@@
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
    attrs: @@CALLER_ATTRS@@,
    authzPrincipals: [p IN groupPrincipals
        + coalesce(u[$@@P_INLINE_GROUPS@@], [])
        + [ head([k IN $@@P_MATCH_KEYS@@ WHERE u[k] IS NOT NULL | u[k]]), $@@P_EVERYONE@@ ]
      WHERE p IS NOT NULL]
  } AS authz
}"""

# remote — the principals were resolved on another connection before this query
# was sent, so the prelude is a parameter binding. The engine builds that map; it
# is never accepted from a tool caller, because P_AUTHZ is a reserved parameter.
_PRELUDE_REMOTE = """WITH $@@P_AUTHZ@@ AS authz"""


# CALLER_ATTRIBUTES
# -----------------
# A set of principal names answers "is the caller one of these?". It cannot answer
# "is the caller senior enough?", because an ordering is not a membership test.
# Encoding a threshold as membership means one principal per rank, re-issued on
# every promotion — the materialisation problem this engine exists to avoid.
#
# So the prelude also lifts a declared handful of the caller's own properties into
# `authz.attrs`, and a rule compares them:
#
#     where: "authz.attrs.rankLevel >= 5"
#
# The map is built with LITERAL keys rather than `u[$param]` for two reasons. A
# non-constant property key returns NULL across a composite `USE` boundary (see
# COMPOSITE_PROPERTY_ACCESS), which would make every threshold silently false. And
# core Cypher cannot construct a map from a parameterised key list at all. The
# names are bundle config validated as identifiers at manifest load, so this is
# the same trust level as the identity labels two lines below.
#
# THREE PROPERTIES WORTH NAMING, because they are why this beats a JOIN:
#
#   Read once, not per row. The attributes are resolved in the prelude, so a
#   threshold on a million rows costs one property read.
#
#   Crosses the boundary as a VALUE. Unlike the caller NODE — which a composite
#   database refuses to export — a scalar travels. Every rank threshold works
#   unchanged under `source: composite` and `source: remote`.
#
#   NULL fails closed in a grant, OPEN in a denial. `NULL >= 5` is NULL, so a
#   missing attribute withholds a grant (visible: somebody's rows disappear) and
#   withdraws a barrier (invisible: nothing is missing from anyone). Hence the
#   load-time check on declared names, and hence the conformance invariant that
#   every caller actually carries every deciding attribute.
def caller_attrs_map(policy: SecurityPolicy) -> str:
    attrs = policy.identity.caller_attributes
    if not attrs:
        return "{}"
    return "{" + ", ".join(f"{a}: u.{a}" for a in attrs) + "}"


def prelude_for(policy: SecurityPolicy) -> str:
    if policy.identity.source == SOURCE_COMPOSITE:
        return _PRELUDE_COMPOSITE
    if policy.identity.source == SOURCE_REMOTE:
        return _PRELUDE_REMOTE
    return _PRELUDE


def binds_caller(policy: SecurityPolicy) -> bool:
    """Whether the caller node is reachable from the data query."""
    return policy.identity.source == SOURCE_GRAPH


# The entitlement test for ONE variable. Built per variable rather than as a list
# comprehension because path grants dispatch on the variable's labels.
#
#   strict  (named in `protect`) — must be explicitly granted; ungoverned denied.
#   derived (everything else in scope) — granted if governed, otherwise flows as
#           reference data so joins through Clients, teams and desks still work.
_TENANT = ("(authz.tenantId IS NULL OR {v}.tenantId IS NULL "
           "OR {v}.tenantId = authz.tenantId)")


# COMPOSITE_PROPERTY_ACCESS
# -------------------------
# The ACL is read with a CONSTANT property key (`n.`Permissions.Read``) rather
# than a parameterised one (`n[$param]`), because a non-constant key silently
# returns NULL for any entity exported across a composite `USE` boundary —
# verified on 2025.10.1; see neo4j-issue-composite-dynamic-property/.
#
# That defect is not merely a wrong answer here, it is a LEAK: a strict variable
# would see NULL and deny (fail closed), but a derived variable treats "no ACL"
# as reference data and would let every row through (fail OPEN).
#
# The property name is bundle config validated at manifest load, so interpolating
# it is the same trust level as the identity labels and group relationship types.
def _perm(policy: SecurityPolicy, var: str) -> str:
    return f"{var}.`{policy.permissions_property}`"


def _property_test(policy: SecurityPolicy, var: str, strict: bool) -> str:
    tenant = _TENANT.format(v=var)
    acl = _perm(policy, var)
    matches = (f"any(principal IN coalesce({acl}, []) "
               f"WHERE principal IN authz.authzPrincipals)")
    if strict:
        return f"({var} IS NULL OR ({tenant} AND {matches}))"
    return (f"({var} IS NULL OR {acl} IS NULL "
            f"OR ({tenant} AND {matches}))")


def _bind(pattern: str, var: str) -> str:
    """Point a grant pattern's `resource` at the variable being tested."""
    return re.sub(r"\bresource\b", var, pattern.strip())


# GRANT_SPLITTING
# ---------------
# A grant is a traversal from the caller to the row. When identity lives in a
# different constituent of a composite database, that traversal cannot run as
# one pattern — a relationship never spans two graphs. But a *node* can exist in
# both, which is Neo4j's documented "proxy node" pattern, and the traversal can
# be CUT there:
#
#   (caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(resource)
#   └──── identity constituent ─────┘ └────────────── data constituent ──────────────┘
#                        ^ cut here: AdGroup exists in both, and its NAME crosses
#                          as a value in authz.authzPrincipals
#
# The prelude already resolves the caller to that list of names, so the data-side
# suffix is re-rooted at a proxy node identified by a value we already hold. What
# runs in the data constituent is still a real query-time traversal, so the
# no-materialisation and no-staleness properties survive.
#
# Where to cut is derived, not declared: walk from `caller` and consume the
# leading run of IDENTITY-side relationship types (``identity.group_rels`` —
# MEMBER_OF and friends, the edges that live in the identity graph). The node
# where that run ends is the cut point.
#
#   * cut after a group hop  -> bind the proxy by name against authzPrincipals
#   * cut at the caller      -> bind the caller's own proxy by principalId, which
#                               is how (caller)-[:LOGGED]->(resource) survives
#
# THE FAILURE MODE, and why this validates at load: if an identity-side
# relationship appears AFTER the cut, the suffix would run in the data
# constituent where those edges do not exist. It would match nothing and deny
# silently — a false negative, the error direction that hides data rather than
# leaking it, and the one hardest to notice. Such a grant is rejected outright.
_PATTERN_TOKEN = re.compile(r"\([^)]*\)|<?-\[[^\]]*\]->?")
_CUT = "__secure_cut"


class GrantSplitError(ValueError):
    """A grant cannot be evaluated with identity in a separate constituent."""


def _rel_types(token: str) -> list[str]:
    inner = token[token.index("[") + 1: token.index("]")]
    if ":" not in inner:
        return []
    types = inner.split(":", 1)[1].split("*", 1)[0]
    return [t.strip() for t in types.split("|") if t.strip()]


def _node_parts(token: str) -> tuple[str, str]:
    """Split a node token into ``(variable, label_expression)``; either may be ''."""
    inner = token[1:-1].strip()
    if ":" in inner:
        var, label = inner.split(":", 1)
        return var.strip(), label.strip()
    return inner, ""


def _rebind_node(token: str, labels: list[str]) -> str:
    """Turn a node token into the bound cut variable, keeping its label if any."""
    _, label = _node_parts(token)
    if not label:
        label = "|".join(labels)
    return f"({_CUT}:{label})" if label else f"({_CUT})"


# PROPERTY_CUT
# ------------
# The node cut needs a proxy node on the data side, because the traversal has to
# land somewhere. But a boundary is often already recorded as a PROPERTY: the
# covering team is written on the Client, the author's address on the Interaction.
# Where that is true the cut can go one node further and compare the property
# instead, which removes the proxy nodes entirely and drops a hop:
#
#   node cut      (cut:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(resource)
#                 WHERE cut.name IN authz.authzPrincipals
#   property cut  (c:Client)<-[:FOR_CLIENT]-(resource)
#                 WHERE c.coverageTeam IN authz.authzPrincipals
#
# Declared per label under ``security.identity.boundary_properties``. Declaring it
# IS the opt-in; a grant whose boundary lands on a declared label uses it.
#
# THE HAZARD, and why the conformance suite must cover both: the property and the
# relationship are two recordings of the same fact, and they can disagree. A
# Client whose ``coverageTeam`` was updated without rewriting ``COVERED_BY``
# (or the reverse) makes the two cuts return different rows. Nothing here can
# detect that from the pattern alone, so a bundle using a property cut should
# keep a ``differential:`` case proving the two agree on real data.
def _cut_predicate(var: str, prop: str, by_caller: bool) -> str:
    if by_caller:
        return f"{var}.`{prop}` = authz.principalId"
    return f"{var}.`{prop}` IN authz.authzPrincipals"


def split_grant(policy: SecurityPolicy, via: str, resource_label: str = "",
                keep_terminal_node: bool = False) -> tuple[str, str]:
    """Cut a grant pattern at the identity/data boundary.

    Returns ``(data_side_pattern, cut_predicate)``; the pattern is ``''`` when the
    boundary property sits on the resource itself, so no traversal is needed at
    all and the predicate applies directly to the row. Raises
    :class:`GrantSplitError` when the pattern cannot be cut safely.
    """
    via = via.strip()
    compact = re.sub(r"\s+", "", via)
    tokens = _PATTERN_TOKEN.findall(compact)
    # Re-joining must reproduce the pattern exactly. If it does not, the tokenizer
    # met something it does not model (a quantified path pattern, an inline WHERE)
    # and the cut would be computed from an incomplete reading of the traversal.
    if not tokens or "".join(tokens) != compact:
        raise GrantSplitError(
            f"pattern {via!r} is not a simple node/relationship chain, so the "
            "identity/data cut point cannot be determined")
    if "caller" not in tokens[0]:
        raise GrantSplitError(
            f"pattern {via!r} must start at (caller) to be split; write it left to right "
            "from the caller")

    group_rels = set(policy.identity.group_rels)
    cut, i = 0, 1
    while i < len(tokens) - 1:
        types = _rel_types(tokens[i])
        if types and all(t in group_rels for t in types):
            cut, i = i + 1, i + 2
        else:
            break

    data_tokens = tokens[cut:]
    for token in data_tokens:
        if token.startswith(("-", "<")):
            leftover = [t for t in _rel_types(token) if t in group_rels]
            if leftover:
                raise GrantSplitError(
                    f"pattern {via!r} uses identity relationship {leftover[0]!r} after the "
                    "identity/data boundary. Those edges live in the identity constituent, so "
                    "the check would match nothing and deny silently. Reorder the pattern so "
                    "all identity hops come first, or use security.identity.source=graph")
    if len(data_tokens) < 3:
        raise GrantSplitError(
            f"pattern {via!r} has no data-side traversal left after the identity hops, so "
            "there is nothing for the data constituent to check")

    # Property cut: skip the proxy node entirely and compare a property on the
    # node one hop further in. See PROPERTY_CUT above.
    boundary = policy.identity.boundary_properties
    if boundary and len(tokens) > cut + 2:
        target = tokens[cut + 2]
        var, label = _node_parts(target)
        # The resource's label is declared on the grant, not written in the
        # pattern, so fall back to it for the terminal node.
        if not label and var == "resource":
            label = resource_label
        prop = boundary.get(label.split("|")[0].split("&")[0].strip()) if label else None
        if prop:
            rest = tokens[cut + 2:]
            if len(rest) == 1:
                # The boundary property sits on the terminal node itself, so there
                # is no traversal left. For a grant that is ideal: the variable is
                # already bound, so the test collapses to a bare comparison. An
                # ANCHOR still needs something to MATCH, so it asks for the node
                # pattern back — a label scan plus a property predicate, which is
                # exactly the anchor you would hand-write.
                bound_var = var or "resource"
                if keep_terminal_node:
                    node = f"({bound_var}:{label})" if label else f"({bound_var})"
                    return node, _cut_predicate(bound_var, prop, cut == 0)
                return "", _cut_predicate(bound_var, prop, cut == 0)
            bound_var = var or _CUT
            bound = f"({bound_var}:{label})" if label else f"({bound_var})"
            return (bound + "".join(rest[1:]),
                    _cut_predicate(bound_var, prop, cut == 0))

    if cut == 0:
        bound = _rebind_node(tokens[0], policy.identity.labels)
        predicate = (f"any(k IN ${P_MATCH_KEYS} "
                     f"WHERE {_CUT}[k] = authz.principalId)")
    else:
        bound = _rebind_node(tokens[cut], [])
        predicate = (f"any(k IN ${P_GROUP_KEYS} "
                     f"WHERE {_CUT}[k] IN authz.authzPrincipals)")
    return bound + "".join(data_tokens[1:]), predicate


def split_anchor(policy: SecurityPolicy, variable: str, pattern: str) -> tuple[str, str]:
    """Cut an anchor pattern at the identity/data boundary.

    An anchor is a traversal from the caller, exactly like a grant, so it splits
    by the same rule. The one extra requirement is that the anchor's own variable
    must survive the cut: if it sits at the cut point it would be replaced by the
    bound proxy variable and the tool's match would lose its starting point.
    """
    data_pattern, predicate = split_grant(policy, pattern, keep_terminal_node=True)
    if not re.search(rf"\b{re.escape(variable)}\b", data_pattern):
        raise GrantSplitError(
            f"anchor variable {variable!r} is on the identity side of the split, so the data "
            "query has nothing to anchor on. Anchor on a node that lives in the data database")
    return data_pattern, predicate


def rule_test(policy: SecurityPolicy, rule, var: str) -> str:
    """The boolean test for one grant or denial against ``var``.

    Three shapes, and a rule may combine the first two:

    * ``via`` only    — pure reachability: is there a path from the caller?
    * ``where`` only  — pure row condition, no traversal at all.
    * both            — the condition is evaluated inside the traversal.

    Co-located, ``via`` is the pattern as authored. With identity separated it is
    the data-side half, re-rooted at a proxy — see GRANT_SPLITTING. Same
    semantics either way; only the starting point moves from the caller NODE to a
    caller-derived VALUE.

    THE AUTHOR'S PREDICATE IS ALWAYS PARENTHESISED, and this is a security fix
    rather than tidiness. `AND` binds tighter than `OR` in Cypher, so composing an
    unbracketed predicate that contains a top-level `OR`:

        WHERE <cut predicate> AND resource.notional <= 50000000
                              OR authz.attrs.rankLevel >= 5

    parses as `(<cut> AND notional) OR rank`, and for a caller who clears the rank
    bar the whole subquery collapses to `true` — DISCARDING the predicate that ties
    the pattern to this caller. That is a disclosure, not a wrong count: it was
    found by running the conformance suite under `identity.source: remote`, where a
    managing director in an unrelated business unit was handed every trade in the
    firm. `where: "a OR b"` is a completely reasonable thing to author, so the
    engine brackets it rather than asking authors to.
    """
    cond = f"({_bind(rule.where, var)})" if rule.where else ""
    if not rule.via:
        # No traversal: the rule is a statement about the row itself.
        return f"({cond})"

    if policy.identity.source == SOURCE_GRAPH:
        inner = f"MATCH {_bind(rule.via, var)}"
        return f"EXISTS {{ {inner}{f' WHERE {cond}' if cond else ''} }}"

    pattern, predicate = split_grant(policy, rule.via, rule.label)
    predicate = _bind(predicate, var)
    if cond:
        predicate = f"{predicate} AND {cond}"
    if not pattern:
        # Property cut landing on the row itself — a bare comparison, no subquery.
        return f"({predicate})"
    return f"EXISTS {{ MATCH {_bind(pattern, var)} WHERE {predicate} }}"


# DENIALS
# -------
# A denial is evaluated exactly like a grant and then inverted. Two properties
# make it a security control rather than a convenience:
#
#   DENY WINS. The test is `(granted) AND NOT (denied)`, so a matching denial
#   removes the row whether or not a grant also matched. "Granted, then withdrawn"
#   is a different fact from "never granted" — a restricted list revokes access
#   someone genuinely had, and the audit story differs accordingly.
#
#   A DENIAL THAT DOES NOT MATCH DOES NOT FIRE — including when its predicate
#   evaluates to NULL. An earlier version of this treated NULL as "deny", on the
#   reasoning that an undecidable revocation should revoke. That is wrong in
#   Cypher's three-valued logic, and unusably so: an ABSENT property yields NULL,
#   so `where: "resource.restricted = true"` denied every row whose property was
#   simply not set — which is every unrestricted row. Absence is not ambiguity.
#   Each rule is therefore wrapped in `coalesce(..., false)`.
#
#   The consequence for authors: where absence SHOULD deny, say so in the
#   predicate — `coalesce(resource.clearance, 0) < 3`, not `resource.clearance
#   < 3`. That is the same obligation a grant already carries, and it is checkable
#   in a conformance case; a rule that silently denies everything is not.
#
# The `var IS NULL` guard comes first so an OPTIONAL MATCH that produced no node
# is not treated as a denial — consistent with the rest of the filter, where a
# null variable never removes a row.
def _denial_test(policy: SecurityPolicy, var: str) -> str:
    by_label: dict[str, list[str]] = {}
    for rule in policy.denials:
        by_label.setdefault(rule.label, []).append(
            f"coalesce({rule_test(policy, rule, var)}, false)")
    if not by_label:
        return ""
    denied = " OR ".join(f"({var}:{label} AND ({' OR '.join(tests)}))"
                         for label, tests in by_label.items())
    return f"({var} IS NULL OR NOT coalesce({denied}, false))"


def _path_test(policy: SecurityPolicy, var: str, strict: bool) -> str:
    """Grant test for one variable.

    A label counts as GOVERNED if it is named in `protected_labels` or has a
    grant. Governed-but-ungranted must therefore be DENIED, not waved through as
    reference data — otherwise a label with no grant would be readable by anyone,
    and under `both` that permissive default would OR away the property model's
    restriction entirely.
    """
    by_label: dict[str, list[str]] = {}
    for grant in policy.grants:
        by_label.setdefault(grant.label, []).append(rule_test(policy, grant, var))

    governed = sorted(set(policy.protected_labels) | set(by_label))
    if not governed:
        return "true"

    # Satisfying a grant declared for one of the variable's labels.
    granted = [f"({var}:{label} AND ({' OR '.join(tests)}))"
               for label, tests in by_label.items()]
    granted_expr = " OR ".join(granted) if granted else "false"

    if strict:
        return f"({var} IS NULL OR {granted_expr})"
    # Ungoverned nodes (Client, AdGroup, Desk…) still flow so joins work.
    is_governed = " OR ".join(f"{var}:{label}" for label in governed)
    return f"({var} IS NULL OR NOT ({is_governed}) OR {granted_expr})"


def _variable_test(policy: SecurityPolicy, var: str, strict: bool) -> str:
    granted = _granted_test(policy, var, strict)
    denied = _denial_test(policy, var)
    return f"({granted} AND {denied})" if denied else granted


def _granted_test(policy: SecurityPolicy, var: str, strict: bool) -> str:
    model = policy.grant_model
    if model == "property":
        return _property_test(policy, var, strict)
    if model == "path":
        return _path_test(policy, var, strict)
    # both: either route suffices.
    return f"({_property_test(policy, var, strict)} OR {_path_test(policy, var, strict)})"


def build_filter(policy: SecurityPolicy, scope: list[str], protect: list[str]) -> str:
    """The WHERE clause applied to every variable the query produces."""
    clauses = [_variable_test(policy, v, strict=(v in protect)) for v in scope]
    carried = ["authz", "caller"] if binds_caller(policy) else ["authz"]
    return ("WITH " + ", ".join(carried + scope)
            + "\nWHERE " + "\n  AND ".join(clauses))


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


# DYNAMIC_TYPES
# -------------
# Cypher 5.26+ supports dynamic node labels and relationship types — `MATCH
# (n:$any($labels))` and `-[r:$any($types)]->` — which would let the two
# interpolations below become ordinary parameters. That is the right instinct:
# these values come from bundle.yaml, so they are author-trusted rather than
# caller-supplied, but parameterising them would remove the last string
# interpolation from the prelude.
#
# We do NOT use it for the group traversal, because on 2025.10.1 a dynamic
# relationship type is SILENTLY IGNORED inside a variable-length pattern. Minimal
# reproduction, on a graph of (a:A)-[:GOOD]->(:B), (a:A)-[:BAD]->(:B):
#
#     MATCH (a:A)-[:GOOD*1..3]->(b)       -> ['good']          correct
#     MATCH (a:A)-[:$('GOOD')]->(b)       -> ['good']          correct (single hop)
#     MATCH (a:A)-[:$('GOOD')*1..1]->(b)  -> ['good', 'bad']   TYPE FILTER DROPPED
#
# The plan confirms it: the expand degrades to an untyped `(u)-[*..]->(g)`. Our
# prelude walks `-[:MEMBER_OF|...*1..]->` to collect the caller's groups, so this
# would over-collect — handing the caller principals they do not hold, which is
# privilege escalation, not a slow query. The quantified-path-pattern form
# `(()-[:$('GOOD')]->()){1,3}` filters correctly and is the way in if we ever
# adopt this.
#
# Labels are safe to parameterise (`DynamicLabelNodeLookup`, ~equal db hits), but
# the planner cannot see the label at plan time and estimated 150,188 rows where
# the interpolated form estimated 250. That mis-estimate is harmless in the
# prelude and would not stay harmless once it feeds a join, so both stay
# interpolated for consistency until there is a reason to move.

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
        .replace("@@PERM_PROP@@", policy.permissions_property)
        .replace("@@P_AUTHZ@@", P_AUTHZ)
        # Graph references are validated as identifiers at manifest load
        # (bundles._GRAPH_REF) because a USE clause cannot be parameterised.
        .replace("@@IDENTITY_GRAPH@@", policy.identity.identity_graph)
        .replace("@@DATA_GRAPH@@", policy.identity.data_graph)
        .replace("@@GROUP_RELS@@", "|".join(policy.identity.group_rels))
        .replace("@@CALLER_ATTRS@@", caller_attrs_map(policy))
        # A label expression rather than an unlabelled MATCH: the latter forces an
        # AllNodesScan that grows with the whole graph (measured: 405,430 db hits
        # per call against 4,541). See DYNAMIC_TYPES above on why not `$any($p)`.
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
        # Property names tried, in order, when naming a node in an explanation.
        P_DISPLAY_KEYS: list(dict.fromkeys(
            list(policy.resource_keys.values())
            + policy.identity.group_name_keys
            + policy.identity.match_keys
            + ["name", "title", "codename", "subject"]
        )),
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

    if not binds_caller(policy):
        # Separated identity. There is no caller node, but an anchor does not
        # actually need one — like a grant, it is a traversal FROM the caller, and
        # it can be cut at the same proxy. Measured on 100,000 trades at 1%
        # visibility: the split anchor reaches 6.2 ms / 17,008 db hits against
        # 79 ms / 1,302,001 unanchored, matching co-located anchoring (6.3 ms).
        # See scripts/bench_separation.py.
        filt = build_filter(policy, scope, protect)
        steps = [match_clause.strip()]
        if anchor:
            anchor_var, anchor_pattern = anchor
            pattern, predicate = split_anchor(policy, anchor_var, anchor_pattern)
            inner_scope = [v for v in scope if v != anchor_var]
            steps = [f"MATCH {pattern}", f"WHERE {predicate}",
                     f"WITH DISTINCT authz, {anchor_var}"]
            if inner_scope:
                steps.append("CALL {\n  WITH " + anchor_var + "\n  " + match_clause.strip()
                             + "\n  RETURN " + ", ".join(inner_scope) + "\n}")
        steps += [filt, "RETURN " + ", ".join(scope)]

        if policy.identity.source == SOURCE_COMPOSITE:
            # The filter runs INSIDE the constituent, not in the outer composite
            # query, because a composite query may not perform graph access at all
            # (42NA1) — and a path grant is graph access. Filtering here is also
            # strictly better: rows are discarded before they cross the boundary.
            inner = "\n  ".join(
                ["USE " + policy.identity.data_graph, "WITH authz"]
                + [s.replace("\n", "\n  ") for s in steps])
            return _subst("\n".join(
                [prelude_for(policy), "CALL {\n  " + inner + "\n}", final_return]), policy)
        # Still a subquery, even without a USE clause: only `scope` escapes it, so
        # a variable the match bound but did not declare cannot reach the output.
        body = ("CALL {\n  WITH authz\n  "
                + "\n  ".join(s.replace("\n", "\n  ") for s in steps) + "\n}")
        return _subst("\n".join([prelude_for(policy), body, final_return]), policy)

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
        # `caller` is imported alongside `authz` so a tool may scope its own
        # match to the calling user — e.g. "the clients I cover". That is a
        # BUSINESS scoping decision expressed in the query, distinct from
        # `anchor:`, which is a performance optimisation that must not change
        # which rows are entitled (see ANCHOR_SAFETY).
        body = (
            "CALL {\n  WITH authz, caller\n  " + match_clause.strip()
            + "\n  RETURN " + ", ".join(scope) + "\n}"
        )
    filt = build_filter(policy, scope, protect)
    return _subst("\n".join([_PRELUDE, body, filt, final_return]), policy)


def resolve_identity_query(policy: SecurityPolicy) -> str:
    """The standalone identity lookup.

    Under ``composite`` this runs on the same connection as the data query, so it
    must name the identity constituent. Under ``remote`` it runs on a different
    connection entirely and needs no USE clause — the source picks the database.
    """
    query = RESOLVE_IDENTITY_QUERY
    if policy.identity.source == SOURCE_COMPOSITE:
        query = "USE @@IDENTITY_GRAPH@@\n" + query
    return _subst(query, policy)


def prelude_only_query(policy: SecurityPolicy) -> str:
    """The authorization prelude with nothing after it.

    The fixed per-call cost of mediation: resolving the caller and expanding
    their principals. Used by scripts/bench_mediation.py to separate that
    constant overhead from the filter, which scales with rows examined.
    """
    return _subst(prelude_for(policy), policy) + "\nRETURN authz.authzPrincipals AS principals"


# --------------------------------------------------------------------------- #
# Access explanation
# --------------------------------------------------------------------------- #
#
# EXPLANATION_SAFETY
# ------------------
# An explanation is itself a disclosure channel. Saying "you are not entitled to
# TRD-3001" confirms TRD-3001 exists, which a bare "not found" would not. The
# tool therefore collapses "no such row" and "row exists but you may not read it"
# into one indistinguishable answer. That costs some debuggability and is the
# right trade: a caller learns why they CAN see something, never that something
# they cannot see is there.


def _rule_case_list(policy: SecurityPolicy, rules, label: str, base=None) -> str:
    """A Cypher list of the indexes of whichever rules match, for explanations.

    Indexes are positions in ``base`` (the full declared list) so an explanation
    can name the rule that fired, even when only a subset is being tested here.
    """
    base = rules if base is None else base
    arms = [f"      CASE WHEN {rule_test(policy, r, 'resource')} THEN {base.index(r)} ELSE null END"
            for r in rules if r.label == label]
    if not arms:
        return "[]"
    return "[x IN [\n" + ",\n".join(arms) + "\n    ] WHERE x IS NOT NULL | x]"


def explain_query(policy: SecurityPolicy, label: str, key_property: str) -> str:
    """Find one row and report which declared grants reach it from the caller.

    Returns one row per grant with whether it matched and the matching path, plus
    the ACL intersection for the property model. Callers must treat a `found`
    of false as "no answer", never as "does not exist" (see EXPLANATION_SAFETY).
    """
    # Each UNION arm is independent and must import the variables it uses.
    # OPTIONAL MATCH keeps one row per grant whether or not it matched, so a
    # resource with no matching grant still yields a row to report on.
    # A separated identity source has no caller node, so the resource lookup must
    # be scoped to the data graph. Under `composite` the split grants can still be
    # reported — each one is a data-side traversal — so provenance survives; the
    # matched path is not rendered because the identity-side prefix is not walked.
    if not binds_caller(policy):
        # Split grants are reported the same way for both separated sources — each
        # one is a data-side traversal, so provenance survives. The matched PATH is
        # not rendered, because the identity-side prefix was resolved elsewhere and
        # is not walked here.
        matched = _rule_case_list(policy, policy.grants, label)
        denied = _rule_case_list(policy, policy.denials, label)
        lookup = f"""  MATCH (resource:{label} {{{key_property}: ${P_RESOURCE_ID}}})
  RETURN resource,
    [x IN {matched} | {{idx: x, nodes: [], rels: []}}] AS matched,
    {denied} AS denied,
    [x IN coalesce(resource.`@@PERM_PROP@@`, [])
       WHERE x IN authz.authzPrincipals] AS aclMatches"""
        # The subquery must import authz explicitly — the grant tests reference it.
        use = (f"  USE {policy.identity.data_graph}\n"
               if policy.identity.source == SOURCE_COMPOSITE else "")
        return _subst(f"""{prelude_for(policy)}
CALL {{
{use}  WITH authz
{lookup}
}}
RETURN matched, denied, aclMatches, authz.authzPrincipals AS principals""", policy)

    # Only via-bearing grants can render a PATH. A grant that is a pure row
    # condition still has to be reported, so it goes through the same CASE list
    # the separated sources use, with no path to show.
    arms = [
        f"  WITH caller, resource\n"
        f"  OPTIONAL MATCH grantPath = {_bind(grant.via, 'resource')}\n"
        f"  RETURN {i} AS idx, grantPath AS p LIMIT 1"
        for i, grant in enumerate(policy.grants)
        if grant.label == label and grant.via and not grant.where
    ]
    conditional = [g for g in policy.grants
                   if g.label == label and (g.where or not g.via)]
    extra = (_rule_case_list(policy, conditional, label, policy.grants)
             if conditional else "[]")
    denied = _rule_case_list(policy, policy.denials, label)

    if arms:
        collect = ("CALL {\n" + "\n  UNION\n".join(arms) + "\n}\n"
                   "WITH caller, authz, resource, collect({idx: idx, path: p}) AS grants")
        matched = """[g IN grants WHERE g.path IS NOT NULL | {
     idx: g.idx,
     nodes: [n IN nodes(g.path) | {
       labels: labels(n),
       name: head([k IN $@@P_DISPLAY_KEYS@@ WHERE n[k] IS NOT NULL | toString(n[k])])
     }],
     rels: [r IN relationships(g.path) | type(r)]
  }]"""
    else:
        collect = "WITH caller, authz, resource"
        matched = "[]"

    return _subst(f"""{_PRELUDE}
MATCH (resource:{label} {{{key_property}: ${P_RESOURCE_ID}}})
WITH caller, authz, resource
{collect}
RETURN
  {matched} + [x IN {extra} | {{idx: x, nodes: [], rels: []}}] AS matched,
  {denied} AS denied,
  [x IN coalesce(resource.`@@PERM_PROP@@`, [])
     WHERE x IN authz.authzPrincipals] AS aclMatches,
  authz.authzPrincipals AS principals""".replace(
        "@@P_DISPLAY_KEYS@@", P_DISPLAY_KEYS), policy)
