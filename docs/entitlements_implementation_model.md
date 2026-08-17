# How we implement entitlements in secure read

A conceptual walkthrough of the entitlement model behind `secure-read-cypher` and
the curated mediated tools. Aimed at a 5–10 minute introduction: what the model
is, where each part lives, and what it does and doesn't guarantee. Implementation
reference lives in [`mediation-spec.md`](mediation-spec.md).

---

## 1. The problem (≈1 min)

A solution with database access is only safe if it can see exactly what the
person asking is allowed to see — no more, and no less.

Two obvious approaches don't work for a bank:

- **Filter afterwards.** If the model retrieves everything and then omits rows,
  the data has already left the database and entered the context window. And any
  aggregate it computed is wrong.
- **Use the database's native roles.** Neo4j's RBAC is scoped to labels and
  properties, not to _values in a row_. You cannot express "rows whose
  `Permissions.Read` list contains one of my groups" without creating a role per
  combination of entitlements — which at bank cardinality isn't viable.

So we mediate the query: **authorization is composed into the statement before
Neo4j executes it.** Unentitled rows are never read, never returned, and never
counted.

---

## 2. The core idea: four-part composition (≈1.5 min)

Every read the gateway performs in a mediated bundle is assembled from four parts,
in this order:

```
  1. authorization prelude   →  who is asking, and what do they hold?
  2. the query's match part  →  what are they asking for?
  3. entitlement filter      →  drop anything they may not see
  4. final RETURN            →  project or aggregate what survived
```

The order is the whole point. The filter sits _between_ the match and the return,
so **aggregates run after filtering** — a total is a total of the rows that caller
is entitled to, not a firm-wide number with rows hidden from the list.

**Where it lives:** [`gateway/mediation.py`](../gateway/mediation.py) —
`_PRELUDE` (part 1), `_FILTER` (part 3), and `compose()` which assembles all four.

---

## 3. Where policy lives: three layers (≈1.5 min)

Authorization is expressed in three distinct places. Keeping them separate is what
makes this reusable across use cases:

| Layer             | What it answers                                     | Where                                          |
| ----------------- | --------------------------------------------------- | ---------------------------------------------- |
| **Configuration** | _How_ is access enforced here?                      | `security:` block in each `bundle.yaml`        |
| **Identity**      | _Who_ is this caller, and what groups do they hold? | The graph — traversed at query time            |
| **Grants**        | _Who_ may read _this row_?                          | A list-valued property on each business record |

Note the asymmetry, because someone will ask about it: **identity is graph-native
and transitive** (`(:User)-[:MEMBER_OF*]->(:AdGroup)`), while **grants are
denormalised access-control lists** on the record itself. That's fast and it
mirrors how source systems export entitlements — but it means a grant has no
provenance. We can tell you _that_ Joe has access, not _why_ he was given it.

**Where it lives:**

- Config — [`gateway/bundles.py`](../gateway/bundles.py): `SecurityPolicy`,
  `IdentityConfig`, `PrincipalConfig`
- Identity graph — [`bundles/iam/data/iam_demo.cypher`](../bundles/iam/data/iam_demo.cypher)
  (the `MEMBER_OF` block)
- Grants — the same file, the `Permissions.Read` assignments on `Communication`,
  `Request`, `Trade`, `Deal`

**The seam:** the two halves meet in exactly one expression, in `_FILTER` —
"is any principal on this row's list one of the caller's principals?" Left side is
a property; right side is derived from the graph. Replacing that one expression is
how a future path-based grant model would drop in.

---

## 4. Two ways a query gets there (≈1.5 min)

The same mediation applies to both, and the difference is who wrote the Cypher:

**Curated** — a human authors the tool in YAML, declaring the split explicitly:
`match:` / `scope:` / `return:`. The engine inserts the prelude and filter between
them. This is the production-shaped path: the query is reviewed once, then serves
every caller with different results.
→ [`bundles/iam/tools/client_activity.yaml`](../bundles/iam/tools/client_activity.yaml)

**Open-ended** — the assistant generates a match fragment at call time and passes
it to `secure-read-cypher`. Flexible, good for exploration, and subject to more
restrictions because we didn't write the query.
→ [`gateway/security_tools.py`](../gateway/security_tools.py): `build_secure_read_cypher()`

Both are entitled identically. Neither can bypass the filter, because the raw
`read-cypher` tool is removed from what the client can see whenever a bundle is
mediated ([`gateway/server.py`](../gateway/server.py), enforced by
`HideToolsMiddleware`).

