# Tutorial: testing entitlements on Aura

Run the entitlement model against Aura and see the identity graph for yourself.
Uses the `client_platform` bundle — an institutional client platform where clients
consume research, analytics, execution and data products, and the commercial
question is which product to offer next — plus `client_platform_split`, the same
platform with the identity graph in a different database.

The dataset ships as **two separate Cypher files** — that separation is the point
of this tutorial:

| File | Contains | Labels |
| --- | --- | --- |
| `data/platform.cypher` | the business graph | `Client`, `Product`, `UsageSummary`, `Interaction`, `Opportunity`, `ResearchNote` |
| `data/identity.cypher` | the identity graph | `User`, `AdGroup`, `Desk` |

They meet at exactly three seams, all created by `identity.cypher`:

```
(:Client)-[:COVERED_BY]->(:AdGroup)     team coverage
(:User)-[:COVERS]->(:Client)            named individual coverage
(:User)-[:LOGGED]->(:Interaction)       authorship
```

`platform.cypher` has **no dependency** on the identity graph — it records the
covering team as a property and lets `identity.cypher` promote it to a
relationship. That is what makes the separated topologies below possible, so load
**platform first, identity second**.

---

## Which topology to try

| | Arrangement | Instances | Works today | Path grants |
| --- | --- | --- | --- | --- |
| **A** | one graph, identity and business data intermingled | 1 | ✅ | ✅ |
| **B** | one graph, distinct subgraphs joined at defined seams | 1 | ✅ | ✅ |
| **C** | two business domains, **each with its own copy of identity** | 2 | ✅ | ✅ |
| **D** | identity in one database, the data in another, **naively** | 2 | ❌ | — |
| **E** | same split, joined by a **composite database** | 1–2 | ✅ | ✅ via proxy nodes |
| **F** | same split, identity resolved over a **second connection** | 2 | ✅ | ❌ |

A and B are the same physically and differ in modelling discipline; B is what you
would actually govern. C is the multi-bundle setup.

**D, E and F are the same physical arrangement with three different answers.**
D is what happens if you split identity from data and change nothing else — it
fails. E and F are the two supported ways to make that split work. **E keeps path
grants** by cutting each traversal at a node present in both databases; F gives
them up. Read D first; it explains why.

### C and D are not "one instance vs two" — read this before skipping

Both C and D use two instances, so the instance count is not what separates them.
**What separates them is whether a single query has to cross the boundary.**

```
  C — works                              D — does not work
  ┌── instance 1 ──────────┐             ┌── instance 1 ─────┐
  │ client platform data   │             │ identity graph    │
  │ + identity  (a copy)   │             │ (only)            │
  └────────────────────────┘             └───────────────────┘
  ┌── instance 2 ──────────┐             ┌── instance 2 ─────┐
  │ iam data               │             │ business data     │
  │ + identity  (a copy)   │             │ (only)            │
  └────────────────────────┘             └───────────────────┘
  Every query resolves the caller        Resolving the caller and reading the
  and reads the data in ONE place.       data are in DIFFERENT places, and one
  The boundary is between DOMAINS.       Cypher statement cannot span them.
```

In C the identity graph is **replicated** — instance 1 and instance 2 each hold
their own copy, so each is independently self-sufficient. The split is *domain vs
domain*: client-platform data over here, IAM data over there, neither needing the
other. That is why it works, and it is why `identity.cypher` is a standalone file
you can run against as many instances as you like.

In D the identity graph is **not** replicated: it lives in one instance and is
expected to authorize data sitting in another. Mediation composes a *single*
Cypher statement — resolve the caller, run the query, filter the results — and a
statement cannot traverse two databases, so the authorization prelude has nothing
to resolve against.

