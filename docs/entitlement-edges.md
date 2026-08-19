# Which edges decide access, and how each one can be governed

> Computed, not described:
> ```bash
> uv run python scripts/entitlement_surface.py asset_platform
> uv run python scripts/entitlement_surface.py asset_platform --write-guard business_feed
> ```

An entitlement graph is not a separate subgraph. It is the **projection** of the
graph onto the relationship types and properties named in `security.grants`,
`security.denials` and `security.identity` — so it is derivable from configuration
and can be printed, rather than described in a document that drifts.

That projection is the artefact a security review actually asks for: **the exact
set of relationship types whose modification changes who can read what.** No
relational entitlement store can produce it, because the JOINs live in application
code.

---

## "Entitlement edge" is not a property of an edge

`AUTHORED_BY` is a business fact when you ask who wrote something and an
entitlement route when a grant traverses it. Same edge, same graph. What makes a
relationship type entitlement-bearing is that **a declared rule names it**.

Two consequences worth being explicit about.

**Polarity is per rule, not per edge.** A type traversed by a grant enables; by a
denial, disables; and the same type does both. In `asset_platform`, `IN_UNIT`
reaches a booking desk in a grant and a restricted desk in a denial;
`WITH_COUNTERPARTY` and `WITH_ORG` likewise. So the graph cannot be coloured red
and green — you have to name the rule. Which is why

> authorised iff there is an enabling path and no disabling path

operationalises as *matches a grant pattern and no denial pattern*.

**The set is computable but the write path is not.** Everything above comes out of
the rules. Who *writes* each edge is a deployment fact, so it is declared once, in
`security.ingested_rels`, and it is the declaration that turns the surface from
interesting into actionable.

---

## The split that matters is write ownership

| | Authored | Feed-written |
| --- | --- | --- |
| Written by | a change-controlled process | an automated upstream extract |
| In `asset_platform` | `SCOPED_TO`, `RESTRICTED_FOR`, `HAS_ROLE`, `HAS_CLIENT_ROLE` | the other 18 |
| Available control | **`DENY … ON GRAPH … RELATIONSHIP`** — absolute, needs no testing | change review, conformance in CI after every load |
| Failure to worry about | a deliberate bad change | a routine correct-looking upstream edit |

Four out of twenty-two. That ratio is the honest measure of how much of an
entitlement model can be put behind a write privilege, and it is why the answer for
the rest has to be testing rather than permissions.

`--write-guard <role>` generates the DDL for the authored column and lists the
excluded types with their owners, so the exclusions are visible rather than
implied.

---

## Why not just build parallel rails?

The obvious response to feed-written deciding edges is to stop traversing business
facts: derive a private `ENTITLES_*` edge beside each one, and let entitlement run
on exclusive rails that only the entitlement pipeline may write. It is the right
instinct, and it is worth being precise about when it pays — because usually it
does not.

**A rail is a second recording of a fact, and two recordings can disagree.**

### A pure copy is strictly worse

If the rail is derived mechanically from the business edge, a CRM edit still moves
access — one derivation step later. The risk has been *renamed*, not reduced, and a
new failure mode is added: the copy going stale, which nothing in the query can
detect and no per-caller test can see.

`AUTHORED_BY` is this case. "The author may read what they authored" *is* the
business fact. There is no policy decision to encode, so a rail would carry no
information the original does not.

### A rail earns its keep when the entitlement is not the fact

If only *primary* coverage entitles, or only coverage inside a window, or only
desks above a threshold, then the rail encodes a decision that is nowhere in the
business edge. Real content, worth a distinct type.

But note the cheaper instrument: **a `where:` predicate on the rule expresses the
same restriction with no second recording at all.** That is why this bundle uses
`where` for validity windows and the notional limit rather than minting rails for
them — same expressiveness, nothing to drift.

### The one thing a rail buys that a predicate cannot

**Write-path governance.** A distinct relationship type is a distinct privilege
target. A predicate constrains reads while the feed still owns the edge; a separate
type can be taken away from the feed entirely:

```cypher
DENY CREATE ON GRAPH neo4j RELATIONSHIP SCOPED_TO TO business_feed;
DENY DELETE ON GRAPH neo4j RELATIONSHIP SCOPED_TO TO business_feed;
DENY SET PROPERTY {*} ON GRAPH neo4j RELATIONSHIP SCOPED_TO TO business_feed;
```

