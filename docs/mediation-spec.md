# Spec: entitlement mediation as an engine capability

Status: **implemented**. Item 1 in commit `fca100a`, item 2 following it.
Deviations from the original proposal are noted inline as **[as built]**.
Covers two changes:

1. **Derived protection** — remove "the model chooses its own security parameter."
2. **`security:` config block + mediated YAML tools** — make entitlement filtering a
   per-bundle, config-driven property of the engine rather than a bundle called `iam`.

---

## 0. Where policy lives (mental model)

After this change, authorization is expressed in **three** places. Keeping them
distinct is the whole design:

| Layer | Representation | Answers | Changed by |
| --- | --- | --- | --- |
| **Configuration** | `bundle.yaml` → `security:` | *How* is access enforced? | Engineers, at deploy |
| **Identity** | Graph traversal — `(:User)-[:MEMBER_OF*]->(:AdGroup)`, plus an inline list property | *Who* is this caller, and what groups do they hold? | Identity sync / SSO |
| **Grants** | Denormalised ACL — list-valued `Permissions.Read` on each business record | *Who* may read *this row*? | Source systems / data pipeline |

So the rules are a **hybrid**, not purely a graph: identity is graph-native and
transitive; grants are embedded per-row ACLs. See §4 for the tradeoff and the
seam left for a future path-based grant model.

---

## 1. Derived protection

### Problem

`secure-read-cypher` filters only the variables named in `protectedVariables`,
which is supplied by the **caller (the LLM)**. Nothing forces that list to be
complete. This leaks:

```jsonc
// fragment: MATCH (x:ResearchNote) MATCH (t:Trade)
{ "protectedVariables": ["x"], "returnVariables": ["x","t"],
  "finalReturn": "RETURN t.tradeId" }     // -> every trade, unfiltered
```

The same shape leaks through aggregates (`RETURN count(t)`).

### Change

The entitlement predicate applies to **every variable in scope** (protected ∪
returned), not to a caller-supplied subset. `protectedVariables` becomes
**optional and advisory** — useful for documenting intent and for forcing
fail-closed on a specific variable — but it is no longer the security boundary.

Per-variable rule, evaluated in the wrapper:

| Variable state | Outcome | Rationale |
| --- | --- | --- |
| `null` | pass | `OPTIONAL MATCH` miss |
| carries the permissions property | must intersect caller principals | the grant |
| lacks it | pass | reference data (Client, AdGroup, Desk…) flows |
| lacks it **but** holds a `protected_labels` label | **deny** | data-quality guard, fail closed |

Predicate shape (property access works on nodes *and* relationships, so no type
introspection is required in the hot path):

```cypher
WITH authz, <scope>
WHERE all(r IN [<every scope variable>] WHERE
      r IS NULL
   OR ( r[$__secure_perm_prop] IS NOT NULL
        AND any(p IN r[$__secure_perm_prop] WHERE p IN authz.authzPrincipals) )
   OR ( r[$__secure_perm_prop] IS NULL AND <not protected-label> )
)
```

**Implementation note.** The `protected_labels` arm needs `labels(r)`, which errors
on relationships. Two options: (a) verify `valueType()` availability on the target
Neo4j version and guard with it, or (b) — **recommended** — keep the hot path
property-only and enforce the fail-closed guarantee as a *validation* check
(§3.4) rather than at query time. Option (b) is portable and moves a data-quality
problem out of the request path.

### Known limitation: inference channels

Scope-based filtering cannot stop an *existence oracle* in an open-ended fragment:

```cypher
MATCH (x:ResearchNote) WHERE EXISTS { MATCH (t:Trade {tradeId:'TRD-3001'}) }
```

`t` never enters scope, so no filter applies, yet its existence changes the result.
**Mitigation:** in mediated mode, extend the fragment blocklist to reject
`EXISTS {`, `COUNT {`, and inline pattern predicates in `WHERE`. Document the
residual risk honestly — this is a general property of query mediation, and it is
the strongest argument for preferring **curated (YAML) mediated tools** in
production, with the open-ended tool reserved for exploration.

### Not covered

Relationship-level ACLs. A relationship is only bindable if its endpoints matched,
so this is acceptable, but it should be stated rather than implied.

---

## 2. `security:` block and mediated YAML tools

