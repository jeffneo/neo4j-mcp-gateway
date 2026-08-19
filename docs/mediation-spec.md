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

## 4b. Denials and row conditions

A grant and a denial have the same shape and are evaluated the same way. A rule
needs at least one of `via` (a path from the caller to the row) and `where` (a
condition on the row); a rule with only `where` needs no traversal at all.

```yaml
grants:
  - label: Trade
    via: "(caller)-[:ON_DESK]->(:Desk)<-[:BOOKED_ON]-(resource)"
    where: "resource.notional < 50000000"
    reason: "on the booking desk, within the desk notional limit"

denials:
  - label: Communication
    via: "(caller)-[:ON_DESK]->(:Desk)<-[:RESTRICTED_FOR]-(:Client)<-[:WITH_CLIENT]-(resource)"
    reason: "the client is on the restricted list for this caller's desk"
  - label: Trade
    where: "resource.restricted = true"
    reason: "the trade is flagged restricted"
```

The composed test per variable is **`(granted) AND NOT (denied)`**.

**Why denials are not just "remove the grant".** A restricted list withdraws
access someone genuinely holds. In the `iam` bundle, `priya` is a *participant*
in `COMM-1003` — a path grant, the strongest route the model has — and the
restriction on her desk removes it anyway. There is no grant to delete: hers is
correct and should stay. "Granted, then withdrawn" and "never granted" are
different facts with different audit stories, and `explain-access` reports them
differently:

```
COMM-1003  priya  granted=False  DENIED BY: the client is on the restricted list for this caller's desk
    (grants that DID match but were overridden: ['was a participant in the conversation'])
```

**NULL does not deny.** An absent property yields `NULL` in Cypher, so a denial
written `resource.restricted = true` evaluates to `NULL` on every row that simply
has no such property. An earlier version of this treated `NULL` as "deny" on the
reasoning that an undecidable revocation should revoke; the result denied every
unrestricted row. Absence is not ambiguity. Each rule is wrapped in
`coalesce(..., false)`.

> Where absence *should* deny, write it into the predicate:
> `coalesce(resource.clearance, 0) < 3`, not `resource.clearance < 3`. A
> conformance case can check that; a rule that silently denies everything cannot
> be distinguished from a caller who is entitled to nothing.

**Both fields are interpolated into Cypher**, so both are author-trusted config at
the same level as a tool's own query, and both are validated at load: no clause
keywords, no semicolons, a `via` must bind `resource`, and a `where` must
reference `resource` when there is no `via` (otherwise it would apply to every row
of that label regardless of which one). A `where` may reference `caller` only
when identity is co-located — a separated source has no caller node in the data
query, and the engine says so rather than failing at query time.

Denials split at the identity/data boundary exactly like grants, so they work
under every identity source.

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

### Invariants

Every other case asks *"what does this caller see?"* — per-caller, per-query,
asserted against enumerated ids. An **invariant** asks a different kind of
question: *"is there any entity for which the model is wrong?"* It quantifies
over the whole graph and has no principal.

```yaml
- name: "invariant: every ACL entry names a principal that exists"
  invariant: |
    MATCH (n) WHERE n.`Permissions.Read` IS NOT NULL
    UNWIND n.`Permissions.Read` AS entry
    WITH DISTINCT entry
    WHERE entry <> 'everyone'
      AND NOT EXISTS { MATCH (g:AdGroup {name: entry}) }
      AND NOT EXISTS { MATCH (u:User  {email: entry}) }
    RETURN entry AS danglingPrincipal
  expect_count: 0
```

Each returned row is a violation; `expect_count` says how many are tolerated.

**It runs unmediated, and that is the point.** Composing an invariant through the
entitlement filter would restrict it to some caller's entitlements — backwards,
since its job is to see everything and report what should not be connected. It is
therefore a declared exception to a mediated bundle's posture: no tool exposes
one, and the gateway never runs them. They belong in CI and in a control-room
review. Write clauses are refused.

**`expect_count` is a baseline, not always zero.** The `iam` bundle carries a
separation-of-duty invariant at `expect_count: 1` — one person was already a
participant on a client when the restriction landed on their desk, which is
precisely how these arise. The filter correctly denies each individual read while
the structural conflict persists, and no per-caller test can see it. Recording
the count makes it a *reviewed* decision and turns any second bridge into a
failed build.

**Invariants are graph-wide**, so in a database shared with another bundle they
see that bundle's records too. Correct — the graph is shared — but a mediated
bundle wanting clean invariants wants its own database, which the open/mediated
safety rule already pushes toward.

