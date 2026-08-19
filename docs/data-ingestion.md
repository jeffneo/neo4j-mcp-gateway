# What each entitlement model requires you to ingest

Every mechanism in this engine is a claim on somebody's data pipeline. This is
that bill, stated per mechanism, so the choice is made by the people who will
maintain the feed rather than discovered by them afterwards.

Three questions decide it:

1. **What must exist in the data database** for the check to evaluate?
2. **What writes it, and how often?**
3. **What breaks when the feed is late or wrong**, and does it fail open or closed?

---

## The four mechanisms side by side

| | Materialised ACL | Path grant, co-located | Path grant, **node cut** | Path grant, **property cut** |
| --- | --- | --- | --- | --- |
| In the data database | `Permissions.Read` on every protected row | the full identity graph | proxy nodes + data-side edges | one property per boundary |
| Written by | an entitlement expansion job | identity sync | identity sync (partial) + business feed | the business feed alone |
| Update on a team move | rewrite every affected row | one edge | one edge | one property per client |
| Rows touched per change | |records the person can reach| | 1 | 1 | |clients the team covers| |
| Fails | **open** if a row is missed | closed | closed | closed |
| Needs identity co-located | no | **yes** | no | no |

The last row is why the split topologies exist; the "rows touched" row is why path
grants exist at all.

---

## 1. Materialised ACL (`grant_model: property`)

**Ingest:** a list of principal names on every protected record, under
`security.permissions_property`.

```cypher
SET record.`Permissions.Read` = ['coverage-emea-am', 'compliance-review']
```

**Who writes it:** an upstream expansion job that knows the entitlement rules and
flattens them per record. This is the job that already exists in most banks, and
the one whose cost prompted this whole design — the expansion is
`|records| x |principals entitled|`, recomputed whenever anyone moves.

**Failure mode: fails OPEN, and this is the important one.** A record that is
*missing* its ACL is treated as reference data and flows to everyone. That is why
`security.protected_labels` exists and why
[`scripts/validate_bundle.py`](../scripts/validate_bundle.py) fails the build when
a protected label has a row without one. Run it in CI against production-shaped
data, not only against the demo dataset.

> A stale ACL is invisible. Nothing in the query can tell a correct empty list
> from one that was never written.

---

## 2. Path grant, identity co-located (`identity.source: graph`)

**Ingest:** the identity graph itself, in the same database as the business data.

```
(:User)-[:MEMBER_OF]->(:AdGroup)         who is in what
(:Client)-[:COVERED_BY]->(:AdGroup)      the seam
(:User)-[:LOGGED]->(:Interaction)        relationship-derived grants
```

**Who writes it:** identity sync for the membership half, the business feed for
the seams. `identity.cypher` in the `asset_platform` bundle is deliberately a
separate file from `platform.cypher` for exactly this reason — two owners, two
refresh cycles.

**Update semantics:** a team move is **one relationship write**. Nothing is
recomputed, because nothing was materialised. This is the property that kills the
staleness argument, and it is worth stating in those terms: the entitlement is
not stored, it is derived, so there is no window in which it can be stale.

**Streaming fit:** this is the shape that suits a change feed. The Neo4j Connector
for Kafka can apply membership and coverage changes as they happen. Note what
makes that tractable — it is not the streaming, it is that **the work downstream
of each event is O(1)**. A queue feeding an expansion job is racing a
combinatorial workload; a queue feeding an edge write is not.

**Reload ordering matters.** `identity.cypher` builds the seams, so it must run
*after* any reload of `platform.cypher`. Get it backwards and coverage
relationships are missing — callers silently see less than they should.
`check_entitlements.py` catches this as a `must_see` failure, which is the reason
to run it after every load.

---

## 3. Path grant, node cut (separated identity, proxy nodes)

**Ingest:** proxy nodes in the data database for every node a grant or anchor
traverses, plus the data-side relationships.