### 2.1 IAM stops being a bundle

| Today | After |
| --- | --- |
| `bundles/iam/pytools/iam_tools.py` defines `resolve-identity` + `secure-read-cypher` | Both move into the **engine**, registered whenever a bundle is mediated |
| Enforcement is a property of "the IAM bundle" | Enforcement is a property of a bundle's **datasource**, declared in config |
| `bundles/iam/` is the security mechanism | `bundles/iam/` is a **demo bundle** that happens to enable mediation |

Bundles never depend on other bundles. The capability lives in the engine; bundles
opt in. This avoids bundle-to-bundle dependency ordering and versioning entirely.

### 2.2 Config schema

```yaml
security:
  mode: mediated                 # open | mediated. Default open, WARNED loudly at startup.
  permissions_property: "Permissions.Read"
  protected_labels: [Communication, Request, Trade, Deal]

  principal:
    env: [NEO4J_MCP_PRINCIPAL, NEO4J_MCP_AUTH_SUBJECT, USER_EMAIL]
    everyone: everyone
    allow_impersonation: false   # env NEO4J_MCP_ALLOW_IMPERSONATION may enable; never in prod

  identity:                      # how to resolve a principal into authzPrincipals
    labels: [User, Principal]
    match_keys: [username, email, mail, userPrincipalName, upn, name, id]
    group_rels: [MEMBER_OF, MEMBER_OF_GROUP, IN_GROUP, HAS_GROUP]
    group_name_keys: [name, group, displayName, email, mail, id]
    inline_group_list: AdGroupList

  expose_open_query_tool: true   # register secure-read-cypher (text2cypher path)
  allow_unmediated_read: false   # must be explicitly true to keep raw read-cypher
```

Engine behaviour when `mode: mediated`:

1. Register `resolve-identity`, and `secure-read-cypher` if `expose_open_query_tool`.
2. Wrap **every YAML tool** in the auth prelude + entitlement filter.
3. Hide proxied `read-cypher` unless `allow_unmediated_read: true` (which logs a
   prominent warning — it defeats the guarantee).
4. Force downstream `read_only: true` by default. Mediation covers **reads only**;
   an unmediated `write-cypher` alongside a mediated read path is incoherent.
5. Reject at load: any YAML tool with `read_only: false` (see 2.3).

`mode: open` is unchanged behaviour — but must now be *stated*. Omitting `security:`
defaults to open **and logs a warning naming the bundle**, converting today's silent
default into a recorded decision. The scaffolder template ships `mode: open` explicitly.

### 2.3 Mediated YAML tool schema

To wrap a YAML tool we must know where its `RETURN` begins. **Parsing Cypher is
not acceptable** (WITH chains, subqueries, multiple RETURNs). Instead, mediated
tools are authored in fragment + final-return form, mirroring `secure-read-cypher`:

```yaml
name: acme_trades
description: Trades booked for a client.
parameters:
  - name: client
    type: string
    required: true
match: |                          # no RETURN; may use MATCH/OPTIONAL MATCH/WHERE/WITH
  MATCH (t:Trade)-[:FOR_CLIENT]->(c:Client {name: $client})
scope: [t, c]                     # [as built] REQUIRED: variables carried match -> return
protect: [t]                      # optional, advisory (see §1)
return: |                         # runs AFTER filtering — aggregates belong here
  RETURN t.tradeId AS tradeId, t.notional AS notional
read_only: true                   # required true in mediated mode
sample_args:                      # [as built] optional; lets CI exercise a tool
  client: "Acme Corp"             #   that has required parameters
```

**[as built] `scope:` is required.** The engine must know which variables to carry
out of the match subquery and into the filter, and deriving that would mean
parsing Cypher. Declaring it is one line and keeps the composition exact.

**[as built] `sample_args:`** — validation-only arguments so a tool with required
parameters is still exercised and persona-diffed by `validate_bundle.py`. Without
it, the most interesting tools are the ones CI skips.

Compatibility rules:

