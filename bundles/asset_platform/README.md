# Asset platform bundle

An institutional platform where research, client interactions and corporate-access
meetings are all *about* assets; where trades are booked against client
counterparties; and where employees have compensation records. Entitlement comes
from three sources with three different owners.

```bash
CYPHER_SHELL="cypher-shell" ./scripts/load_asset_platform.sh   # all five steps + conformance
```

Or by hand, in this order — each step joins to nodes the previous one created:

```bash
cypher-shell -f bundles/asset_platform/data/platform.cypher        # 1. business graph
uv run python scripts/ingest_business_hierarchy.py                 # 2. people, units, reporting
uv run python scripts/ingest_coverage_teams.py                     # 3. coverage
cypher-shell -f bundles/asset_platform/data/identity.cypher        # 4. roles, scope, barriers
cypher-shell -f bundles/asset_platform/data/trading.cypher         # 5. trades, compensation
ACTIVE_BUNDLE=asset_platform uv run python scripts/check_entitlements.py   # 47 cases
```

Getting the order wrong does not fail loudly. It produces a graph with missing
entitlement edges, which **under-grants silently** — only the conformance suite
notices.

## Why this bundle exists

Five entitlement shapes the other bundles do not reach:

| Shape | Why it is hard |
| --- | --- |
| **Taxonomy-scoped** — a role is scoped to a `Sector`, and a record is reached by walking *up* a classification hierarchy | Variable-depth traversal. No access-control list can express it, and an unbounded version grants everything |
| **Two caller classes** — internal `Employee` and external `ClientUser` | Different rules, and a rule for one must not apply to the other. Distinguished by a label on the **caller** |
| **A dated entitlement** — the scope and the coverage carry validity windows | An entitlement graph is a statement about a moment in time; the window is evaluated at query time rather than materialised |
| **A threshold on the caller** — "managing director or above" | An *ordering*, not a set membership. Needs an attribute (`authz.attrs.rankLevel`), because encoding it as principals means one per rank, re-issued on every promotion |
| **Six routes to one record, and a seventh withdrawing it** | Trade entitlement is many unrelated routes at once. The risk is not a missing route — it is one route quietly widening another |

## Three sources, three owners, three refresh cycles

This is the real shape of the problem, and a single seed file hid it.

| Source | Owns | Loaded by |
| --- | --- | --- |
| **business_hierarchy** view | `Employee` + `rankLevel`, the `OrgUnit` tree, `IN_UNIT`, `PART_OF`, `REPORTS_TO` | `scripts/ingest_business_hierarchy.py` — a mechanical projection |
| **coverage_teams** view | `CoverageTeam`, `MEMBER_OF`, `COVERS {validFrom, validTo}` | `scripts/ingest_coverage_teams.py` — likewise |
| **authored policy** | `HAS_ROLE`, `SCOPED_TO`, `RESTRICTED_FOR`, the client-side population | `data/identity.cypher` |

The two projections carry **no business logic**: every output edge is one column
pair of one input row, so swapping the real schema in is editing `COLUMNS` at the
top of the script. Sample extracts are in `data/views/`.

`SCOPED_TO` and `RESTRICTED_FOR` are deliberately **authored, never ingested** —
they exist only to express entitlement, and a barrier a business feed can write is
a barrier a business feed can lift.

Keeping the barrier *edge* authored is not sufficient on its own, because a denial
also has to **reach** the caller. If it reaches them over an edge the grant it
overrides does not use, a feed can sever that edge and lift the barrier with every
`RESTRICTED_FOR` still in place. That is why the restricted-list barrier hangs off
the caller's `CoverageTeam` rather than their `Desk`: the denial and the coverage
grant now traverse the same `MEMBER_OF`, so severing it withdraws both. The
`BARRIER COUPLING` section of the surface report prices what remains. See
[docs/entitlement-edges.md](../../docs/entitlement-edges.md), and:

```bash
uv run python scripts/entitlement_surface.py asset_platform --write-guard business_feed
```

## The model

