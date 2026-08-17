# Entitlement mediation — reference

How the gateway filters reads against a caller's entitlements: the configuration
surface, the filtering semantics, where each part lives, and the known limits.

For a 5–10 minute conceptual introduction, read
[`entitlements_implementation_model.md`](entitlements_implementation_model.md)
instead — this document is the detailed reference behind it.

---

## 1. Where policy lives

Authorization is expressed in three distinct places. Keeping them separate is
what makes the capability reusable across use cases:

| Layer | Representation | Answers | Maintained by |
| --- | --- | --- | --- |
| **Configuration** | `bundle.yaml` → `security:` | *How* is access enforced? | Engineers, at deploy |
| **Identity** | Graph traversal — `(:User)-[:MEMBER_OF*]->(:AdGroup)`, plus an inline list property | *Who* is this caller, and what groups do they hold? | Identity sync / SSO |
| **Grants** | Denormalised ACL — a list-valued property (default `Permissions.Read`) on each business record | *Who* may read *this row*? | Source systems / data pipeline |

The rules are a hybrid rather than purely a graph: identity is graph-native and
transitive; grants are embedded per-row ACLs. That is fast and mirrors how source
systems export entitlements, at the cost of provenance — you can tell *that* a
principal has access, not *why* it was granted. See §5 for the seam that a
path-based grant model would use.

## 2. Access modes

Every `bundle.yaml` must declare `security.mode`. There is no default, so an
unfiltered bundle is a recorded decision rather than an omission.

| Mode | Behaviour | Use when |
| --- | --- | --- |
| `open` | Tools read the graph directly. | Every consumer of this bundle is uniformly entitled to all of its data. |
| `mediated` | Every read is wrapped in an authorization prelude and filtered against the caller's principals. | Callers are entitled to different subsets. |

Under `mediated` the engine:

1. registers `resolve-identity`, and `secure-read-cypher` when
   `expose_open_query_tool` is true;
2. composes every curated YAML tool with the prelude and entitlement filter;
3. hides the proxied `read-cypher` (it would bypass the filter) unless
   `allow_unmediated_read` is explicitly set, which logs a prominent warning;
4. defaults the downstream official server to read-only — mediation covers reads,
   so an unmediated `write-cypher` alongside a filtered read path is incoherent;
5. requires tools to use the mediated authoring form (§4) and to be read-only.

### Postures

| Posture | Config | Who writes the Cypher |
| --- | --- | --- |
| Open | `mode: open` | Humans; unfiltered |
| Exploration | `mode: mediated` | Humans and the model |
| Curated only | `mode: mediated`, `expose_open_query_tool: false` | Humans only |

`EXPOSE_OPEN_QUERY_TOOL=false` in the environment tightens a deployment to
curated-only at runtime. It can only tighten: a bundle that declares
`expose_open_query_tool: false` cannot be re-opened from the shell. The active
posture is printed in the gateway's startup log.

## 3. Configuration surface

```yaml
security:
  mode: mediated                 # REQUIRED: open | mediated
  permissions_property: "Permissions.Read"
  protected_labels: [Communication, Request, Trade, Deal]

  principal:
    env: [NEO4J_MCP_PRINCIPAL, NEO4J_MCP_AUTH_SUBJECT, USER_EMAIL]
    everyone: everyone
    allow_impersonation: false   # demo only; NEO4J_MCP_ALLOW_IMPERSONATION may also enable

  identity:                      # how to resolve a principal into authzPrincipals
    labels: [User, Principal]
    match_keys: [username, email, mail, userPrincipalName, upn, name, id]
    group_rels: [MEMBER_OF, MEMBER_OF_GROUP, IN_GROUP, HAS_GROUP]
    group_name_keys: [name, group, displayName, email, mail, id]
    inline_group_list: AdGroupList

  expose_open_query_tool: true   # register secure-read-cypher
  allow_unmediated_read: false   # keep raw read-cypher (defeats the guarantee)
```