> Worth recording: the dangling-principal invariant above found a real bug on its
> first run. `platform.cypher` derived ACL entries as `'product-' +
> toLower(family)` and the catalogue has a Data family, but `identity.cypher`
> created no `product-data` group. Every Data-product usage record carried a
> grant to a principal that did not exist — harmless until the name is reused, at
> which point it silently becomes a grant to someone new. Nine conformance cases
> over that bundle had never touched it, because no caller's view was wrong.

### Conformance cases

`scripts/check_entitlements.py` runs declarative cases from a bundle's
`entitlement_tests.yaml`, asserting what a named principal **must see** and
**must not see**, plus exact counts and cross-principal parity (`same_for`, for
"two people covering the same client must see the same book"). It exits non-zero
on failure, so it gates CI and stands as evidence in a security review.

```bash
uv run python scripts/check_entitlements.py iam
```

The negative assertions are the security tests. Note the division of labour with
the data guard above: a record explicitly named in `protect` and **missing** its
ACL is *denied* (fail closed), so conformance still passes; a record only in
derived scope and missing its ACL becomes visible to everyone, and that is what
`protected_labels` catches. Two failure modes, two guards.

### Measuring the cost

`scripts/bench_mediation.py` runs each mediated tool three ways — open (no
prelude, no filter), mediated, and the prelude alone — and reports wall-clock
percentiles plus `PROFILE` database hits, separating the **fixed** cost
(identity resolution, once per call) from the **variable** cost (the row filter,
which scales with rows examined):

```bash
uv run python scripts/bench_mediation.py iam --runs 50
```

Prefer db hits to wall-clock on small datasets: wall-clock there is mostly
network round-trip. `scripts/generate_scale_data.py` builds a realistically
shaped dataset (by default 100,000 clients with any one salesperson entitled to
~1,000) so the numbers mean something.

Two things scale testing established:

- **Identity labels must be scoped.** The prelude matches `(u:Label|Label)` using
  the configured `identity.labels`, which the planner resolves to a label scan.
  An unlabelled match would force an `AllNodesScan` that grows with the entire
  graph — on the 200k-node dataset that was 405,430 db hits per call versus
  4,541 once scoped.
- **The remaining cost is rows examined, not identity.** The filter tests list
  membership per row, which is CPU rather than db hits, so the way to reduce it
  is to examine fewer rows. Filtering after a full scan means touching every
  record and discarding what the caller cannot see; anchoring the query on what
  the caller covers touches only the entitled subset. On the 100k dataset the
  same answer cost 700,001 db hits scanned-and-filtered versus 5,015 anchored.
  That difference is the argument for the path-based grant model in §7.

## 5b. Audit trail

`gateway/audit.py` appends one JSON object per tool call when
`NEO4J_MCP_AUDIT_LOG` is set. It is a record of **authorization decisions**, not
of data.

| Field | Why it is there |
| --- | --- |
| `principal`, `principalSource` | who the call ran as, and how that was established (`env:X` or `impersonation-request`) |
| `impersonated` | top-level boolean: running as another principal is a privileged action and the first thing a reviewer looks for |
| `bundle`, `tool`, `mode` | which surface was used, and whether it was filtered at all |
| `identitySource`, `grantModel` | which topology and which entitlement model produced the decision |
| `rows` | how many rows survived the filter — the outcome of the decision |
| `outcome`, `error`, `durationMs` | success, rejection, or failure |
| `argumentNames` | which question was asked, without its subject |
| `arguments` | the subject too — **opt-in**, `NEO4J_MCP_AUDIT_ARGUMENTS=true` |

**Row contents are never recorded.** A log that copies the rows it audits is a
second, less-protected replica of the data the filter exists to restrict, on a
filesystem with weaker controls and often shipped to an aggregator a different
team can read. That is why `rows` is a count.

Coverage is the whole gateway, not only mediated tools: proxied tools are wrapped
too, so an `open` bundle's raw `read-cypher` is audited on the same terms, and an
attempt to call a hidden tool is recorded as a rejection rather than vanishing.

`security.require_audit: true` makes the gateway refuse to start without a log
path configured — running unaudited becomes a recorded decision, matching how
`security.mode` treats running unfiltered.

### Tamper evidence