That is a real, enforceable control, and it is the only one on this page that does
not depend on anybody remembering to run a test.

### So: the rule of thumb

> Promote to a rail when you need to **revoke someone's ability to write it** — not
> when you want the diagram tidier.

And one hard constraint: **never derive a denial onto a rail.** A grant rail that
misses an edge under-grants, and somebody complains. A denial rail that misses an
edge fails **open**, and nobody does.

---

## When can a feed lift a barrier?

Deny wins, so a denial must override *every* grant for its label. But the denial
only matches while **its own** path holds. That asymmetry is the whole problem:

> A denial **D** can be severed while a grant **G** survives
> **iff D traverses some feed-written edge that G does not.**

Sever that edge and D stops matching while G still does. Nothing is absent from
anybody's results — somebody simply sees more. No per-caller case notices unless it
happens to enumerate that person, and **no invariant over the barrier edge notices
either, because the barrier edge is still there.**

Computed per denial:

```bash
uv run python scripts/entitlement_surface.py asset_platform    # BARRIER COUPLING
```

```
!!    Interaction: the client organisation is restricted for this caller's coverage team
      depends on MEMBER_OF, WITH_ORG; these grants survive if that is severed:
        - participated in this interaction
OK    Document: the document is under pre-publication embargo
      traverses no feed-written edge — structurally total, there is nothing to sever
```

### Worked example: moving a barrier from the desk to the coverage team

The restricted-list barrier originally hung off the caller's `Desk`, reached by
`IN_UNIT` from the HR view. The coverage *grant* it existed to override runs through
`MEMBER_OF`, and shares nothing with it. So:

```
HR feed moves Sam to another desk. RESTRICTED_FOR untouched, both edges present.

  sam sees before:  (nothing — denied)
  sam sees after:   INT-2
```

Hanging it off `CoverageTeam` instead makes the denial traverse `MEMBER_OF` and
`WITH_ORG` — the same edges as the coverage grant. The reorg lift closes: severing
either now withdraws the grant at the same moment it withdraws the denial.

```
  sam after the desk move:      (still denied)
```

**It does not close everything, and the report says so.** Sam also
`PARTICIPATED_IN` that interaction, and attending a meeting has nothing to do with
covering the account:

```
  sam after losing MEMBER_OF:   INT-2      <- grant gone, denial gone, participation survives
```

Which is the general result: **the only structurally total barrier is one that
traverses nothing.** A `where`-only denial depends on the row and on no feed edge,
so there is no edge to sever:

```yaml
- label: Document
  where: "resource.embargoed = true"
```

The cost is that a row condition cannot be group-scoped — it denies everyone,
including the people the barrier was meant to leave alone. Group-scoped *and*
absolute is only available when the caller-side hop is **authored** rather than
ingested (`HAS_ROLE` here), which trades precision for immovability.

So the honest ranking, and the reason this is a report rather than a rule:

| Barrier form | Severable by a feed | Group-scoped |
| --- | --- | --- |
| Row condition (`where` only) | **no** | no — denies everyone |
| Caller-side hop over an **authored** edge | **no** | yes, but only at role granularity |
| Caller-side hop sharing every grant's edges | no, for those grants | yes |
| Caller-side hop sharing some grants' edges | yes, for the rest | yes |

---

## Ingestion, and why it is a projection

The two sources feeding this model are relational views with authoritative
upstream providers. Applications entitle off them today by JOINing the view to
whatever data they are guarding — the JOIN written once per application,
differently each time.

```
business_hierarchy  ->  scripts/ingest_business_hierarchy.py
    unit_id, parent_unit_id  ->  (:OrgUnit)-[:PART_OF]->(:OrgUnit)
    employee_id, unit_id     ->  (:Employee)-[:IN_UNIT]->(:OrgUnit)
    employee_id, manager_id  ->  (:Employee)-[:REPORTS_TO]->(:Employee)
    rank_level               ->  Employee.rankLevel   (a caller attribute)

coverage_teams      ->  scripts/ingest_coverage_teams.py
    team_id, employee_id     ->  (:Employee)-[:MEMBER_OF {role}]->(:CoverageTeam)
    team_id, account_id      ->  (:CoverageTeam)-[:COVERS {validFrom, validTo}]->(:ClientOrg)
```

