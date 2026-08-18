# Design: path-based grants

Status: **proposed**, with the load-bearing assumptions validated by spike (§5).
Nothing here is implemented yet.

Today a row is readable when its `Permissions.Read` list intersects the caller's
principals. That is a *materialised* grant: some upstream process must write the
list onto every row. This document proposes *derived* grants, where a row is
readable when a path exists between the caller and the row.

---

## 1. The key separation

Path grants are attractive for two different reasons, and conflating them leads
to a bad design. They are separate capabilities and should ship separately:

| | What it changes | What it buys |
| --- | --- | --- |
| **Grant model** | *How we decide* whether a caller may read a row | No materialisation, no staleness window, provenance |
| **Anchoring** | *Which rows the query examines* | Performance |

Measured on a 100,000-client dataset where the caller is entitled to 1,000: the
same answer costs **700,001 db hits / 97 ms** scanned-and-filtered, versus
**5,015 db hits / 2.5 ms** anchored. That 38× is the anchoring prize, not the
grant-model prize — swapping the predicate alone does not deliver it.

## 2. Grant model

### Configuration

```yaml
security:
  grant_model: path            # property (default) | path | both
  grants:
    - label: Trade
      via: "(caller)-[:BOOKED]->(resource)"
      reason: "booked the trade"
    - label: Trade
      via: "(caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(resource)"
      reason: "covers the client the trade was booked for"
    - label: Communication
      via: "(caller)-[:PARTICIPANT_IN]->(resource)"
      reason: "was a participant in the conversation"
```

`caller` and `resource` are the two bound endpoints the engine supplies. Several
grants for one label are OR-ed. Note that the config reads back as the
requirement itself — *"he can see it because he booked it, and she can see it
because she covers the client"* — which makes it reviewable by the people who own
the policy rather than only by engineers.

### Filter semantics

The current filter tests list membership. Under `path` it becomes an existence
check with both endpoints already bound:

```cypher
WHERE (NOT resource:Trade OR EXISTS {
        MATCH (caller)-[:BOOKED]->(resource) }
     OR EXISTS {
        MATCH (caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(resource) })
  AND (NOT resource:Communication OR EXISTS { ... })
```

Everything else is unchanged: the filter still applies to **every variable in
scope**, unlabelled/ungoverned nodes still flow as reference data, and `protect:`
still means strict. The prelude keeps binding the caller node, which is what
supplies `caller`.

One structural change is required: the filter currently iterates
`all(resource IN [a, b, c] WHERE …)`. Since grants dispatch on label, it becomes
one clause per scope variable instead. That is clearer to read anyway.

### `both` mode — the migration path

`grant_model: both` makes a row readable when **either** the ACL matches **or** a
path exists. This matters more than it looks: an organisation with materialised
control tables cannot flip everything at once. `both` lets derived grants run
alongside the existing ACLs, per label, until the ACLs are retired.

It also enables **differential validation**, which is the strongest adoption
argument available: run the same query under `property` and under `path` and
assert the result sets are identical. That converts "trust our new model" into
"we reproduce your current decisions exactly, row for row, and here is the test
that proves it." I would add this to `check_entitlements.py` as a case type:

```yaml
- name: derived grants agree with materialised ACLs
  differential: true          # run under both models, assert identical
  principal: joe.hart@bank.com
  tool: client_activity
  args: {client: Acme Corp}
  id_field: tradeId
```

## 3. Anchoring

Anchoring is where the performance is, and it cannot be derived safely from an
arbitrary query — rewriting a caller's Cypher to start somewhere else is the same
class of problem as parsing out its `RETURN`, and I am not willing to do it
implicitly.

Instead the tool author declares it:

```yaml
match: |
  MATCH (t:Trade)-[:FOR_CLIENT]->(c)      # c arrives already bound
scope: [t, c]
anchor:
  variable: c
  via: "(caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(c)"
```

The engine emits the anchor traversal *before* the tool's match, so the match
receives `c` pre-bound and restricted to what the caller can reach:

```cypher
CALL { …prelude… RETURN authz, caller }
MATCH (caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(c)      // engine-emitted
CALL {
  WITH c
  MATCH (t:Trade)-[:FOR_CLIENT]->(c)                            // author's match
  RETURN t, c
}
WITH … WHERE <filter>                                            // unchanged
```

The contract is: *if you declare an anchor, your match must use that variable as
already bound rather than binding it fresh.*

### Safety: the two error directions are not symmetric

An early draft of this design claimed a wrong anchor "costs speed, never
correctness". **That is only half true, and the implementation corrected it.**
An anchor restricts what the match examines, and the filter downstream can only
remove rows further — never restore them:

