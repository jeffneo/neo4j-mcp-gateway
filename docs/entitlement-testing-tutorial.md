# Tutorial: testing entitlements on Aura

Run the entitlement model against Aura and see the identity graph for yourself.
Uses the `client_platform` bundle: an institutional client platform where clients
consume research, analytics, execution and data products, and the commercial
question is which product to offer next.

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

| | Arrangement | Aura instances | Works today |
| --- | --- | --- | --- |
| **A** | one graph, identity and business data intermingled | 1 | ✅ |
| **B** | one graph, distinct subgraphs joined at defined seams | 1 | ✅ |
| **C** | two business domains, **each with its own copy of identity** | 2 | ✅ |
| **D** | identity in **one** database, the data it protects in **another** | 2 | ❌ |

A and B are the same physically and differ in modelling discipline; B is what you
would actually govern. C is the multi-bundle setup. D is the one to understand
before promising it to anyone.

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
one arrangement that does not work. The fix is not architectural purity; it is
**replication** (C) or one of the two options in the D section below.

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

## Topology D — identity in a different database from the data

**This does not work today, and it is worth understanding why.**

Mediation composes a *single* Cypher statement: resolve the caller, run the
query, filter the results. A single statement cannot traverse two databases, so
if the identity graph lives in instance 1 and the business data in instance 2,
the authorization prelude has nothing to resolve against.

You have three options:

1. **Replicate identity into each business database** — topology C. Simple,
   works now, and the identity file is designed for it.
2. **Resolve identity out of band**, then pass the resulting principals as a
   parameter to the business query. This needs a pluggable identity source in the
   engine (`resolve-identity` fetching from an external service or a second
   connection instead of traversing the local graph). Designed but not built —
   it is also the natural integration point for an external policy service that
   already knows a user's entitlements.
3. **Push the check into the database** with native controls, so the business
   instance enforces per-user rules without needing the identity graph locally.
   See §6 of [`entitlement-model-brief.md`](entitlement-model-brief.md).

If you try topology D as it stands, `identity.cypher` will load its people and
groups into the identity instance and silently create **no seams** — there are no
`Client` nodes there to attach to. Callers then resolve to their groups but match
no business data, and `check_entitlements.py` reports `must_see` failures rather
than anything dangerous. It fails closed, which is the right direction, but it
fails.

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