Each output edge is one column pair of one input row. **No business logic lives in
the projection**, which is what makes it trustworthy: it cannot decide anything, so
it cannot decide anything wrong. All the entitlement logic is in `bundle.yaml`,
where it is declared, computable and tested.

Two properties follow:

- **Swapping the real schema in is a column remap.** `COLUMNS` at the top of each
  script, and nothing else.
- **The mess upstream stays upstream.** Whatever tangle produces the authoritative
  view, the projection consumes its *output*. Reimplementing the lineage is not on
  offer, and should not be.

### The team is a node, not a flattened edge

`(:Employee)-[:MEMBER_OF]->(:CoverageTeam)-[:COVERS]->(:ClientOrg)` rather than
`(:Employee)-[:COVERS]->(:ClientOrg)` per row. Three reasons, the second decisive:

1. It matches the source. The view is called coverage_*teams*; the team is what the
   business names, staffs and audits, and flattening it loses a question the
   business asks.
2. **Coverage parity becomes structural.** Two people on one team see the same book
   because they traverse the same node — not because two rows happened to agree.
   With a flattened edge, parity is a coincidence a partial refresh can break
   silently. There is a conformance case asserting it between an executive director
   and an associate.
3. It splits cheaply. A shared intermediate node is the natural cut point when
   identity is separated from data; a per-person edge has none.

---

## Thresholds are not principals

`rankLevel >= 5` is an **ordering**, and a set of principal names cannot express
one. Encoding it as membership means minting a principal per rank and re-issuing it
on every promotion — the materialisation problem this whole design exists to avoid,
in miniature.

So `security.identity.caller_attributes` lifts declared properties of the caller
into `authz.attrs`, resolved once in the prelude:

```yaml
identity:
  caller_attributes: [rankLevel]
grants:
  - label: Compensation
    via: "(caller)<-[:REPORTS_TO*1..4]-(:Employee)<-[:COMPENSATION_OF]-(resource)"
    where: "authz.attrs.rankLevel >= 4"
```

Three properties worth naming:

- **Read once, not per row.** A threshold over a million rows costs one property
  read.
- **It crosses a separated-identity boundary.** A scalar travels where the caller
  *node* cannot, so every threshold works unchanged under `source: composite` and
  `source: remote`. Verified by running the full suite under both.
- **NULL fails closed in a grant and open in a denial.** `NULL >= 5` is NULL, so a
  missing attribute withholds a grant (visible — somebody's rows vanish) and
  withdraws a barrier (invisible). Hence the load-time check that every referenced
  attribute is declared, and the invariant that every caller carries every deciding
  attribute.

Demonstrated rather than asserted: a vice president with a direct report holds the
path to that report's compensation and is refused. Change one integer and the row
appears. No schema change, no principal minted.

---

## The failure directions, in one table

| Damage | Consequence | What catches it |
| --- | --- | --- |
| Deciding type declared, absent from graph — in a **grant** | under-grants | `must_see` case; surface report |
| …in a **denial** | **fails OPEN** | invariant asserting the edge is present |
| Missing caller attribute | grant withheld silently | invariant over the attribute |
| Cut in the unit tree | supervision under-grants | invariant reaching a `Division` |
| Cycle in `REPORTS_TO` | **over-grants** across the cycle | invariant; bounded walk limits blast radius |
| Coverage window absent | grant withheld silently | invariant over `validFrom` |
| Feed-written edge changed correctly-but-unexpectedly | access moves | the computed list, plus conformance after every load |
| Feed severs an edge a **denial** uses but a grant does not | **barrier lifts, fails OPEN** | `BARRIER COUPLING` in the surface report — no per-caller case or edge-presence invariant sees it |
| An author's `where` with a top-level `OR` | **disclosure**, under a separated topology only | `check_entitlements.py --identity-source remote` |

The last row is not hypothetical. `AND` binds tighter than `OR`, so composing an
unbracketed predicate with the boundary cut predicate produced
`(cut AND notional) OR rank`, which collapses to `true` for any caller clearing the
rank bar. Co-located it was harmless, because the predicate was alone in its
subquery. The engine now brackets author predicates unconditionally, and the
topology sweep exists so the next one of these is caught by CI rather than by
someone changing a setting.