Everything under `identity:` is a graph-shape detail, so a bundle whose identity
model uses different labels, relationships or property names can be mediated
without code. Connection details are never configured here — they come from
`.env` only (root, then the bundle's own).

Parsed by `SecurityPolicy`, `IdentityConfig` and `PrincipalConfig` in
[`../gateway/bundles.py`](../gateway/bundles.py).

## 4. Filtering semantics

Each mediated read is assembled as: **authorization prelude → the query's match
part → entitlement filter → final RETURN**. The filter sits between the match and
the return, so aggregates are computed over only the rows the caller may see.

The filter applies to **every variable the query produces**, not to a list the
caller supplied — security cannot depend on what a model declared:

| Variable state | Outcome | Rationale |
| --- | --- | --- |
| `null` | pass | `OPTIONAL MATCH` miss |
| carries the permissions property | must intersect the caller's principals | the grant |
| lacks it | pass | reference data (Client, AdGroup, Desk…) must flow for joins |
| explicitly listed in `protect` / `protectedVariables` | **strict**: must carry an ACL *and* match | opt-in fail-closed |

Composition lives in `compose()` in [`../gateway/mediation.py`](../gateway/mediation.py);
`_PRELUDE` and `_FILTER` in the same module are the two templates.

### Authoring a mediated tool

The engine never parses Cypher to locate the `RETURN` — mis-locating it would be
a security bug — so mediated tools declare the split:

```yaml
name: acme_trades
description: Trades booked for a client.
parameters:
  - name: client
    type: string
    required: true
match: |                          # no RETURN; MATCH / OPTIONAL MATCH / WHERE / WITH
  MATCH (t:Trade)-[:FOR_CLIENT]->(c:Client {name: $client})
scope: [t, c]                     # variables carried from match into return; all are filtered
protect: [t]                      # optional strict subset
return: |                         # runs AFTER filtering — aggregates belong here
  RETURN t.tradeId AS tradeId, t.notional AS notional
read_only: true                   # required in a mediated bundle
sample_args:                      # optional; lets validate_bundle exercise a tool
  client: "Acme Corp"             #   that has required parameters
```

The single-block `cypher:` form remains valid in `open` bundles; in a mediated
bundle it is a load error naming the file and showing the conversion.

### Code tools

The engine cannot auto-wrap arbitrary Python, so a code tool that reads business
records calls `ToolContext.secure_run()`
([`../gateway/pytools.py`](../gateway/pytools.py)). Reaching for the raw executor
is permitted for reference data but is recorded, and the server warns at startup
when a mediated bundle's code tools took an unfiltered path.

## 5. Validation

`scripts/validate_bundle.py` performs two entitlement-specific checks against a
live database, in addition to running every tool:

- **Fail-closed data guard** — a business record carrying a `protected_labels`
  label but *missing* its ACL would flow to everyone as reference data. The check
  fails when any such record exists, catching the problem in CI rather than in
  production. Run it after every data load.
- **Persona differentiation** — a filter returning the same rows for everyone is
  indistinguishable from no filter, so each mediated tool is run as several real
  principals and the results compared.

## 6. Known limits

- **The gateway is the enforcement boundary**, not the database. Anyone with
  direct database credentials is bounded only by Neo4j's own auth.
- **Inference channels.** A model-generated fragment can in principle use a
  pattern as an existence test for data it cannot read. `EXISTS`, `COUNT`,
  `COLLECT` and `CALL` subquery expressions are blocked, but inline pattern
  predicates in `WHERE` remain. Curated tools avoid the class entirely, and the
  curated-only posture (§2) removes it outright.
- **Read-side only.** Mediated bundles are read-only by design.
- **Relationship-level ACLs are not supported.** A relationship is only bindable
  when its endpoints matched, so this is acceptable but worth stating.
- **No provenance.** Grants are values in a list, with no grantor or date (§1).

## 7. Deliberately not included

Left out until a concrete requirement justifies them; the design does not
preclude any of them:

- **Path-based / derived grants** — access implied by a traversal such as
  `(t:Trade)-[:FOR_CLIENT]->(c:Client)<-[:COVERS]-(u:User)` rather than a
  materialised ACL. This is the graph-native model: it yields provenance ("this
  caller sees it because they cover the client") and avoids rewriting many rows on
  a regrant. **The seam** is the single predicate in `_FILTER`; it is templated so
  a `grant_model: property | path` option can be added without touching
  composition.
- **An `explain-access` tool** — natural once path grants exist.
- Principal-resolution caching (a traversal per call is fine at demo scale).
- Field-level redaction, write-side authorization, policy inheritance, and
  general ABAC/ReBAC engines.