The distinction is worth being precise about in an architecture conversation,
because "put identity in its own database" sounds like good hygiene and is the
one arrangement that does not work on its own. The fix is **replication** (C), or
declaring a separated identity source so the engine composes the query
differently (**E** and **F**). Note that E's split still requires replicating the
*seam nodes* into the data constituent — see
[how E keeps path grants](#how-e-keeps-path-grants-proxy-nodes) — so "no duplication" is
never actually on the menu.

---

## Setup

```bash
git clone <repo> && cd neo4j-mcp-gateway
uv sync
cp .env.example .env
```

Put your Aura connection in `.env` — from the credentials file Aura gives you at
instance creation:

```bash
NEO4J_URI=neo4j+s://xxxxxxxx.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<your password>
NEO4J_DATABASE=neo4j
```

> Aura Free and Professional give you **one database per instance**, always called
> `neo4j`. "Two databases" therefore means **two instances**. AuraDB Business
> Critical and Virtual Dedicated Cloud support more databases per instance, in
> which case topology C can also be done with one instance and two databases.

---

## Topology A — one instance, one graph

The simplest thing that works. Both halves in the same database.

```bash
export NEO4J_URI=... NEO4J_USERNAME=neo4j NEO4J_PASSWORD=... NEO4J_DATABASE=neo4j

cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
  -f bundles/client_platform/data/platform.cypher
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
  -f bundles/client_platform/data/identity.cypher
```

Enable impersonation so you can act as each persona, then run the conformance
suite:

```bash
export ACTIVE_BUNDLE=client_platform
export NEO4J_MCP_ALLOW_IMPERSONATION=true
export NEO4J_MCP_PRINCIPAL=grace.okonjo@bank.com

uv run python scripts/check_entitlements.py
```

Expect **12 passed, 0 failed**. Then try a tool as different people:

```bash
uv run python scripts/try_tool.py client_opportunities principal=evan.brooks@bank.com
uv run python scripts/try_tool.py client_opportunities principal=peter.lindqvist@bank.com
```

Same tool, same question — `evan` sees one opportunity worth $120,000, compliance
sees four worth $900,000. **The total is computed after filtering**, so each
caller gets their own honest number rather than the firm's.

### Look at the identity graph

In Aura's Query browser:

```cypher
// The identity graph on its own
MATCH p = (u:User)-[:MEMBER_OF]->(:AdGroup) RETURN p;

// The seams — where identity meets the business graph
MATCH p = (:User)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(:Client) RETURN p;

// One person's full reach: groups, covered clients, and what they logged
MATCH p = (u:User {email:'lena.fischer@bank.com'})-[:MEMBER_OF|COVERS|LOGGED|ON_DESK]->()
RETURN p;
```

And ask the gateway why a specific record is visible:

```bash
uv run python - <<'EOF'
import asyncio, warnings; warnings.simplefilter("ignore")
from gateway.config import Config
from gateway.yaml_tools import Neo4jExecutor
from gateway.security_tools import build_explain_access
c = Config.from_env(); ex = Neo4jExecutor(c)
fn = build_explain_access(c, ex).fn
async def main():
    for who in ["nadia.haddad@bank.com", "sofia.rossi@bank.com", "evan.brooks@bank.com"]:
        r = await fn(resource="INT-2006", principal=who)
        print(who, "->", "GRANTED" if r["granted"] else "no access")
        for g in r.get("grantedBy", []):
            print("   ", g["reason"], "\n    ", g["path"])
asyncio.run(main())
EOF
```

`nadia` reaches it *only* through `LOGGED` — she does not cover that client — so
the answer shows a path. `sofia` reaches the same record through her coverage
team. `evan` gets no access, and the answer deliberately does not reveal whether
the record exists.

---

## Topology B — one instance, two subgraphs

Physically identical to A; the difference is that you treat identity as a
separate asset with its own lifecycle. Load the same two files, then govern them
apart:

- reload `identity.cypher` on its own when people move teams — it wipes and
  rebuilds only `source:'cp-identity'` nodes
- reload `platform.cypher` on its own when business data refreshes
- check the seams have survived a reload:

```cypher
MATCH (:Client)-[r:COVERED_BY]->(:AdGroup) RETURN count(r) AS teamCoverage;
MATCH (:User)-[r:COVERS]->(:Client)        RETURN count(r) AS namedCoverage;
MATCH (:User)-[r:LOGGED]->(:Interaction)   RETURN count(r) AS authorship;
```

> Reload order matters: `identity.cypher` builds the seams, so run it **after**
> any reload of `platform.cypher`, or coverage relationships will be missing and
> callers will silently see less than they should. `check_entitlements.py` catches
> exactly this — a seam that failed to rebuild shows up as a `must_see` failure.

This is the arrangement to govern in production: one graph, two clearly owned
subgraphs, joined only at seams you can enumerate and test.

---

## Topology C — two Aura instances, one bundle each

Two **business domains** on two instances, each instance holding its own copy of
the identity graph. Each bundle carries its own connection, so put a git-ignored
`.env` in each bundle directory:

```bash
# bundles/client_platform/.env
NEO4J_URI=neo4j+s://<instance-1>.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<password 1>
NEO4J_DATABASE=neo4j
```

```bash
# bundles/iam/.env
NEO4J_URI=neo4j+s://<instance-2>.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<password 2>
NEO4J_DATABASE=neo4j
```

Serve both from one endpoint:

```bash
ACTIVE_BUNDLE=client_platform,iam uv run neo4j-mcp-gateway
```

Tools are namespaced per bundle — `client_platform_client_opportunities`,
`iam_secure-read-cypher` — and each bundle keeps its own security posture and its
own connection. Validate each in turn:

```bash
ACTIVE_BUNDLE=client_platform,iam uv run python scripts/validate_bundle.py
```

**Each instance needs its own identity data** — run `identity.cypher` against
both. This is the load-bearing difference from topology D: the identity graph is
replicated into every instance that must enforce against it, so no query ever has
to cross the boundary. That is a real production pattern — identity synced into
each domain database — and it is why `identity.cypher` is a standalone file with
no dependency on any particular business dataset.

Prove the replication is doing the work: drop identity from one instance and
watch that bundle's callers fall to zero rows while the other bundle is
unaffected.

```cypher
// against the client_platform instance only
MATCH (n {source:'cp-identity'}) DETACH DELETE n;
```

`check_entitlements.py` then fails `must_see` on every client_platform case while
the `iam` bundle keeps passing — the two instances are genuinely independent.
Reload `identity.cypher` to restore it.

> The gateway **refuses to start** if an `open` bundle and a `mediated` bundle
> resolve to the same database, since the open bundle's unfiltered tools would
> read the rows the mediated one protects. Separate instances avoid this
> entirely.

---

## Topology D — identity in a different database, and nothing else changed

**This does not work, and it is worth understanding why before reaching for E or F.**

Mediation composes a *single* Cypher statement: resolve the caller, run the
query, filter the results. A single statement cannot traverse two databases, so
if the identity graph lives in instance 1 and the business data in instance 2,
the authorization prelude has nothing to resolve against.

If you try topology D as it stands, `identity.cypher` will load its people and
groups into the identity instance and silently create **no seams** — there are no
`Client` nodes there to attach to. Callers then resolve to their groups but match
no business data, and `check_entitlements.py` reports `must_see` failures rather
than anything dangerous. It fails closed, which is the right direction, but it
fails.

You have four options:

1. **Replicate identity into each business database** — topology C. Simple,
   works now, keeps everything including path grants.
2. **Join the two databases with a composite database** — topology E below.
3. **Resolve identity over a second connection** — topology F below.
4. **Push the check into the database** with native controls, so the business
   instance enforces per-user rules without needing the identity graph locally.
   See §6 of [`entitlement-model-brief.md`](entitlement-model-brief.md).

### What options 2 and 3 cost you

Both give up the caller **node** in the data query:

> A composite database refuses to import an entity across a `USE` boundary
> (`22N16`), and a second connection has no caller node in the data database
> at all.

| | Composite (E) | Remote (F) |
| --- | --- | --- |
| Per-caller row filtering | ✅ | ✅ |
| Aggregates computed after filtering | ✅ | ✅ |
| Curated-only posture, harness, `explain-access` | ✅ | ✅ |
| One statement, one transaction | ✅ | ❌ two round trips, consistency window |
| **Path grants** | ✅ kept, via proxy nodes | ❌ `grant_model: property` only |
| **Anchoring** | ❌ | ❌ |
| Tools scoping to `caller` | ❌ must use a parameter | ❌ |
| Extra requirement | proxy nodes in the data constituent | none |

The engine **refuses to start** rather than degrading quietly: an anchor, a tool
referencing `caller`, path grants under `remote`, or a grant that cannot be cut
safely all fail at load with a message saying why.

### How E keeps path grants: proxy nodes

A *relationship* cannot span two graphs. A *traversal* can still be split at a
node that exists in both — the documented
[proxy node pattern](https://neo4j.com/docs/operations-manual/current/scalability/composite-databases/concepts/):
one label present in both constituents, carrying full data in one and only its
identifier in the other.

Our grant already passes through such a node:

```
(caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(resource)
└──── resolve in fed.identity ────┘ └────────── traverse in fed.data ──────────┘
                        ^ AdGroup exists in BOTH; the group NAME crosses as a value
```

**You author the grant once.** `client_platform_split` declares the same four
grants, in the same words, as the co-located bundle. The engine finds the cut by
walking from `caller` and consuming the leading run of identity relationships
(`identity.group_rels`), then re-roots the data-side half at the proxy:

```cypher
-- authored
(caller)-[:MEMBER_OF]->(:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(resource)
-- emitted, inside USE fed.data
EXISTS { MATCH (cut:AdGroup)<-[:COVERED_BY]-(:Client)<-[:FOR_CLIENT]-(o)
         WHERE any(k IN $groupKeys WHERE cut[k] IN authz.authzPrincipals) }
```

A grant with no group hop cuts at the caller instead, which is how authorship
survives — `(caller)-[:LOGGED]->(resource)` becomes a match on the `User` proxy
by `principalId`.

**The filter runs inside the `USE` block** for this source, because the outer
composite query rejects every graph access:

```
42NA1: Graph access operations are not supported on composite databases.
```

Filtering there is also strictly better — rows are discarded before they cross
the boundary.

**What it costs is replication surface.** Every node a grant passes through needs
a proxy in the data constituent, and the data-side relationships must live there.
[`data/proxies.cypher`](../bundles/client_platform_split/data/proxies.cypher)
builds them from properties `platform.cypher` already wrote, so it needs nothing
from the identity graph:

| Identity constituent | Data constituent |
| --- | --- |
| `(:User)` full attributes | `(:User {email})` — proxy |
| `(:AdGroup)` full attributes | `(:AdGroup {name})` — proxy |
| `(:User)-[:MEMBER_OF]->(:AdGroup)` | `(:Client)-[:COVERED_BY]->(:AdGroup)` |
| | `(:User)-[:LOGGED]->(:Interaction)` |

Membership stays in identity — the high-churn half, the part that changes when
someone moves desks. What replicates is coverage and authorship, which are facts
*about* the business records.

**The refusal that matters.** If an identity relationship appears *after* the
boundary, the suffix would run where those edges do not exist, match nothing, and
deny silently. False negatives are the error direction nobody notices, so the
engine rejects such a grant at load rather than composing it.

---

## Topologies E and F — the split, made to work

Both use the **`client_platform_split`** bundle: the same client platform as
above, with `grant_model: property` and the identity graph elsewhere. It ships
configured for E; switching to F is two lines in `bundle.yaml`.

Load the halves into two databases — the data side also gets the proxy nodes. On
self-managed Enterprise (or AuraDB Business Critical / Virtual Dedicated Cloud,
which support several databases per instance):

```cypher
CREATE DATABASE datadb WAIT;
CREATE DATABASE identitydb WAIT;
```

```bash
cypher-shell -d datadb     -f bundles/client_platform/data/platform.cypher
cypher-shell -d datadb     -f bundles/client_platform_split/data/proxies.cypher
cypher-shell -d identitydb -f bundles/client_platform/data/identity.cypher
```

`identity.cypher` finds no `Client` nodes in `identitydb` and creates no seams
there — expected, and exactly the situation topology D failed on. The seams now
live in `datadb`, built by `proxies.cypher` from properties `platform.cypher`
already wrote.

### E — composite database

```cypher
CREATE COMPOSITE DATABASE fed;
CREATE ALIAS fed.data     FOR DATABASE datadb;
CREATE ALIAS fed.identity FOR DATABASE identitydb;
```

`bundle.yaml` already declares the constituents:

```yaml
identity:
  source: composite
  identity_graph: fed.identity
  data_graph: fed.data
```

Point `.env` at the **composite** database and run:

```bash
NEO4J_DATABASE=fed ACTIVE_BUNDLE=client_platform_split \
  uv run python scripts/check_entitlements.py
```

Expect **13 passed, 0 failed**. The engine composes one statement: the prelude
resolves the caller under `USE fed.identity`, and the tool's match, the
entitlement filter and the split path grants all run under `USE fed.data`.
**One statement, one transaction, two databases** — so unlike F there is no
consistency window between resolving and reading.

### F — a second connection

Comment out the three composite lines in `bundle.yaml` and uncomment:

```yaml
identity:
  source: remote
  remote_env_prefix: IDENTITY
```

You must also set `grant_model: property` and remove the `grants:` block — a
remote source has no proxy in the data database to re-root a traversal at, and
the engine refuses to start otherwise. That is the concrete difference between
E and F, enforced rather than documented.

The identity connection comes from the environment, never from the manifest —
same rule as every other connection here:

```bash
# .env
NEO4J_URI=neo4j+s://<data-instance>.databases.neo4j.io
NEO4J_DATABASE=neo4j
IDENTITY_NEO4J_URI=neo4j+s://<identity-instance>.databases.neo4j.io
IDENTITY_NEO4J_USERNAME=neo4j
IDENTITY_NEO4J_PASSWORD=<identity password>
IDENTITY_NEO4J_DATABASE=neo4j
```

```bash
ACTIVE_BUNDLE=client_platform_split uv run python scripts/check_entitlements.py
```

The ACL-derived cases pass; the authorship case fails, because without a
traversal into the data constituent `nadia` loses `INT-2006`. That is the honest
difference between E and F, and the reason to prefer E when you can run a
composite database.

What F buys instead is the most independent identity store available here — a
different instance, a different region, its own credentials, its own lifecycle,
and no proxy nodes to maintain. It is also the extension point for an
**external** entitlement service: implement `IdentitySource` in
[`gateway/identity_sources.py`](../gateway/identity_sources.py), register it, and
the rest of the engine is unchanged.

### The case that proves the split is honest

`INT-2006` is readable by `nadia.haddad` **only** because she logged it — a
traversal from the caller that no ACL entry expresses. Three cases pin it down:

```
PASS  authorship reaches a record no ACL entry grants
PASS  the covering team reaches both, through the group proxy
PASS  a caller with neither route sees nothing
```

Ask `explain-access` the same question under each bundle — change only
`ACTIVE_BUNDLE` and the connection:

```bash
uv run python - <<'EOF'
import asyncio, os, warnings; warnings.simplefilter("ignore")
from gateway.config import Config
from gateway.yaml_tools import Neo4jExecutor
from gateway.security_tools import build_explain_access
c = Config.from_env(active_bundle=os.environ["ACTIVE_BUNDLE"]); ex = Neo4jExecutor(c)
fn = build_explain_access(c, ex).fn
async def main():
    for who in ["nadia.haddad@bank.com", "sofia.rossi@bank.com"]:
        r = await fn(resource="INT-2006", principal=who)
        print(f"  {who:24} granted={r['granted']:<6} "
              f"{[g['reason'] for g in r.get('grantedBy', [])]}")
asyncio.run(main())
EOF
```

```
client_platform         nadia  granted=True   ['logged this interaction']
                        sofia  granted=True   ['covers the client this interaction was with']

client_platform_split   nadia  granted=True   ['logged this interaction']
                        sofia  granted=True   ['covers the client this interaction was with']
                        evan   granted=False  []
```

Identical answers, identical reasons, two different topologies. The suite goes
further and compares whole result sets: **24 comparisons across three record
types and eight callers, zero divergence.** The grant patterns are authored once
and mean the same thing on either side of the split.

---

## The hard entitlement scenarios

The `client_platform` bundle above demonstrates coverage, product roles and
authorship. The three scenarios that decide whether a model is credible on a
sales-and-trading floor live in the **`iam`** bundle, and each is already a
conformance case rather than a story. Load it and run them:

```bash
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
  -f bundles/iam/data/iam_demo.cypher

ACTIVE_BUNDLE=iam uv run python scripts/check_entitlements.py
```

Expect **22 passed, 0 failed**. What those cases prove:

| # | Scenario | Cases | The hard part |
| --- | --- | --- | --- |
| 1 | **Private client communication.** A salesperson's direct 1:1 with a client is readable by the participants, *not* by the rest of the coverage team. | `participant sees their own private client chat`, `coverage colleague cannot see a private 1:1 chat`, `unrelated coverage sees no Acme communications` | Two records on the same client differ in visibility: `COMM-1002` (team email thread) reaches the coverage group, `COMM-1001` (1:1 chat) reaches only Anna. **Entitlement is per-record, not per-client** — any model that grants at the client level fails here. |
| 2 | **Coverage parity.** Two salespeople covering the same corporate see *exactly* the same request book. | `coverage team members see an identical request book`, `coverage parity holds on the anchored tool` | Asserted as set equality between principals (`same_for`), not as a count. Divergence between two people who should be identical is the failure mode that erodes trust in an entitlement system, and it is invisible to per-user spot checks. |
| 3 | **Booked trade.** A trade booked by one salesperson on behalf of a client. | `booker sees the trade they booked`, `coverage team sees the client trade`, `owning desk sees the trade`, `settlements sees the trade`, `unrelated coverage cannot see the trade`, `private side cannot see markets trades` | Five *different* routes to one record — booker, coverage, desk, settlements, supervision — and two negative controls. The negatives are the test; the positives are easy. |

A fourth scenario nobody asks for up front but everyone raises in review:

| 4 | **Information barrier.** A wall-crossed deal is need-to-know, and broad rights in one domain must not imply access in another. | `named deal team sees the wall-crossed deal`, `supervision does not cross the information barrier`, `sales cannot see the wall-crossed deal` | Supervision sees every communication firm-wide **and still cannot see the deal**. This is the case that shows the model expresses restriction, not just accumulation — a role-hierarchy model where "supervisor ⊇ everyone" cannot represent it at all. |

### Seeing scenario 1 for yourself

Two communications with the same client, two different shapes of entitlement:

```cypher
MATCH (c:Communication)
OPTIONAL MATCH (u:User)-[:PARTICIPANT_IN]->(c)
RETURN c.commId AS id, c.channel AS channel,
       c.`Permissions.Read` AS acl, collect(u.email) AS participants
ORDER BY id;
```

```
"COMM-1001", "chat",  ["anna.ross@bank.com", "compliance-supervision"], ["anna.ross@bank.com"]
"COMM-1002", "email", ["coverage-acme",      "compliance-supervision"], ["anna.ross@…","joe.hart@…","sam.diaz@…"]
```

`COMM-1002` is a team email thread whose ACL names a *group*. `COMM-1001` is a
1:1 chat whose ACL names an *individual* — which is exactly the entry that has to
be written, and later revoked, for every private conversation on the floor. Ask
why each is visible:

```bash
ACTIVE_BUNDLE=iam uv run python - <<'EOF'
import asyncio, warnings; warnings.simplefilter("ignore")
from gateway.config import Config
from gateway.yaml_tools import Neo4jExecutor
from gateway.security_tools import build_explain_access
c = Config.from_env(); ex = Neo4jExecutor(c)
fn = build_explain_access(c, ex).fn
async def main():
    for res in ["COMM-1001", "COMM-1002"]:
        for who in ["anna.ross@bank.com", "joe.hart@bank.com"]:
            r = await fn(resource=res, principal=who)
            print(f"{res}  {who:22} -> {'GRANTED' if r['granted'] else 'no access'}")
            for g in r.get("grantedBy", []):
                print("        ", g["reason"])
asyncio.run(main())
EOF
```

```
COMM-1001  anna.ross@bank.com     -> GRANTED
             was a participant in the conversation
COMM-1001  joe.hart@bank.com      -> no access
COMM-1002  anna.ross@bank.com     -> GRANTED
             was a participant in the conversation
COMM-1002  joe.hart@bank.com      -> GRANTED
             was a participant in the conversation
```

This is the clearest argument for path grants. Joe covers the same client as
Anna, and on `COMM-1001` he correctly sees nothing. On `COMM-1002` the engine
does not fall back to the group ACL at all — it answers **"was a participant"**,
because participation is a relationship that already exists in the graph. The
`PARTICIPANT_IN` edge is written once when the conversation happens; no ACL entry
has to be minted per participant per conversation, and none has to be revoked
when someone leaves the desk.

> **Running iam and client_platform in one database will mix the two identity
> graphs** — both create `User` and `AdGroup` nodes, and `entitlement_directory`
> will then list groups from both. For the scenarios above that is harmless, but
> use separate instances (topology C) if you want either bundle's directory
> output to look clean.

---

## What to look at

| Question | Command |
| --- | --- |
| Does the model hold? | `uv run python scripts/check_entitlements.py` |
| What does each persona see? | `try_tool.py client_opportunities principal=<email>` |
| Why can they see it? | `explain-access` snippet in topology A |
| Do derived grants match the lists? | the `differential: true` case in `entitlement_tests.yaml` |
| What does mediation cost? | `uv run python scripts/bench_mediation.py client_platform` |
| What does separating identity cost? | run the same suite under `client_platform` and `client_platform_split` — they should agree |
| What do native controls cost? | `uv run python scripts/bench_native_controls.py` (Enterprise/Business Critical) |

## The cast

| Person | Role | Reaches data by |
| --- | --- | --- |
| `lena.fischer@bank.com` | Coverage, EMEA asset managers | coverage team |
| `marc.dubois@bank.com` | Coverage, EMEA asset managers | coverage team (same as Lena) |
| `sofia.rossi@bank.com` | Coverage, EMEA hedge funds | coverage team |
| `evan.brooks@bank.com` | Coverage, NAMR corporates | coverage team |
| `nadia.haddad@bank.com` | Analytics product specialist | product role — **and authorship** on `INT-2006` |
| `tomas.silva@bank.com` | Execution product specialist | product role |
| `grace.okonjo@bank.com` | Platform administration | control role |
| `peter.lindqvist@bank.com` | Supervisory review | control role, sees everything |

Nadia is the interesting one: she reaches `INT-2006` **only** because she logged
it, which is a relationship rather than a list entry. That single record is the
clearest illustration of why the model supports both.