| Identity database | Data database |
| --- | --- |
| `(:User)` full attributes | `(:User {email})` — identifier only |
| `(:AdGroup)` full attributes | `(:AdGroup {name})` — identifier only |
| `(:User)-[:MEMBER_OF]->(:AdGroup)` | `(:Client)-[:COVERED_BY]->(:AdGroup)` |
| | `(:User)-[:LOGGED]->(:Interaction)` |

A proxy script builds these — see the recipe in
[`entitlement-testing-tutorial.md`](entitlement-testing-tutorial.md). Where the
business feed already records the fact as a property (an author's address, a
covering team) the proxies can be derived from it and need nothing from the
identity system; where it does not, they have to be fed separately.

**Division of labour, and why it is defensible:** *membership* stays in the
identity store — the high-churn half, the part that changes when someone moves
desks. What replicates is *coverage* and *authorship*, which are facts about the
business records: "which team covers this client" belongs with the client.

**New group:** the proxy must exist before any client points at it. A missing
proxy makes the `COVERED_BY` write fail (loudly) or silently create an
unlabelled node, depending how the feed is written — use `MERGE` on a
constrained key.

**Fails closed.** A missing proxy means a traversal that matches nothing, so the
caller sees less. Detectable with `check_entitlements.py`.

---

## 4. Path grant, property cut (separated identity, **no proxies**)

**Ingest:** nothing extra at all, provided the boundary is already recorded as a
property — which it usually is, because the business feed knows it.

```cypher
CREATE (:Client {name: 'Northwind', coverageTeam: 'coverage-emea-am'})
CREATE (:Interaction {interactionId: 'INT-2006', loggedByEmail: 'nadia@…'})
```

```yaml
identity:
  boundary_properties:
    Client: coverageTeam
    Interaction: loggedByEmail
```

**This is the cheapest option to operate** and the one to reach for first. No
proxy nodes, no identity graph in the data database, one fewer hop per check.

**Update on a team move:** rewrite `coverageTeam` on the clients that team
covers. That is more rows than the single edge of option 2, but far fewer than
option 1 — it scales with |clients|, not |clients x records x principals|.

**Index it.** `CREATE INDEX FOR (c:Client) ON (c.coverageTeam)`. Without it, an
anchored query on this property is a label scan.

**The hazard: two recordings of one fact.** If a deployment keeps *both*
`Client.coverageTeam` and `(:Client)-[:COVERED_BY]->(:AdGroup)`, they can drift
apart, and the two cut strategies will then disagree about who may read what.
Nothing in the query can detect that. Two defences:

- keep a `differential:` conformance case, which runs the same question under
  both models and fails on divergence;
- or pick one recording and derive the other at load time — build the
  relationship *from* the property, so the two cannot disagree.

The second is strictly better where you can do it.

---

## Choosing

```
Is the identity graph allowed to live beside the business data?
├─ yes → option 2. One edge per change, nothing materialised, full expressiveness.
└─ no  → Is the boundary already a property on a business record?
         ├─ yes → option 4. No proxies, no extra feed, cheapest to run.
         └─ no  → option 3. Proxy nodes, fed from the business side.

Option 1 stands alone: use it for entitlements that are ROLE-based rather than
relationship-derived ("supervision reads everything"), where there is no path to
walk. `grant_model: both` runs it alongside any of the others, which is the
steady state rather than a migration step.
```

---

## Operational checks worth wiring into CI

| Check | Command | Catches |
| --- | --- | --- |
| Protected rows all carry an ACL | `validate_bundle.py` | option 1 failing open |
| Entitlements still correct after a load | `check_entitlements.py` | seams that did not rebuild |
| The two recordings agree | a `differential:` case | property/relationship drift |
| Anchors did not narrow entitlement | automatic in `check_entitlements.py` | a too-narrow cut hiding rows |

The first is the one that fails open. Prioritise it accordingly.
