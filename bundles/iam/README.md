# IAM bundle — entitlement-aware graph access

A tier-1 investment-bank **entitlements** use case. SSO names the user, the graph
resolves their effective entitlement groups, and **`secure-read-cypher`** composes
an authorization wrapper around a generated Cypher fragment before Neo4j runs it.
Adapted from the Go IAM MCP fork to this gateway's Python pattern.

Run the demo: **[`data/demo_prompts.md`](data/demo_prompts.md)** (four scenarios,
exact tool calls, verified expected output).

## Model

```
(:User {email, name, role, desk, region, AdGroupList:[...]})-[:MEMBER_OF]->(:AdGroup)
(:User)-[:COVERS]->(:Client)
(:User)-[:PARTICIPANT_IN]->(:Communication)-[:WITH_CLIENT]->(:Client)
(:Client)-[:SUBMITTED]->(:Request)
(:User)-[:BOOKED]->(:Trade)-[:FOR_CLIENT]->(:Client)
(:Trade)-[:BOOKED_ON]->(:Desk)
(:Deal)-[:INVOLVES_CLIENT]->(:Client)          // wall-crossed, need-to-know
```

Business records carry a list-valued **`Permissions.Read`** property naming the
principals allowed to read that row. A caller's effective principals =
their identity + `everyone` + `AdGroupList` + groups via `MEMBER_OF` — typically
coverage teams (`coverage-acme`), desks (`desk-rates-emea`), `ops-settlements`,
`compliance-supervision`, `control-room`, and named deal teams (`deal-atlas`).

## Tools

**Code-backed** (`pytools/iam_tools.py`) — logic that can't be static Cypher:

- **`resolve-identity`** — resolve the caller and expand their entitlement groups
  into `authzPrincipals`. Call this first.
- **`secure-read-cypher`** — run a read-only MATCH fragment (no `RETURN`) inside
  the authorization wrapper. Pass `protectedVariables` (rows to authorize) and an
  optional `finalReturn` — the projection/aggregate runs **after** filtering.

**YAML** (`tools/`) — `entitlement_directory`, identity/reference metadata only
(groups and members; no permissioned business records). It shares the bundle and
the connection, which is the point: mixed tool styles behind one gateway.

**Hidden:** raw `read-cypher` (via `downstream.hide`) because it would bypass the
filter, and `write-cypher` (via `downstream.read_only: true`, so the official
server never registers it).

## Run it

```bash
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" -d "$NEO4J_DATABASE" \
  -f bundles/iam/data/iam_demo.cypher

export NEO4J_MCP_PRINCIPAL="joe.hart@bank.com"     # identity for STDIO clients
export NEO4J_MCP_ALLOW_IMPERSONATION=true          # demo only — lets a call pass `principal`
ACTIVE_BUNDLE=iam uv run neo4j-mcp-gateway         # or --bundle iam
```

> The official downstream server requires the **APOC** plugin (`apoc.meta`) for
> `get-schema`. Aura has it; a bare local Neo4j needs `NEO4J_PLUGINS='["apoc"]'`.

`scripts/try_tool.py` exercises **YAML** tools (`entitlement_directory`); the
code-backed tools run through the MCP Inspector or a client.

## Connection

Env only — inherit the root `.env`, or drop a git-ignored `.env` here (see
[`.env.example`](.env.example)) to point IAM at its **own** Neo4j instance or
database. No credentials in `bundle.yaml`.

## Security notes

- The entitlement filter is applied by `secure-read-cypher`; the gateway is the
  enforcement boundary. Anyone who can reach the Neo4j instance directly (or run
  the official server themselves) is bounded only by Neo4j's own auth — treat DB
  credentials accordingly.
- Defence in depth on the generated fragment: a blocklist rejects `RETURN`,
  writes and `CALL apoc|gds|dbms|db.`, **and** execution happens in a Neo4j read
  transaction, so a write is refused by the server (`AccessMode` error) even if a
  fragment slipped past the regex.
- `NEO4J_MCP_ALLOW_IMPERSONATION` is for demos. Leave it unset in production so a
  caller cannot choose their own principal.