```
Employee ─HAS_ROLE→ Role ─SCOPED_TO {validFrom,validTo}→ Sector
   │  ├─IN_UNIT→ OrgUnit(:Desk) ─PART_OF→ OrgUnit(:BusinessUnit) ─PART_OF→ OrgUnit(:Division)
   │  ├─REPORTS_TO→ Employee
   │  ├─MEMBER_OF {role}→ CoverageTeam ─COVERS {validFrom,validTo}→ ClientOrg
   │  └─WORKS_IN_REGION→ Region        ClientOrg ←RESTRICTED_FOR─ CoverageTeam / Desk
ClientUser ─HAS_CLIENT_ROLE→ ClientRole   ─WORKS_FOR→ ClientOrg   ─SIGNED_UP_FOR→ Meeting

Asset ─CLASSIFIED_AS→ SubIndustry ─NARROWER_THAN→ Industry ─NARROWER_THAN→ Sector
Asset ─OF_CLASS→ AssetClass        Asset ─ISSUED_BY→ Issuer

Document ─WAS_ABOUT→ Asset   ─AUTHORED_BY→ Employee   ─DISTRIBUTED_TO→ ClientOrg
Interaction ─WAS_ABOUT→ Asset  ─WITH_ORG→ ClientOrg   ←PARTICIPATED_IN─ (either class)
Meeting ─WAS_ABOUT→ Asset
Trade ─BOOKED_BY→ Employee  ─BOOKED_ON→ Desk  ─WITH_COUNTERPARTY→ ClientOrg  ─ON_ASSET→ Asset
Compensation ─COMPENSATION_OF→ Employee
```

**Four modelling rules that make path-based entitlement possible.** Each is the
opposite of a mistake that is easy to make and expensive to unwind:

1. **One relationship type, one meaning.** Classification, taxonomy and org
   structure are `CLASSIFIED_AS`, `NARROWER_THAN` and `PART_OF` — not one shared
   "belongs to". A single overloaded type wired in both directions makes bounded
   traversal impossible: a variable-length walk reaches most of the graph, and a
   rule written over it grants far more than intended.
2. **One direction.** `NARROWER_THAN` and `PART_OF` always point child → parent.
   Mixed directions force undirected traversal, which is where over-reach starts.
3. **Leaf-only classification.** Assets attach at `SubIndustry`; sector membership
   is *derived*. Storing the shortcut too would mean most rules took it and the
   hierarchy was never exercised by a test.
4. **No universal root.** Divisions have no parent. A firm-level node above
   everything is a two-hop bridge between any two subtrees, which defeats every
   bounded supervision rule.

`OrgUnit` carries a second, semantic label (`Desk` / `BusinessUnit` / `Division`)
from the view's `unit_kind`, so a rule can say either "the desk it was booked on"
or "the unit tree above it".

## The cast

| Person | Rank | Unit | Reaches content by |
| --- | --- | --- | --- |
| `ella.moreau` | 3 VP | Equity Research EMEA | **Energy** sector scope, plus what she authored |
| `raj.patel` | 2 | Equity Research EMEA | **Technology** sector scope |
| `priya.raman` | **5 MD** | Global Research | supervises research — and **no trades**, being in another unit |
| `oscar.lindgren` | 4 ED | Institutional Sales EMEA | coverage of Northwind and Kestrel; his reports' compensation |
| `nina.holt` | 2 | Institutional Sales EMEA | the **same** coverage as Oscar, being on the same team |
| `sam.okoye` | 2 | Institutional Sales EMEA | his team covers Rivermark — **restricted for that team**; its Kestrel coverage **expired in 2025** |
| `yuki.tanaka` | 3 VP | Institutional Sales APAC | covers Aster; her Technology scope **expired in 2024** |
| `hana.kim` | **5 MD** | Institutional Client | compensation across her unit |
| `noor.haddad` | **5 MD** | Global Markets | every trade on both markets desks |
| `tomas.vogel` | 4 ED | Equity Derivatives EMEA | his own bookings, and his reports' |
| `felipe.souza` | 2 | Equity Derivatives EMEA | his own bookings |
| `omar.faruq` | 2 | Equity Derivatives EMEA | the desk's flow, **up to 50m** |
| `ingrid.svensson` | 3 VP | Rates EMEA | her own bookings |
| `dana.whitfield` | 4 ED | Control Room | supervisory access-control lists — and **only her own compensation** |
| `mia.torres` | — | **ClientUser** | Northwind's own research, and meetings she signed up for |
| `liam.becker` | — | **ClientUser** | Kestrel's own research |

