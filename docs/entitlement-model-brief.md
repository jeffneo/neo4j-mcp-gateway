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
3. **Native database roles cannot express this.** They scope access by label and
   property, not by values in a row — "rows whose readers include one of my
   groups" would need a role per combination of entitlements. Value-based
   entitlement needs mediation of this kind.

## Suggested framing

Lead with §2. *The requirement is already a sentence about relationships, so store
it as relationships instead of flattening it into tables that go stale.*
Everything else is implementation detail hanging off that.