| Anchor is | Effect |
| --- | --- |
| too **broad** | extra rows examined, filter removes them — correct, merely slower. **Cannot leak.** |
| too **narrow** | rows the caller *is* entitled to are never matched. **False negatives.** |

So an anchor cannot cause a disclosure, but it *can* silently hide data. The rule
is that an anchor must reach every route by which a caller may be entitled to
that variable. In practice, anchor tools whose question **is** the anchor
("trades for clients I cover"), not general tools serving several roles that
reach the data differently.

This is enforced rather than trusted: `scripts/check_entitlements.py` runs every
anchored tool a second time *without* its anchor and fails on any difference.
Wrongly anchoring a tool that also serves desk and operations users produces:

```
FAIL  owning desk sees the trade
      ANCHOR MISMATCH: anchor HID ['TRD-3001'] from tom.becker@bank.com
```

Documented as `ANCHOR_SAFETY` in `gateway/mediation.py`.

## 4. Provenance — `explain-access`

Derived grants make a question answerable that no ACL system can answer: *why*
can this caller see this row? The grant that matched **is** the reason, and the
path is the evidence.

```
explain-access(resource: "TRD-3001")
  → granted by: "covers the client the trade was booked for"
    path: (joe.hart) -[:MEMBER_OF]-> (coverage-acme) <-[:COVERED_BY]- (Acme Corp) <-[:FOR_CLIENT]- (TRD-3001)
```

Cheap to build once grants are declarative, and it is the demo moment that an
audit or control-room audience reacts to.

## 5. Spike results

Measured on 100,000 trades where the caller is entitled to 1,000. All four
variants return the identical answer (1,000 trades / 1,000 clients).

| Composition | p50 | db hits |
| --- | --- | --- |
| scan + ACL filter *(today)* | 107.8 ms | 506,508 |
| scan + path filter | 185.3 ms | 2,501,499 |
| **anchored + ACL filter** | **5.5 ms** | **13,530** |
| **anchored + path filter** | **9.3 ms** | **28,521** |

**1. `EXISTS { }` short-circuits, but path filtering is more expensive than ACL
filtering.** The plan shows `SelectOrSemiApply` with a `Limit`, confirming it
stops at the first match rather than materialising. But a traversal per row costs
more than a list-membership test: 2.8× the db hits for one grant, and each
additional grant adds more. **This is the design's most important negative
result** — path grants must never be sold as a filter-path optimisation. Their
value is no materialisation, no staleness, and provenance. All of the performance
comes from anchoring, which is exactly why §1 separates them.

**2. Anchoring survives the composition boundary.** The engine-shaped form —
prelude subquery, engine-emitted anchor, the tool's match in a `CALL { }` with the
anchored variable passed in — reaches 5.5 ms / 13,530 hits against 107.8 ms /
506,508 today: **20× faster, 37× fewer db hits**, and within 2× of hand-written
Cypher (3.6 ms / 7,025). The planner pushes through the subquery boundary.

Retaining the entitlement filter on top of the anchor — the safety property that
a wrong anchor costs speed but never correctness — costs about 2.4 ms and 2,000
db hits on an already-20×-faster query. That is cheap insurance, and it means the
guarantee never rests on the author declaring the anchor correctly.

**3. Label dispatch is free.** Db hits were identical (25,549) whether the filter
carried 2, 6, or 12 grant clauses; wall-clock moved from 9.1 ms to 10.2 ms across
that range. The `NOT var:Label` guard short-circuits before the traversal, so a
bundle may declare grants for many record types without penalty on queries that
touch few.

### Selectivity sweep

Anchoring's advantage depends entirely on how much of the population the caller
can see, so the headline number must be quoted with its ratio.
`scripts/sweep_selectivity.py` measures the curve (100,000 clients / 100,000
trades; identical results at every point):

| caller sees | scan | anchored | speedup | db-hit ratio |
| --- | --- | --- | --- | --- |
| 0.5% (500 clients) | 95.9 ms | 2.4 ms | **40×** | 56× |
| 1% (1,000) | 98.3 ms | 3.0 ms | **33×** | 37× |
| 5% (5,000) | 130.0 ms | 8.2 ms | 16× | 10× |
| 10% (10,000) | 162.0 ms | 13.7 ms | 12× | 5.5× |
| 25% (25,000) | 242.1 ms | 38.7 ms | 6× | 2.4× |
| 50% (50,000) | 316.7 ms | 97.2 ms | 3× | 1.3× |
| 66% (66,000) | 94.9 ms | 91.5 ms | 1.0× | 1.1× |
| 75% (75,000) | 94.7 ms | 104.0 ms | 0.9× | 1.0× |
| 100% (100,000) | 89.7 ms | 126.9 ms | 0.7× | 0.8× |