Records are hash-chained: each carries `seq`, `prev` (the previous record's hash)
and its own `hash` over the canonical serialisation of everything else.
[`scripts/verify_audit.py`](../scripts/verify_audit.py) recomputes the chain and
names the first line that breaks it.

Be precise about what that buys, because "immutable audit log" is a phrase a
reviewer will press on:

| | |
| --- | --- |
| **Detected** | a line edited in place; a line removed from the middle; lines reordered or inserted |
| **Not detected** | truncating the whole file and starting fresh — the new chain is internally valid, and a genesis record is indistinguishable from a log never written |
| **Not prevented** | anything. Write access plus the code is enough to recompute the chain forward from any change |

Both gaps close the same way and only the same way: **get the head hash out of
reach of whoever can write the file.** `NEO4J_MCP_AUDIT_FORWARDER` periodically
publishes `(seq, head)` to a destination the gateway host cannot rewrite, and
`verify_audit.py --checkpoints` compares them. A checkpoint whose seq is past the
end of the file is proof of truncation; a head that disagrees is proof of
rewriting.

Demonstrated: after emptying the log and letting the gateway start a new chain,
verification alone reports **PASSED**; verification against the anchors reports
`checkpoint seq 6 is past the end of the log (last is 1) — 5 record(s) are
MISSING`. The forwarder is not decoration.

Two forwarders ship — `stderr` and `file:<path>` — and neither is a real anchor
on the same host. That is the seam: a deployment registers its own SIEM, WORM
bucket or signing service with `gateway.audit.register_forwarder()`, a one-method
contract. The gateway logs a warning at startup when no forwarder is configured,
and refuses to start on an unknown one.

## 5c. Downstream identity

Native database rules — RBAC, property-based rules, roles assigned by attribute —
are evaluated against the account that **connects**. A gateway holding one
service connection therefore has them evaluated against the service account, and
the layer-2 floor is absent. Two per-session ways to close that, in
`gateway/yaml_tools.py` (see DOWNSTREAM_IDENTITY):

| Variable | Effect |
| --- | --- |
| `NEO4J_MCP_ACCESS_TOKEN` | the session authenticates with the caller's token; **the database** validates signature, issuer, audience and expiry, and maps claims to roles |
| `NEO4J_MCP_DB_IMPERSONATION` | the service account impersonates the resolved principal, so that user's roles and property rules apply |

Setting both is refused: a token already asserts who the caller is, so
impersonating another user on top leaves the effective identity ambiguous.

Do not confuse `NEO4J_MCP_DB_IMPERSONATION` with `NEO4J_MCP_ALLOW_IMPERSONATION`.
The latter lets a *tool caller* claim a principal for testing — a layer-3 switch.
This one decides which *database user* the connection runs as.

**They compose with mediation rather than replacing it.** Measured against a real
native role carrying `GRANT MATCH ... FOR (o:Opportunity) WHERE o.stage =
'Proposal'`: the same caller sees 2 rows through mediation alone (her coverage)
and 1 impersonated — the intersection of layer 2 and layer 3.

**Deployment note.** With identity co-located, the impersonated user must also be
able to read the identity graph, or the authorization prelude resolves nothing
and every query returns zero rows. That fails closed, but it presents as an
entitlement bug rather than a permissions one. `identity.source: remote` avoids
it: identity resolution uses its own connection, and only the data query is
impersonated.

**Not solved by this.** A token supplies *group membership*; it cannot express a
relationship-derived entitlement ("the person who logged this record"). Native
property rules compare a property to a constant fixed at grant time, so
team-scoped rules cost one role per team. Both remain layer-3 work — which is
the argument in §6 of `entitlement-model-brief.md`, unchanged.

## 5d. The entitlement surface — which edges decide access

"Entitlement edge" is not a property of an edge. `AUTHORED_BY` is a business fact
when you ask who wrote something and an entitlement route when a grant traverses
it — the same edge in the same graph. What makes a relationship type
entitlement-bearing is that a **declared rule names it**.

So there is no separate entitlement subgraph to maintain. The entitlement graph is
the **projection** of the graph onto the relationship types and properties named
in `grants`, `denials` and `identity` — derivable from configuration rather than
described in a document that drifts:

```bash
uv run python scripts/entitlement_surface.py asset_platform
```

That produces the artefact a security review wants: the exact set of relationship
types and properties whose modification changes who can read what.

### The split that decides governance is WRITE OWNERSHIP

Everything above comes out of the rules. Who *writes* each edge does not — it is a
deployment fact, declared once in `security.ingested_rels` as
`relationship type -> the feed that writes it`. Anything absent from that map is
**authored**: no automated writer, so a database role can be denied write on it
outright.

| | Authored | Feed-written |
| --- | --- | --- |
| `asset_platform` | `SCOPED_TO`, `RESTRICTED_FOR`, `HAS_ROLE`, `HAS_CLIENT_ROLE` | the other 18 |
| Control available | `DENY … ON GRAPH … RELATIONSHIP` — absolute, needs no testing | change review, conformance in CI after every load |
| Generate it | `entitlement_surface.py --write-guard <role>` | — |

Four out of twenty-two is the honest measure of how much of an entitlement model
can be put behind a write privilege, and it is why the rest is answered by testing.

**A parallel "entitlement-only" copy of a feed-written edge does not fix this.** A
derived rail moves the same upstream edit one step downstream and adds a staleness
failure nothing in the query can detect. A rail is worth minting when you need to
revoke someone's ability to *write* it — not to tidy the diagram. Full argument in
[entitlement-edges.md](entitlement-edges.md).

### Polarity is per rule, not per edge

A type traversed by a grant enables; by a denial, disables; and the same type does
both — in `asset_platform`, `IN_UNIT`, `WITH_ORG` and `WITH_COUNTERPARTY` each
appear in a grant and in a denial. The graph therefore cannot be coloured red and
green; you have to name the rule. This is why *"authorised iff there is an enabling
path and no disabling path"* operationalises as *"matches a grant pattern and no
denial pattern"*.

### The drift it detects

A rule that traverses a relationship type absent from the graph can never fire.
For a grant that under-grants and is caught by a `must_see` failure. **For a denial
it fails OPEN** — the barrier silently stops applying, and no per-caller test
notices because nothing is missing from anyone's results. The surface report flags
both, and separately flags every barrier resting on a feed-written edge:

```
!! BARRIERS THAT DEPEND ON A FEED-WRITTEN EDGE (3)
   IN_UNIT              written by business_hierarchy
   WITH_COUNTERPARTY    written by platform_records
   WITH_ORG             written by platform_records
```

The mitigation is an invariant asserting the barrier edges are present — the only
kind of test that catches a lifted barrier. Worth running in CI beside the
conformance suite.

## 5e. Caller attributes — thresholds are not principals

`rankLevel >= 5` is an **ordering**, and a set of principal names cannot express
one. Encoding "managing director or above" as membership means minting a principal
per rank and re-issuing it on every promotion: the materialisation problem this
design exists to avoid, in miniature.

`security.identity.caller_attributes` therefore lifts declared properties of the
caller into `authz.attrs`, resolved once in the prelude:

```yaml
identity:
  caller_attributes: [rankLevel]
grants:
  - label: Compensation
    via: "(caller)<-[:REPORTS_TO*1..4]-(:Employee)<-[:COMPENSATION_OF]-(resource)"
    where: "authz.attrs.rankLevel >= 4"
```

- **Read once, not per row.** A threshold over a million rows costs one property
  read, because the attribute is resolved with the principals.
- **It survives a separated topology.** A scalar crosses a composite or remote
  boundary where the caller *node* cannot, so thresholds work unchanged under all
  three `identity.source` values — verified by running the whole suite under each.
- **NULL fails closed in a grant, OPEN in a denial.** `NULL >= 5` is NULL, so a
  missing attribute withholds a grant (visible: rows vanish) and withdraws a
  barrier (invisible). Hence two guards: an undeclared attribute name is rejected
  at manifest load, and an invariant asserts every caller carries every deciding
  attribute.

Only `authz.principalId`, `authz.tenantId` and `authz.attrs.<declared>` are
readable from a rule; anything else is a load-time error.

### The author's predicate is always bracketed

`AND` binds tighter than `OR`, so a `where` containing a top-level `OR` composed
with the boundary cut predicate parses as `(cut AND a) OR b` — which collapses to
`true` for any caller satisfying `b`, discarding the predicate that ties the pattern
to *that* caller. A disclosure, not a wrong count.

The engine brackets author predicates unconditionally. This class of defect is
invisible co-located, because the predicate is then alone in its subquery, so
`check_entitlements.py --identity-source remote` exists to assert the same cases
under a separated topology. Run both in CI.

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

### `scope` accepts node variables only

The filter applies a label predicate to every variable in `scope`, so a
relationship variable fails with `Type mismatch: expected Node`. A relationship
property can therefore be used to FILTER inside `match` but cannot be projected in
`return`:

```yaml
match: |
  MATCH (r:Role)-[sc:SCOPED_TO]->(s:Sector)
  WHERE sc.validFrom <= date() AND sc.validTo >= date()   # filtering: fine
scope: [r, s]                                             # sc must NOT be here
return: "RETURN r.name AS role, s.name AS sector"         # sc.validTo: not available
```

This matters for dated entitlements, where the window lives on the relationship.
Filtering on it works; reporting it needs the value copied onto a node, or the
question rephrased as "which scopes are in force" — usually the more useful one.

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
