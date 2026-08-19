# Asset platform bundle

An institutional platform where research, client interactions and corporate-access
meetings are all *about* assets, and assets are classified under a sector
taxonomy. Eighteen labels, each relationship type carrying one meaning.

```bash
cypher-shell -f bundles/asset_platform/data/platform.cypher   # business graph
cypher-shell -f bundles/asset_platform/data/identity.cypher   # people + entitlements
ACTIVE_BUNDLE=asset_platform uv run python scripts/check_entitlements.py   # 21 cases
```

## Why this bundle exists

Three entitlement shapes the other bundles do not reach:

| Shape | Why it is hard |
| --- | --- |
| **Taxonomy-scoped** — a role is scoped to a `Sector`, and a record is reached by walking *up* a classification hierarchy | Variable-depth traversal. No access-control list can express it, and an unbounded version of it grants everything |
| **Two caller classes** — internal `Employee` and external `ClientUser` | Different rules, and a rule for one must not apply to the other. Distinguished by a label on the **caller** |
| **A dated entitlement** — the scope carries a validity window | An entitlement graph is a statement about a moment in time; the window is evaluated at query time rather than materialised |

## The model

```
Employee ─HAS_ROLE→ Role ─SCOPED_TO {validFrom,validTo}→ Sector
   │  ├─WORKS_FOR→ Desk ─PART_OF→ BusinessUnit ─PART_OF→ Division
   │  ├─COVERS→ ClientOrg ←RESTRICTED_FOR─ Desk
   │  └─WORKS_IN_REGION→ Region
ClientUser ─HAS_CLIENT_ROLE→ ClientRole
   │  ├─WORKS_FOR→ ClientOrg
   │  └─SIGNED_UP_FOR→ Meeting

Asset ─CLASSIFIED_AS→ SubIndustry ─NARROWER_THAN→ Industry ─NARROWER_THAN→ Sector
Asset ─OF_CLASS→ AssetClass        Asset ─ISSUED_BY→ Issuer

Document ─WAS_ABOUT→ Asset   ─AUTHORED_BY→ Employee   ─DISTRIBUTED_TO→ ClientOrg
Interaction ─WAS_ABOUT→ Asset  ─WITH_ORG→ ClientOrg   ←PARTICIPATED_IN─ (either class)
Meeting ─WAS_ABOUT→ Asset
```

**Three modelling rules that make path-based entitlement possible.** Each is the
opposite of a mistake that is easy to make and expensive to unwind:

1. **One relationship type, one meaning.** Classification, hierarchy and org
   structure are `CLASSIFIED_AS`, `NARROWER_THAN` and `PART_OF` — not one shared
   "belongs to". A single overloaded type wired in both directions makes bounded
   traversal impossible: a variable-length walk reaches most of the graph, and a
   rule written over it grants far more than intended.
2. **One direction.** `NARROWER_THAN` always points child → parent. Mixed
   directions force undirected traversal, which is where the over-reach starts.
3. **Leaf-only classification.** Assets attach at `SubIndustry`; sector
   membership is *derived*. Storing the shortcut too would mean most rules took
   it and the hierarchy was never exercised by a test.

## The cast

| Person | Class | Reaches content by |
| --- | --- | --- |
| `ella.moreau` | Employee | **Energy** sector scope, plus what she authored |
| `raj.patel` | Employee | **Technology** sector scope, plus what he authored |
| `oscar.lindgren` | Employee | covers Northwind and Kestrel |
| `sam.okoye` | Employee | covers Rivermark — **which is restricted for his desk** |
| `yuki.tanaka` | Employee | covers Aster; her Technology scope **expired in 2024** |
| `dana.whitfield` | Employee | supervision, via access-control lists |
| `mia.torres` | **ClientUser** | Northwind's own research, and meetings she signed up for |
| `liam.becker` | **ClientUser** | Kestrel's own research |

## The cases worth demonstrating

**`DOC-4` isolates the taxonomy route.** Ella did not write it, does not cover the
organisation it went to, and is not named in its access-control list. The only
route is `Energy → Utilities → Power Generation → AURGD → DOC-4`. If the
hierarchy traversal breaks, that one case fails and nothing else does.

**`DOC-3` is invisible to everyone, including its author and supervision.** Both
hold genuine grants; the embargo overrides them. `explain-access` names what was
overridden:

```
DOC-3  ella.moreau  denied  the document is under pre-publication embargo
       overrode: ['authored this document', "role is scoped to the sector ..."]
```

**`INT-2` is invisible to the person who covers it.** Sam's coverage of Rivermark
is real; the restriction on his desk withdraws it. Supervision, on a different
desk, still sees it.

**Yuki's expired scope grants nothing.** She sees `DOC-4` by authorship and
coverage, and not `DOC-2`, which her Technology scope would reach if the window
were still open.

## Two things learned building it, kept as comments in the files

**Reference data must be reference data.** `Asset` was permissioned at first,
which looked reasonable and broke every coverage rule that joins to an asset: the
filter drops a row when *any* variable in scope fails, so a caller entitled to an
interaction but not to the asset's sector lost the whole row. A public securities
universe is not the secret; the research about it is.

**`protect:` on reference data denies all of it.** `protect` means strict — the
variable must be explicitly granted, and anything carrying no access-control list
is refused. Naming a reference-data variable there made `asset_universe` return
nothing to anybody.

## Constraint worth knowing

`scope` accepts **node variables only**. The entitlement filter applies label
predicates to every one, and a relationship variable fails with a type mismatch.
So a relationship property can *filter* inside `match` but cannot be projected in
`return` — which is why `active_scope_map` applies the validity window in its
match rather than reporting it as a column.
