# Tutorial: testing entitlements on Aura

Run the entitlement model against Aura and see the entitlement graph for yourself.
Uses the **`asset_platform`** bundle: research, client interactions and meetings
are all about assets, and entitlement follows from sector scope, client coverage
and personal involvement.

The dataset ships as **two Cypher files**, and that separation is the point:

| File | Contains |
| --- | --- |
| `data/platform.cypher` | the business graph — `Asset`, `Document`, `Interaction`, `Meeting`, `Sector`/`Industry`/`SubIndustry`, `AssetClass`, `Issuer`, `ClientOrg` |
| `data/identity.cypher` | identity **and the entitlement layer** — `Employee`, `ClientUser`, `Role`, `ClientRole`, `Desk`, `BusinessUnit`, `Division`, `Region` |

`platform.cypher` has no dependency on identity — it records authorship and
attendance as *properties* and lets `identity.cypher` promote them. So load
**platform first, identity second**. The seams identity creates:

```
(:Role)-[:SCOPED_TO {validFrom,validTo}]->(:Sector)     research scope
(:Employee)-[:COVERS]->(:ClientOrg)                     coverage
(:ClientOrg)-[:RESTRICTED_FOR]->(:Desk)                 a barrier
(:Document)-[:AUTHORED_BY]->(:Employee)                 authorship
(:Employee|:ClientUser)-[:PARTICIPATED_IN]->(:Interaction)
```

---

## Which topology

| | Arrangement | Instances | Works | Path grants |
| --- | --- | --- | --- | --- |
| **A** | one graph, identity and data intermingled | 1 | ✅ | ✅ |
| **B** | one graph, distinct subgraphs joined at named seams | 1 | ✅ | ✅ |
| **C** | two domains, **each with its own copy of identity** | 2 | ✅ | ✅ |
| **D** | identity in one database, data in another, **naively** | 2 | ❌ | — |
| **E** | same split, joined by a **composite database** | 1–2 | ✅ | ✅ via proxies |
| **F** | same split, identity over a **second connection** | 2 | ✅ | ✅ via proxies |

**A is the default and what the bundle ships configured for.** B is the same
physically and differs in modelling discipline. C is the multi-bundle setup.

**D, E and F are one physical arrangement with three answers.** D is what happens
if you split identity from data and change nothing else: it fails. E and F both
work and both keep path grants. They are documented as a **recipe** below rather
than as a shipped bundle — the config is small, and carrying a second copy of a
bundle to demonstrate a config flag was not worth the duplication.

---

## Setup

```bash
uv sync
cp .env.example .env      # then put your Aura connection in it
```

```bash
NEO4J_URI=neo4j+s://xxxxxxxx.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<your password>
NEO4J_DATABASE=neo4j
NEO4J_MCP_ALLOW_IMPERSONATION=true
NEO4J_MCP_PRINCIPAL=dana.whitfield@bank.com
```

> Aura Free and Professional give **one database per instance**, always called
> `neo4j` — so "two databases" means two instances. Business Critical and Virtual
> Dedicated Cloud support several per instance, which makes C, E and F possible on
> one.

---

## Topology A — one instance, one graph

```bash
cypher-shell -a "$NEO4J_URI" -u neo4j -p "$NEO4J_PASSWORD" \
  -f bundles/asset_platform/data/platform.cypher
cypher-shell -a "$NEO4J_URI" -u neo4j -p "$NEO4J_PASSWORD" \
  -f bundles/asset_platform/data/identity.cypher

ACTIVE_BUNDLE=asset_platform uv run python scripts/check_entitlements.py
```

Expect **21 passed, 0 failed**. Then run the same tool as different people:

```bash
ACTIVE_BUNDLE=asset_platform uv run python scripts/try_tool.py \
  asset_research principal=ella.moreau@bank.com
ACTIVE_BUNDLE=asset_platform uv run python scripts/try_tool.py \
  asset_research principal=raj.patel@bank.com
ACTIVE_BUNDLE=asset_platform uv run python scripts/try_tool.py \
  asset_research principal=mia.torres@northwind.com
```