- `cypher:` (today's single-block form) stays valid in **open** bundles — ATO needs
  no migration.
- `cypher:` in a **mediated** bundle is a **load error** with a message telling the
  author to split it into `match:`/`return:`. Explicit beats a fragile auto-split.
- A bundle may mix both forms only while `mode: open`.

Reserved parameter names (`__secure_*`) are rejected in `parameters:`.

### 2.4 Code tools (pytools) under mediation

The engine cannot auto-wrap arbitrary Python. Therefore:

- `ToolContext` gains `secure_run(match, params, protect=None, final_return=None)` —
  the same composed path the engine uses — so code tools can opt in deliberately.
- At startup in mediated mode, log a warning naming any pytool that touched the raw
  executor, so unmediated code paths are visible rather than assumed safe.

### 2.5 Worked example — `entitlement_directory` under mediation

Converted form; `AdGroup`/`User` carry no permissions property and are not in
`protected_labels`, so they flow as reference data, and the aggregation correctly
runs post-filter:

```yaml
match: |
  MATCH (g:AdGroup)
  WHERE $group_name = '' OR g.name = $group_name
  OPTIONAL MATCH (u:User)-[:MEMBER_OF]->(g)
return: |
  RETURN g.name AS group, g.kind AS kind, count(u) AS memberCount, collect(u.email) AS members
```

---

## 3. Implementation plan

| # | Change | Files | Notes |
| --- | --- | --- | --- |
| 3.1 | `SecurityPolicy` dataclass + parsing | `gateway/bundles.py`, `config.py` | Validation errors name the bundle |
| 3.2 | Extract mediation engine | new `gateway/mediation.py` | Prelude + filter composition, parameterised by policy; **template the predicate**, don't hardcode it (see §4) |
| 3.3 | Engine-provided tools | move from `bundles/iam/pytools/` | `resolve-identity`, `secure-read-cypher` |
| 3.4 | YAML `match:`/`return:`/`protect:` support + mediated wrapping | `gateway/yaml_tools.py` | Load-time rejection of `cypher:` when mediated |
| 3.5 | Auto-hide + read-only defaults | `gateway/server.py`, `middleware.py` | Per-**connection**, not per bundle name |
| 3.6 | Validation | `scripts/validate_bundle.py` | (a) every `protected_labels` node has the permissions property; (b) run each mediated tool as ≥2 personas and assert the result sets differ / are subsets — a genuine entitlement regression test |
| 3.7 | Bundle migration | `bundles/iam/*`, `bundles/ato/bundle.yaml`, `_template` | IAM → `mode: mediated`, delete its pytools, convert its YAML tool; ATO → explicit `mode: open` |

Effort: 3.1–3.3 are mostly relocation of working code. 3.4 is the only substantive
new logic. 3.6 is small and high-value. Docs (`bundles/iam/README.md`,
`data/demo_prompts.md`, root README) follow.

**Verification bar** (same as prior work): Neo4j 5.x **Enterprise + APOC** in a
throwaway container; assert the published entitlement matrix per principal;
assert the §1 leak vector now returns nothing; assert ATO is byte-for-byte
unchanged in behaviour.

---

## 4. Deliberately out of scope

Left out to avoid overengineering, but the design must not preclude them:

- **Path-based / derived grants** — e.g. access implied by
  `(t:Trade)-[:FOR_CLIENT]->(c:Client)<-[:COVERS]-(u:User)` rather than a
  materialised ACL. This is the graph-native model: entitlements as *paths*, which
  gives provenance ("Joe sees this because he covers the client") and avoids
  rewriting millions of rows on a regrant. **Seam:** make the filter predicate a
  configurable template in 3.2 so a `grant_model: property | path` option can drop
  in later without touching the composition logic.
- **Explainability tool** (`explain-access`) — natural once path grants exist.
- Principal-resolution caching (a traversal per call is fine at demo scale).
- Field/property-level redaction; write-side authorization; policy inheritance;
  ABAC/ReBAC engines. All need a real customer requirement first.

## 5. Open questions

1. **Default posture** — spec says `open` + loud warning (non-breaking). A stricter
   reading for a bank reference architecture: make `security.mode` *required*, so
   every bundle states its posture. Breaking, but arguably correct.
2. **`get-schema` under mediation** — currently left exposed because text2cypher
   needs it. It discloses structure (labels, property keys), not rows. Acceptable?
3. **Fail-closed guard** — validation-time (portable, recommended) vs runtime label
   check (stronger, needs `valueType()` verification on the target version).
