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

**The critical safety property: the anchor is an optimisation, the filter is the
guarantee.** The filter still runs over everything in scope. A wrong or missing
anchor costs speed, never correctness. This mirrors `protect:` versus derived
protection — declarations make things faster or stricter, but security never
depends on the author getting a declaration right.

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

### Consequences for the design

- Path grants ship for their **semantics**, not their speed. A bundle that adopts
  `path` without an anchor should expect to be slower than ACLs — the reference
  must say so plainly.
- `both` mode is more valuable than first thought: it lets a bundle keep ACL
  filtering (cheaper) while deriving grants for correctness and provenance.
- Anchoring is where the investment pays, and it is independent of grant model —
  **anchored + ACL is the fastest combination measured**. It should therefore be
  built first, and can ship before path grants entirely.

Secondary risks: grant patterns are interpolated into Cypher, so they are
author-trusted config at the same level as a tool's own query (never caller
input); and variable-length grant patterns are expensive per row, so the
reference should recommend bounded patterns.

## 6. Scope

Revised by the spike results — anchoring moves first, because it is where the
performance is and it is independent of the grant model.

**Build first:** anchoring (`anchor:` on a mediated tool), which delivers 20×
against today's composition regardless of how grants are expressed.

**Build next:** `grant_model` config, path filter semantics, `both` mode, and
differential validation in the conformance harness — for materialisation,
staleness and provenance rather than speed.

**Build after that:** `explain-access`, which needs declarative grants.

**Not building:** automatic anchor inference from an arbitrary query; grant
patterns over relationships rather than nodes; write-side grants.