| Caller | Sees | Because |
| --- | --- | --- |
| `ella.moreau` | DOC-1, DOC-4 | Energy sector scope, plus what she authored |
| `raj.patel` | DOC-2 | Technology sector scope |
| `oscar.lindgren` | DOC-1, DOC-2 | covers Northwind and Kestrel |
| `yuki.tanaka` | DOC-4 | authorship and coverage — her Technology scope **expired in 2024** |
| `dana.whitfield` | DOC-1, DOC-2, DOC-4 | supervision, by access-control list |
| `mia.torres` | DOC-1 | **client user**, her own organisation's research |
| `liam.becker` | DOC-2 | **client user**, a different organisation |

`DOC-3` is invisible to everyone, including its author and supervision: it is
under embargo, and a denial beats every route.

### Look at the entitlement graph

```cypher
// The entitlement layer on its own — the three facts that decide everything
MATCH p = (:Role)-[:SCOPED_TO]->(:Sector) RETURN p;
MATCH p = (:Employee)-[:COVERS]->(:ClientOrg) RETURN p;
MATCH p = (:ClientOrg)-[:RESTRICTED_FOR]->(:Desk) RETURN p;

// One caller's full reach
MATCH p = (e:Employee {email:'ella.moreau@bank.com'})-[:HAS_ROLE|COVERS|WORKS_FOR]->()
RETURN p;

// The taxonomy route, end to end
MATCH p = (:Role {name:'research-energy'})-[:SCOPED_TO]->(:Sector)
          <-[:NARROWER_THAN*1..2]-(:SubIndustry)<-[:CLASSIFIED_AS]-(:Asset)
RETURN p;
```

And ask why a specific record is visible:

```bash
ACTIVE_BUNDLE=asset_platform uv run python - <<'EOF'
import asyncio, warnings; warnings.simplefilter("ignore")
from gateway.config import Config
from gateway.yaml_tools import Neo4jExecutor
from gateway.security_tools import build_explain_access
c = Config.from_env(active_bundle="asset_platform"); ex = Neo4jExecutor(c)
fn = build_explain_access(c, ex).fn
async def main():
    for res, who in [("DOC-4","ella.moreau@bank.com"), ("DOC-4","raj.patel@bank.com"),
                     ("DOC-3","ella.moreau@bank.com"), ("INT-2","sam.okoye@bank.com")]:
        r = await fn(resource=res, principal=who)
        print(f"  {res} {who:26} granted={bool(r['granted'])}")
        for d in r.get("deniedBy", []): print("     DENIED:", d["reason"])
        for g in r.get("grantedBy", []): print("     via:   ", g["reason"])
asyncio.run(main())
EOF
```

`ella` reaches `DOC-4` through the sector scope alone — she did not write it,
does not cover the organisation it went to, and is not named in its list. `raj`
gets no answer at all, and it is deliberately indistinguishable from the record
not existing. `DOC-3` and `INT-2` report the grants a denial overrode.

---

## Topology B — one instance, two governed subgraphs

Physically identical to A; the difference is treating identity as a separate
asset with its own lifecycle. Reload either half independently, then check the
seams survived:

```cypher
MATCH (:Role)-[r:SCOPED_TO]->(:Sector)      RETURN count(r) AS researchScopes;
MATCH (:Employee)-[r:COVERS]->(:ClientOrg)  RETURN count(r) AS coverage;
MATCH (:Document)-[r:AUTHORED_BY]->()       RETURN count(r) AS authorship;
MATCH (:ClientOrg)-[r:RESTRICTED_FOR]->()   RETURN count(r) AS barriers;
```

> Reload order matters: `identity.cypher` builds the seams, so run it **after**
> any reload of `platform.cypher`. Get it backwards and callers silently see less
> than they should. `check_entitlements.py` catches exactly this as a `must_see`
> failure, which is the reason to run it after every load.

---

## Topology C — two instances, one bundle each

Each bundle carries its own connection, so two bundles can sit on two instances.
Put a git-ignored `.env` in each bundle directory:

```bash
# bundles/asset_platform/.env
NEO4J_URI=neo4j+s://<instance-1>.databases.neo4j.io
NEO4J_PASSWORD=<password 1>
NEO4J_DATABASE=neo4j
```

```bash
ACTIVE_BUNDLE=asset_platform,iam uv run neo4j-mcp-gateway
```

Tools are namespaced per bundle — `asset_platform_asset_research`,
`iam_secure-read-cypher` — and each keeps its own security posture and
connection.

**Each instance needs its own identity data.** That is the load-bearing
difference from D: identity is *replicated* into every instance that enforces
against it, so no query crosses the boundary. Prove it by deleting identity from
one instance and watching only that bundle's callers fall to zero:

```cypher
MATCH (n {source:'ap-identity'}) DETACH DELETE n;    // one instance only
```

> The gateway **refuses to start** if an `open` bundle and a `mediated` bundle
> resolve to the same database. Separate instances avoid it entirely.

Note that invariant cases are **graph-wide**, so two bundles sharing one database
will each report the other's access-control entries as unresolvable principals.
That is correct — the graph is shared — but a bundle wanting clean invariants
wants its own database.

---

## Topology D — identity split out, and nothing else changed

**This does not work, and it is worth understanding why before reaching for E or F.**

Mediation composes a *single* Cypher statement: resolve the caller, run the
query, filter the results. A statement cannot traverse two databases, so with
identity in one instance and business data in another the authorization prelude
has nothing to resolve against.

`identity.cypher` will load people and roles into the identity instance and
silently create **no seams** — there are no `Sector` or `ClientOrg` nodes there to
attach to. Callers then resolve to their roles but match no data, and
`check_entitlements.py` reports `must_see` failures. It fails closed, which is the
right direction, but it fails.

Four ways out:

1. **Replicate identity into each business database** — topology C. Simplest,
   keeps everything.
2. **A composite database** — topology E.
3. **A second connection** — topology F.
4. **Push the check into the database** with native controls. See §6 of
   [`entitlement-model-brief.md`](entitlement-model-brief.md).

---

## Topologies E and F — the recipe