**Anchoring pays below roughly 70% visibility and is a slight net cost above it.**
Anchored cost rises monotonically with the caller's reachable set, as expected —
it reads what the caller can see.

The scan curve is not monotonic, which is worth understanding rather than
dismissing as noise: it peaks near 50% and then *falls*. `any(x IN acl WHERE
x IN principals)` short-circuits on a match but scans the whole ACL on a miss,
and the principal list itself grows with visibility. Cost therefore approximates
`misses x |acl| x |principals| + hits x 1 x |principals|`, which is maximised when
about half the rows miss. **List-ACL filtering is most expensive at intermediate
selectivity, not at full visibility.**

### Consequences for the design

- Path grants ship for their **semantics**, not their speed. A bundle that adopts
  `path` without an anchor should expect to be slower than ACLs — the reference
  must say so plainly.
- `both` mode is more valuable than first thought: it lets a bundle keep ACL
  filtering (cheaper) while deriving grants for correctness and provenance.
- Anchoring is where the investment pays, and it is independent of grant model —
  **anchored + ACL is the fastest combination measured**. It should therefore be
  built first, and can ship before path grants entirely.
- **Anchoring must stay opt-in per tool**, as designed. An automatic anchor would
  penalise broad-visibility callers, and the sweep shows that cost is real above
  ~70%. Note the subtlety: the anchor is declared on a *tool*, but its benefit
  depends on the *caller* — one tool may serve a salesperson at 1% visibility and
  a supervisor at 100%. The mix decides. Where most callers are narrow and a few
  are broad, it is strongly net positive.
- A future refinement, not proposed here: the prelude already knows the caller's
  reachable set, so the engine could choose anchored or scanning per call. Worth
  revisiting only if a real deployment has a genuinely bimodal caller population.

Secondary risks: grant patterns are interpolated into Cypher, so they are
author-trusted config at the same level as a tool's own query (never caller
input); and variable-length grant patterns are expensive per row, so the
reference should recommend bounded patterns.

## 6. Scope

Revised by the spike results — anchoring moves first, because it is where the
performance is and it is independent of the grant model.

**Built.** Anchoring (`anchor:` on a mediated tool) is implemented: the prelude
exposes the caller node, `compose()` emits the anchor traversal ahead of the
tool's match with a `WITH DISTINCT` so multiple paths to one anchor node cannot
multiply rows, and the conformance harness verifies anchored equals unanchored.

Measured through the engine on 100,000 trades, caller entitled to 1,000:

| Tool shape | unanchored | anchored | speedup |
| --- | --- | --- | --- |
| returns an aggregate | 121.1 ms | 8.5 ms | **14×** |
| returns 1,000 sorted rows | 150.1 ms | 25.4 ms | **6×** |

Both return identical results. The gap between them is the point: **anchoring
accelerates matching, not returning.** The ~17 ms of row materialisation and
sorting is common to both variants, so a tool that returns many rows sees a
smaller ratio than one that aggregates. Quote the shape along with the number —
the isolated composition measured 20–33×, and a row-returning tool will not
reach that.

**Built.** `grant_model` (`property` | `path` | `both`), grants declared per
label as caller-to-resource patterns with a reason, and `differential: true`
cases in the conformance harness.

Two things the implementation established that the design had wrong or unstated:

- **A permissive default cannot be OR-ed with a restrictive one.** The first cut
  treated a label with no declared grant as ungoverned reference data. Under
  `both` that default OR-ed away the property model's restriction entirely, and
  `Deal` and `Request` — governed by ACLs but carrying no path grants — became
  readable by everyone. The conformance harness caught it as a LEAK. Governance is
  now decided by `protected_labels` ∪ granted labels, so governed-but-ungranted is
  **denied**.
- **Pure `path` is not a complete model for this data, by construction.** Role
  based routes ("supervision reads everything", "the owning desk reads its
  trades") are not statements about a path from the caller to the row, so they
  cannot be expressed as grants. Measured against the same 22 cases:

  | grant_model | result |
  | --- | --- |
  | `property` | 22 pass |
  | `path` | 17 pass, 5 fail — **all under-permissive, zero leaks** |
  | `both` | 22 pass |

  This makes `both` the steady state rather than a staging post: relationship
  derived entitlements are naturally paths, role-based ones are naturally ACLs,
  and a real model contains some of each. Grants are therefore required to bind
  `resource`, and that rule is enforced at load — a "grant" that ignores the row
  is a role rule and belongs in the property model.

**Build after that:** `explain-access`, which needs declarative grants.

**Not building:** automatic anchor inference from an arbitrary query; grant
patterns over relationships rather than nodes; write-side grants.