## The cases worth demonstrating

**`DOC-4` isolates the taxonomy route.** Ella did not write it, is not on a team
covering the organisation it went to, and is not named in its access-control list.
The only route is `Energy → Utilities → Power Generation → AURGD → DOC-4`. If the
hierarchy traversal breaks, that one case fails and nothing else does.

**`DOC-3` is invisible to everyone, including its author and supervision.** Both
hold genuine grants; the embargo overrides them. `explain-access` names what was
overridden:

```
DOC-3  ella.moreau  denied  the document is under pre-publication embargo
       overrode: ['authored this document', "role is scoped to the sector ..."]
```

**`TRD-2` isolates the management line.** Felipe booked it at 80m — above the desk
notional limit — so the desk route does not reach it and Tomas is not a managing
director. `REPORTS_TO` is the only route left.

**Omar's single row is bounded by four mechanisms at once.** Same desk as Tomas, one
rank lower, nobody reporting to him: `TRD-1` visible on the desk route, `TRD-2`
withheld by the notional limit, `TRD-3` granted then withdrawn by the counterparty
restriction, `TRD-4`/`TRD-5` on another desk.

**Priya proves rank is a condition on a path, not a substitute for one.** She is a
managing director and sees no trades at all, because the supervision rule is bounded
by her own subtree.

**Oscar and Nina see an identical blotter, at ranks 4 and 2.** Coverage parity is
structural: both traverse the same `(:CoverageTeam)` node rather than relying on two
projected rows agreeing.

**Sam's empty blotter has two different causes.** His team's Rivermark coverage is
real and the restriction on that team withdraws it; his team's Kestrel coverage expired in
2025 and was never granted. Same row count, different reasons — which
`explain-access` distinguishes and a count cannot.

**The compensation threshold, demonstrated rather than asserted.** Raj reports to
Ella, so the path from Ella to Raj's record exists and matches the rule. She is a
vice president; the rule requires executive director. Promote her by one integer —
no schema change, no principal minted — and the row appears.

## Three things learned building it, kept as comments in the files

**Reference data must be reference data.** `Asset` was permissioned at first, which
looked reasonable and broke every coverage rule that joins to an asset: the filter
drops a row when *any* variable in scope fails, so a caller entitled to an
interaction but not to the asset's sector lost the whole row. A public securities
universe is not the secret; the research about it is.

**`protect:` on reference data denies all of it.** `protect` means strict — the
variable must be explicitly granted, and anything carrying no access-control list is
refused. Naming a reference-data variable there made `asset_universe` return nothing
to anybody.

**Keeping a policy edge authored does not make a barrier immovable.** The
restricted-list barrier used to hang off the caller's desk. Moving one person
between desks — an ordinary HR change, with both `RESTRICTED_FOR` edges present and
correct — lifted it, because the denial reached them by `IN_UNIT` while the coverage
grant it overrode ran through `MEMBER_OF`. Hanging it off the `CoverageTeam` closed
that: the denial and the grant now share an edge, so severing it withdraws both.
It does **not** close the participation route, which shares nothing with either —
and that residual is the general result. A barrier that must be absolute has to be
a condition on the row, like the embargo. `entitlement_surface.py` reports the
severable pairs per denial rather than leaving it to be discovered.

**Per-caller cases do not catch a damaged model.** A deliberate test removed one
employee's `rankLevel` and cut a desk out of the unit tree. All 31 per-caller cases
passed; one person's compensation view had silently collapsed from five records to
one. The invariants named both faults. That is what the `invariant:` cases are for,
and why they run unmediated over the whole graph.

## Constraint worth knowing

`scope` accepts **node variables only**. The entitlement filter applies label
predicates to every one, and a relationship variable fails with a type mismatch. So
a relationship property can *filter* inside `match` but cannot be projected in
`return` — which is why `active_scope_map` applies the validity window in its match
rather than reporting it as a column.