Both declare a **separated identity source**, and both keep path grants by
cutting each traversal at a node present in *both* databases — Neo4j's documented
[proxy node pattern](https://neo4j.com/docs/operations-manual/current/scalability/composite-databases/concepts/).
A relationship cannot span two graphs; a traversal can be cut.

### The config

```yaml
security:
  identity:
    # ---- E: composite database (one statement, one transaction) ----
    source: composite
    identity_graph: fed.identity
    data_graph: fed.data

    # ---- F: a second connection (two round trips, no composite needed) ----
    # source: remote
    # remote_env_prefix: IDENTITY        # reads IDENTITY_NEO4J_URI etc. from .env

    # Optional and preferable where it applies: cut at a PROPERTY instead of a
    # proxy node, which removes the proxies entirely and drops a hop.
    # boundary_properties:
    #   Document: authorEmail
```

For F, the identity connection comes from the environment, never the manifest:

```bash
IDENTITY_NEO4J_URI=neo4j+s://<identity-instance>.databases.neo4j.io
IDENTITY_NEO4J_USERNAME=neo4j
IDENTITY_NEO4J_PASSWORD=<password>
IDENTITY_NEO4J_DATABASE=neo4j
```

### Standing it up

```cypher
CREATE DATABASE datadb WAIT;
CREATE DATABASE identitydb WAIT;
-- E only:
CREATE COMPOSITE DATABASE fed;
CREATE ALIAS fed.data     FOR DATABASE datadb;
CREATE ALIAS fed.identity FOR DATABASE identitydb;
```

```bash
cypher-shell -d datadb     -f bundles/asset_platform/data/platform.cypher
cypher-shell -d datadb     -f <the proxy script below>
cypher-shell -d identitydb -f bundles/asset_platform/data/identity.cypher
```

Then `NEO4J_DATABASE=fed` (E) or `NEO4J_DATABASE=datadb` plus the `IDENTITY_*`
variables (F), and run the suite as normal.

### The proxy script

Every node a grant or anchor traverses needs a proxy in the data database, and
the data-side relationships must live there. For a group-routed model the cut is
cheap — one stub per group. For `asset_platform` it is **not** cheap, and that is
worth knowing:

```cypher
// Proxies for a separated asset_platform. Load into the DATA database, after
// platform.cypher. Identifier properties only — no attributes, no lifecycle.
MERGE (:Role {name:'research-energy'});
MERGE (:Role {name:'research-tech'});
MATCH (r:Role {name:'research-energy'}), (s:Sector {name:'Energy'})
MERGE (r)-[:SCOPED_TO {validFrom: date('2026-01-01'), validTo: date('2026-12-31')}]->(s);
MATCH (r:Role {name:'research-tech'}), (s:Sector {name:'Technology'})
MERGE (r)-[:SCOPED_TO {validFrom: date('2026-01-01'), validTo: date('2026-12-31')}]->(s);

// Caller stubs, derived from properties platform.cypher already wrote.
MATCH (d:Document) WHERE d.authorEmail IS NOT NULL
MERGE (e:Employee {email: d.authorEmail}) MERGE (d)-[:AUTHORED_BY]->(e);
MATCH (i:Interaction) WHERE i.participantEmails IS NOT NULL
UNWIND i.participantEmails AS who
MERGE (p {email: who}) MERGE (p)-[:PARTICIPATED_IN]->(i);

// Coverage, desks and barriers have no property to derive from, so they are
// declared here — which is the point below.
MERGE (dk:Desk {name:'Institutional Sales EMEA'});
MATCH (o:ClientOrg {name:'Rivermark Industries'}), (dk:Desk {name:'Institutional Sales EMEA'})
MERGE (o)-[:RESTRICTED_FOR]->(dk);
MATCH (e:Employee {email:'sam.okoye@bank.com'}), (dk:Desk {name:'Institutional Sales EMEA'})
MERGE (e)-[:WORKS_FOR]->(dk);
```

### The design lesson worth carrying into an architecture conversation

**How splittable a model is depends on how its grants are rooted.**

A grant routed through a *shared intermediate node* — `caller → group → client →
record` — cuts cleanly at the group: one stub per group, and membership stays in
the identity store where it churns.

`asset_platform`'s grants are mostly rooted **at the caller** — authorship,
attendance, coverage, desk. Cutting those means proxying the callers themselves
plus the entitlement edges, which replicates most of the identity graph. It works,
and it is measurably correct, but it is not cheap.

So if separability matters, that is a reason to route entitlements through a
named scope or group rather than attaching them person-to-record. It is a
modelling decision with an architectural consequence, and it is easier to make
before the model is loaded than after.

### What the split costs, measured

On 100,000 records with a caller entitled to ~1,000
([`scripts/bench_separation.py`](../scripts/bench_separation.py), Neo4j 2025.10.1),
every variant returning the identical answer:

| Composition | p50 | db hits |
| --- | --- | --- |
| co-located, scan + ACL | 76.9 ms | 304,018 |
| co-located, scan + path | 98.4 ms | 1,999,518 |
| **co-located, anchored + path** | **4.4 ms** | 22,540 |
| split, scan + split-path | 68.7 ms | 1,302,001 |
| **split, anchored-split** | **4.7 ms** | 17,008 |
| **split, anchored + ACL** | **3.1 ms** | 9,008 |

Anchoring is worth ~17× **either side of the split**, and the split forms are not
slower than their co-located equivalents — splitting a grant is marginally
*cheaper*, because the data-side half starts at the row and tests a value rather
than expanding from the caller on every row examined.

Unanchored, both sit at 70–100 ms and grow with the dataset. That is the number to
quote when anchoring is unavailable — not a cost of separating identity.

### Two constraints the engine enforces rather than degrading quietly

- A grant whose **identity relationship appears after the cut** is rejected at
  load. The suffix would run where those edges do not exist, match nothing, and
  deny silently — a false negative, the error direction nobody notices.
- **Anchors** and tools that reference `caller` are rejected under a separated
  source. Express the scoping with a parameter or an anchor pattern the engine can
  cut.

---

## What to look at

| Question | Command |
| --- | --- |
| Does the model hold? | `check_entitlements.py` |
| What does each persona see? | `try_tool.py asset_research principal=<email>` |
| Why can they see it? | the `explain-access` snippet in topology A |
| Is the model itself sound? | the `invariant:` cases in `entitlement_tests.yaml` |
| What does mediation cost? | `bench_mediation.py asset_platform` |
| What does separation cost? | `bench_separation.py --database benchdb` |
| Many-group callers? | `bench_principal_scale.py --database benchdb` |
| What do native controls cost? | `bench_native_controls.py` (Enterprise) |

## The cast

| Person | Class | Reaches content by |
| --- | --- | --- |
| `ella.moreau` | Employee | Energy sector scope, authorship |
| `raj.patel` | Employee | Technology sector scope, authorship |
| `oscar.lindgren` | Employee | covers Northwind and Kestrel |
| `sam.okoye` | Employee | covers Rivermark — **restricted for his desk** |
| `yuki.tanaka` | Employee | coverage and authorship; Technology scope **expired** |
| `dana.whitfield` | Employee | supervision, by access-control list |
| `mia.torres` | **ClientUser** | Northwind's research, meetings she signed up for |
| `liam.becker` | **ClientUser** | Kestrel's research |
