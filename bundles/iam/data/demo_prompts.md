# IAM entitlements demo — tier-1 investment bank

The pitch in one line: **the graph decides what each person is allowed to read,
and the model physically cannot see the rest.** Text-to-Cypher produces a match
fragment; the gateway wraps it in an authorization prelude and an entitlement
filter before Neo4j ever runs it. Raw `read-cypher` is not exposed, so there is
no bypass.

## Setup

```bash
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" -d "$NEO4J_DATABASE" \
  -f bundles/iam/data/iam_demo.cypher
export NEO4J_MCP_PRINCIPAL="joe.hart@bank.com"       # who the client says you are
export NEO4J_MCP_ALLOW_IMPERSONATION=true            # demo only: lets you switch seats
ACTIVE_BUNDLE=iam uv run neo4j-mcp-gateway
```

`NEO4J_MCP_ALLOW_IMPERSONATION` is what lets one operator demo every seat. In a
real deployment you drop it and identity comes from SSO (Basic/JWT) instead.

## The cast

| Principal | Role | Entitlement groups |
| --- | --- | --- |
| `anna.ross@bank.com` | Sales, Rates EMEA | sales-all, coverage-acme, desk-rates-emea |
| `joe.hart@bank.com` | Sales | sales-all, coverage-acme |
| `sam.diaz@bank.com` | Sales | sales-all, coverage-acme |
| `priya.natarajan@bank.com` | Sales | sales-all, coverage-zenith |
| `tom.becker@bank.com` | Trader | desk-rates-emea |
| `olu.adeyemi@bank.com` | Operations | ops-settlements |
| `maria.chen@bank.com` | Compliance | compliance-supervision |
| `david.okafor@bank.com` | IBD (private side) | control-room, deal-atlas |

**Verified entitlement matrix** (rows each principal can read):

| Principal | Comms | Requests | Trades | Deals | Research | Total |
| --- | --- | --- | --- | --- | --- | --- |
| anna.ross | 2 | 2 | 1 | 0 | 1 | **6** |
| joe.hart | 1 | 2 | 1 | 0 | 1 | **5** |
| sam.diaz | 1 | 2 | 1 | 0 | 1 | **5** |
| priya.natarajan | 1 | 1 | 1 | 0 | 1 | **4** |
| tom.becker | 0 | 2 | 1 | 0 | 1 | **4** |
| olu.adeyemi | 0 | 0 | 2 | 0 | 1 | **3** |
| maria.chen | 3 | 3 | 2 | 0 | 1 | **9** |
| david.okafor | 0 | 0 | 0 | 1 | 1 | **2** |

---

## Scenario 1 — private client communication

> **Say:** "Anna had a private chat with the client's treasurer. Joe and Sam
> cover the *same* client. Coverage does not entitle you to someone else's 1:1."

Ask as **Anna**, then as **Joe** — same question, same tool, different answer:

> "Show me the client communications for Acme Corp."

`secure-read-cypher`:
```json
{
  "query": "MATCH (x:Communication)-[:WITH_CLIENT]->(:Client {name: 'Acme Corp'})",
  "protectedVariables": ["x"],
  "finalReturn": "RETURN x.commId AS commId, x.channel AS channel, x.subject AS subject"
}
```

- **anna.ross** → `COMM-1001` (private chat) **and** `COMM-1002` (team thread)
- **joe.hart** / **sam.diaz** → `COMM-1002` only
- **maria.chen** (supervision) → both Acme comms; drop the client filter and she
  sees all three firm-wide, including Zenith's

🎤 Joe isn't told "access denied" — the row simply isn't in his result. Nothing
leaks, not even the existence of the chat.

## Scenario 2 — shared coverage sees the same book

> **Say:** "Joe and Sam both cover Acme. Entitlement is by coverage team, so
> their view of client demand is identical — no reconciliation, no shadow copies."

> "What requests has Acme Corp submitted, and what's the total notional?"

```json
{
  "query": "MATCH (:Client {name: 'Acme Corp'})-[:SUBMITTED]->(x:Request)",
  "protectedVariables": ["x"],
  "finalReturn": "RETURN count(x) AS requests, sum(x.notional) AS totalNotional"
}
```

- **joe.hart** and **sam.diaz** → identical: 2 requests, 95,000,000
- **priya.natarajan** (covers Zenith) → 0 requests

🎤 The aggregate is computed **after** filtering, so totals are per-entitlement —
critical when the answer is a number rather than a list.

## Scenario 3 — a booked trade and its need-to-know set

> **Say:** "Anna books a 5y swap for Acme on behalf of George Wu. Who should see
> it? The booker, the coverage team, the desk that owns the risk, settlements,
> and supervision — and nobody else."

> "Show me the trades booked for Acme Corp, with notional and desk."

```json
{
  "query": "MATCH (x:Trade)-[:FOR_CLIENT]->(:Client {name: 'Acme Corp'})",
  "protectedVariables": ["x"],
  "finalReturn": "RETURN x.tradeId AS tradeId, x.product AS product, x.notional AS notional"
}
```

Visible to **anna, joe, sam, tom (desk), olu (ops), maria (compliance)**;
invisible to **priya** (different coverage) and **david** (private side).

Follow-up that shows *why*, using the YAML tool in the same bundle:

> "Which entitlement groups grant access, and who's in them?" → `entitlement_directory`

🎤 One endpoint, two tool styles: `entitlement_directory` is a plain YAML tool
over identity metadata; `secure-read-cypher` is code-backed and enforces the
filter. Same gateway, same graph.

## Scenario 4 — information barrier

> **Say:** "Project Atlas is wall-crossed. Only the named deal team and the
> control room. Watch compliance — with broad supervisory rights over comms,
> requests and trades — still see nothing."

> "What deals are in progress involving Acme Corp?"

```json
{
  "query": "MATCH (x:Deal)-[:INVOLVES_CLIENT]->(:Client {name: 'Acme Corp'})",
  "protectedVariables": ["x"],
  "finalReturn": "RETURN x.dealId AS dealId, x.codename AS codename, x.stage AS stage"
}
```

- **david.okafor** → `DEAL-4001 / Project Atlas`
- **everyone else, including maria.chen** → nothing

🎤 Broad rights in one domain don't imply access in another. That's the
information barrier, enforced at query time rather than in application code.

## The closer — no bypass

> "Can you just run a Cypher query directly to check the full picture?"

The model will find that `read-cypher` isn't available — the bundle hides it, and
a direct call is rejected (`tool 'read-cypher' is disabled in this bundle`).
`write-cypher` isn't registered at all (`NEO4J_READ_ONLY=true` at the official
downstream). The only data path is `secure-read-cypher`, which always composes
the entitlement filter.

**Close:** *"Entitlements live in the graph, not in each application. One
endpoint serves every desk, and each person's assistant answers from exactly the
rows they're cleared to see — including the aggregates."*

## Autonomous variant

> "You are a markets assistant for a tier-1 bank with entitlement-aware graph
> access. Call resolve-identity first to establish who I am and my entitlement
> groups. Then brief me on client Acme Corp: our recent communications, their
> requests, and any trades booked. Use secure-read-cypher for all data. Note
> explicitly that results are filtered to my entitlements and may not be the
> firm-wide picture."

Run it as `joe.hart@bank.com`, then as `maria.chen@bank.com`, and diff the briefings.
