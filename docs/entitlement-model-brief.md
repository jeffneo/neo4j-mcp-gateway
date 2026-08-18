# Entitlement model — conceptual brief

For someone who knows the requirement but has not seen this repository. The whole
design is one idea with three refinements; each section says where it lives.

Deeper material: [`entitlements_implementation_model.md`](entitlements_implementation_model.md)
is a 5–10 minute talk track, [`mediation-spec.md`](mediation-spec.md) is the
reference, and [`path-grants-design.md`](path-grants-design.md) carries the design
decisions and measurements.

---

## The idea

**Entitlements are not checked after the query. They are compiled into it** — so
rows the caller may not see are never read, never returned, and never counted.

## 1. Where the check happens

Every question becomes a four-part query the gateway assembles:

> **who is asking** → **what they asked** → **what they are allowed** → **the answer**

The order is the whole trick. Fetch everything and hide rows afterwards and the
data has already left the database — and any total computed along the way is the
firm's total, not the caller's. Filtering in the middle means a sum is a sum of
*their* rows.

*Analogy: a teller who counts your account, not the vault.*

→ `gateway/mediation.py`, the `compose()` function

## 2. Two ways to be entitled

- **A list on the record** — "these groups may read this". Like a guest list
  stapled to each document. It works, but something must keep rewriting it, and
  while it is being rewritten it is stale.
- **A path through the graph** — you booked the trade; or you are on the team
  covering the client. Like being family: nobody maintains a list, it is simply
  true.

The requirement as stated *is already a path* — "he sees it because he booked it,
she sees it because she covers the client". We store that as relationships and
check it by walking them.