**Talking point:** this is why the pattern generalises. Entitlement isn't a
property of "the IAM use case" — it's a property of _how a bundle exposes data_.
Any bundle turns it on with one config line; the account-takeover bundle
deliberately declares `mode: open` because its consumers are uniformly entitled.

---

## 5. Why we trust it (≈2 min)

Three properties worth stating explicitly, because each closes a specific failure:

**Filtering doesn't depend on the model.** _Every_ variable a query produces is
filtered, whatever the caller declared. If the boundary were a caller-supplied
list, an LLM would be choosing its own security parameter and an incomplete list
would leak rows. → `compose()` in `mediation.py`.

**Records without an access-control list are caught before production.** Anything
carrying an ACL must match; anything without one is treated as reference data and
flows. That's correct for clients and desks, but a _business record_ missing its
ACL would silently become world-readable — so each bundle declares which labels
must always carry one, and validation fails if any don't.
→ `_check_protected_labels()` in [`scripts/validate_bundle.py`](../scripts/validate_bundle.py)

**We prove the filter actually discriminates.** A filter that returns the same
rows for everyone is indistinguishable from no filter. Validation runs each
mediated tool as several real principals and reports whether results vary.
→ `_check_entitlement_differentiation()` in the same file

Backstops underneath: generated fragments are screened
(`validate_fragment()`), and everything executes in a read transaction, so Neo4j
itself refuses a write even if something slipped past.

---

## 6. What this is not (≈1 min)

Say this before you're asked:

- **The gateway is the enforcement boundary,** not the database. Anyone with
  direct database credentials is bounded only by Neo4j's own auth. This protects
  the assistant channel.
- **Open-ended queries have a residual inference channel.** A generated fragment
  can, in principle, use a pattern as an existence test for data it can't read. We
  block the common forms — and if that residual risk isn't acceptable, one config
  line removes the class entirely (see §7).
- **Read-side only.** Mediation covers reads; mediated bundles are read-only by
  design rather than pretending to authorize writes.
- **No provenance yet** (see §3). "Why does Joe have access?" needs the
  path-based grant model, which the current design leaves room for.

---

## 7. Three postures, one architecture (≈1.5 min)

The same engine supports three deployment stances. This is usually the answer a
security architect is actually looking for — you're not defending one design, you
are showing a dial:

| Posture | Config | Who writes the Cypher | Use for |
| --- | --- | --- | --- |
| **Open** | `mode: open` | humans (curated), unfiltered | data where every consumer is uniformly entitled (our fraud bundle) |
| **Exploration** | `mode: mediated` | humans **and** the model | analyst desktop; maximum flexibility, fully entitled |
| **Curated only** | `mode: mediated`<br>`expose_open_query_tool: false` | humans only | regulated production workflows |

In **curated only**, the open-ended `secure-read-cypher` is never registered — the
assistant's entire vocabulary is the set of reviewed tools you shipped. Since no
query is generated at runtime, the inference channel in §6 cannot occur at all.
It's the difference between screening dangerous query shapes and making them
impossible.

The tradeoff is real and worth saying out loud: the assistant can only answer
questions your curated tools cover. Ask something nobody anticipated and there is
no path to the data. That is often exactly what a regulated workflow wants.

Two properties worth mentioning:

- **The posture is visible.** The gateway logs which stance it started in.
- **Config is the floor.** `EXPOSE_OPEN_QUERY_TOOL=false` in the environment can
  *tighten* a deployment, but a bundle that declares curated-only cannot be
  re-opened from the shell. → `Config.from_env()` in
  [`gateway/config.py`](../gateway/config.py)

**Where it lives:** `build_security_tools()` in
[`gateway/security_tools.py`](../gateway/security_tools.py) decides which tools a
mediated bundle publishes.

---

## 8. The 60-second demo (≈1 min)

Ask the same question as two people:

> "Show me the client communications for Acme Corp."

Anna — who had a private chat with the client's treasurer — sees two. Joe, who
**covers the same client**, sees one. He isn't told access was denied; the row
simply isn't in his result. Then ask for a total notional and note that the number
itself differs per caller.

Close on the architecture point: _entitlements live in the graph, not in each
application. One endpoint serves every desk, and each person's assistant answers
from exactly the rows they're cleared to see — including the aggregates._

Full script with expected output: [`bundles/iam/data/demo_prompts.md`](../bundles/iam/data/demo_prompts.md)