Both are supported, and **both is the steady state rather than a stepping
stone**: relationship rules are naturally paths, role rules ("supervision sees
everything") are naturally lists, and a real model contains some of each.

→ `bundles/iam/bundle.yaml`: `grant_model:` and `grants:`

## 3. Making it fast

Scanning every trade and discarding 99% of them is like walking the whole library
to reshelve books you were never allowed to read. **Anchoring** starts you at your
own shelf: the query begins at the caller and traverses out to what they cover.

On 100,000 trades where the caller may see 1,000: **14× faster**. The advantage
shrinks as visibility grows and disappears around 70%, so anchoring is declared
per tool rather than applied automatically.

→ `anchor:` on a tool definition, composed in `gateway/mediation.py`

## 4. "Why can I see this?"

A guest list proves you are allowed in. A path tells you *why*. Ask about a record
and the answer names the rule and shows the chain:

```
because: covers the client the trade was booked for
path:    (Anna Ross)-[MEMBER_OF]-(coverage-acme)-[COVERED_BY]-(Acme Corp)-[FOR_CLIENT]-(TRD-3001)
```

No list-based system can answer that — an entry records *that* access exists,
never why. This is the access-review and supervision story.

A caller reaching the same record by a role instead of a relationship is reported
honestly, quoting the matched list entry and showing no path. Those two answers
side by side are the clearest illustration of why both models coexist.

→ `explain-access` in `gateway/security_tools.py`

## 5. How it is proved

Declarative cases state that a person **must see** these records and **must not
see** those. The negative assertions are the security tests. It runs in CI and
fails on a leak.

A **differential** mode runs the same question under the list model and the path
model and proves they agree, row for row — how to migrate without asking anyone
to take it on faith.

→ `scripts/check_entitlements.py` + `bundles/iam/entitlement_tests.yaml`

## 6. Relationship to native database controls

Neo4j has two relevant features, and they are different things:

- **Property-based access control** filters rows in the database:
  `GRANT READ {address} ON GRAPH * FOR (n:Email) WHERE n.domain = 'example.com' TO role`.
- **Attribute-based access control** assigns *roles* dynamically from a user's
  token claims or native tags, rather than from a manually maintained mapping.

Together they express a useful slice of the problem — most notably a
team-scoped rule, by creating one role per team and letting token claims assign
it. Where that is possible it is strictly better than doing it in an application:
the database enforces it for **every** client, not only ours.

They do not reach the rest, for structural reasons rather than maturity:

| Requirement shape | Native? |
| --- | --- |
| Team-scoped, single property, low cardinality | **Yes** |
| Derived from a relationship ("booked it") | No — the predicate compares properties, it cannot traverse |
| A list of permitted readers on the row | No — it compares a property to a constant, not list to list |
| Two conditions in one rule | No — a privilege is restricted by a single property |
| Per-record or high-cardinality grants | No — the cardinality lands in the number of roles |

### The layers

| Layer | Question | Enforced by |
| --- | --- | --- |
| 1. Connection | May you connect at all? | SSO, credentials |
| 2. Database authorization | Which labels, properties and property-values may this role read? | Neo4j (RBAC + property rules, roles assigned by attribute) |
| 3. Query mediation | Which *rows* may this caller read, given relationships and list permissions? | This gateway |
| 4. Tool curation | Which questions may be asked at all? | Bundle tool definitions |

**Layer 2 is a floor; layer 3 is expressiveness.** They compose rather than
compete. Anything expressible at layer 2 should be pushed down to it, because a
bug in layer 3 then still cannot leak it. That is a better answer to "what if
your gateway is wrong?" than mediation alone can give.

### Two things to raise in a design discussion

**Database rules apply to the connecting account, not to the end user.** A
gateway holding one service connection would have layer 2 evaluated against the
service account, so per-user rules would not apply at all. Neo4j impersonation
closes this: the service account connects and impersonates the end user per
request, and the user's roles and property rules then apply. Measured here,
impersonation itself is free (within noise on a 100k-node dataset) — but it has
to be designed in deliberately, and it means the service account must hold
`IMPERSONATE` over every principal it may act for.

**Property rules are not free.** The manual warns of significant overhead;
measured on 100,000 nodes with *identical* visible results, a property-scoped
role cost **5.5× a label-scoped one** (+130 ms). So "push it down to the
database" is a correctness and blast-radius argument, not automatically a
performance one. Design layer 2 around labels where possible and reserve property
rules for what genuinely needs them.

Also worth knowing: native `DENY` rules **fail open** when their criteria cannot
be evaluated — a missing property means the restriction does not apply — which is
the same failure mode our `protected_labels` guard exists to catch, and an
argument for keeping both layers rather than either alone.

---

## Coarse map

| Concept | Where |
| --- | --- |
| Query composition, entitlement filter, anchoring | `gateway/mediation.py` |
| Identity resolution, secure read, explain-access | `gateway/security_tools.py` |
| Policy declaration, per use case | `bundles/<name>/bundle.yaml` |
| Curated queries | `bundles/<name>/tools/*.yaml` |
| Proof of correctness | `scripts/check_entitlements.py` |
| Performance evidence | `scripts/bench_mediation.py`, `scripts/sweep_selectivity.py` |

## Say these before being asked

1. **The gateway is the enforcement point, not the database.** Anyone holding
   direct database credentials is bounded only by Neo4j's own authorisation. This
   protects the application and assistant channel, which is the channel in
   question.
2. **This covers reads.** Writes are not mediated; a mediated bundle is read-only
   by design rather than pretending otherwise.
3. **Native controls cover part of this, and we sit on top of them** — see §6.
   Neo4j property-based access control *can* filter rows by a property value, but
   the comparison is against a constant fixed when the privilege is granted; the
   predicate has no access to the calling user. Anything that varies per user must
   therefore be pushed into role membership — one role per distinct entitlement
   value — which works for low-cardinality attributes and not for relationships,
   list-valued permissions, or per-record grants.

## Suggested framing

Lead with §2. *The requirement is already a sentence about relationships, so store
it as relationships instead of flattening it into tables that go stale.*
Everything else is implementation detail hanging off that.
