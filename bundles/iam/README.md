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

This bundle owns **no security code**. It sets `security.mode: mediated` in
`bundle.yaml`, and the engine supplies the mechanism — any bundle can do the same.

**Engine-provided** (registered automatically when mediated):

- **`resolve-identity`** — resolve the caller and expand their entitlement groups
  into `authzPrincipals`. Call this first.
- **`secure-read-cypher`** — the open-ended path: run a model-generated MATCH
  fragment (no `RETURN`) inside the authorization wrapper. Set
  `security.expose_open_query_tool: false` to publish curated tools only.

**Curated YAML** (`tools/`), authored in the mediated `match:`/`scope:`/`return:`
form so the engine inserts the filter between match and return:

- **`client_activity`** — trades for a client. The **production-shaped path**: a
  human wrote the Cypher, so the inference-oracle risk of open-ended generation
  doesn't apply, and the same tool returns different rows per caller.
- **`entitlement_directory`** — identity/reference metadata (groups and members),
  no business records.

**Hidden:** raw `read-cypher` — automatically, because mediation is on (no
`downstream.hide` entry needed) — and `write-cypher` via
`downstream.read_only: true`, so the official server never registers it.

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

`scripts/try_tool.py` runs the curated tools, including as another persona:

```bash
ACTIVE_BUNDLE=iam uv run python scripts/try_tool.py client_activity \
  client="Acme Corp" principal=joe.hart@bank.com
```

The engine tools (`resolve-identity`, `secure-read-cypher`) run through the MCP
Inspector or a client.

## Connection

Env only — inherit the root `.env`, or drop a git-ignored `.env` here (see
[`.env.example`](.env.example)) to point IAM at its **own** Neo4j instance or
database. No credentials in `bundle.yaml`.

## How filtering decides (derived protection)

Every variable your fragment produces is filtered — the boundary does **not**
depend on the caller declaring a complete `protectedVariables` list:

| Variable | Outcome |
| --- | --- |
| `null` (OPTIONAL MATCH miss) | passes |
| carries `Permissions.Read` | must intersect the caller's principals |
| no `Permissions.Read` | passes as **reference data** (Client, AdGroup, Desk…) |
| listed in `protectedVariables` | **strict**: must carry an ACL *and* match |

`protectedVariables` is therefore optional and advisory — use it to force
fail-closed on a specific variable. Aggregates in `finalReturn` run *after*
filtering, so counts and sums are per-entitlement.

## Deployment postures

The bundle ships in **exploration** posture. To run the locked-down stance —
curated, human-authored tools only, no runtime query generation:

```bash
EXPOSE_OPEN_QUERY_TOOL=false ACTIVE_BUNDLE=iam uv run neo4j-mcp-gateway
```

or make it permanent in `bundle.yaml`:

```yaml
security:
  expose_open_query_tool: false
```

| | Exploration (default) | Curated only |
| --- | --- | --- |
| `resolve-identity` | ✅ | ✅ |
| `secure-read-cypher` (model-generated) | ✅ | **not registered** |
| `usecase_*` curated tools | ✅ entitlement-filtered | ✅ entitlement-filtered |
| `read-cypher` / `write-cypher` | hidden / unregistered | hidden / unregistered |
| Inference-channel risk | mitigated (blocklist) | **eliminated** — no runtime-generated Cypher |

The env variable can only **tighten**: a bundle that declares
`expose_open_query_tool: false` cannot be re-opened with
`EXPOSE_OPEN_QUERY_TOOL=true`. Config is the floor. The active posture is printed
in the gateway's startup log.

## Security notes

- **`get-schema` is intentionally left exposed.** Text-to-Cypher needs it to
  author valid fragments. It discloses *structure* — labels, relationship types,
  property keys, including the fact that `Permissions.Read` exists — but never
  row data. If a deployment treats the schema itself as sensitive, add
  `get-schema` to `downstream.hide` and supply the model a curated schema summary
  in `instructions` instead.
- The entitlement filter is applied by the gateway; **the gateway is the
  enforcement boundary**. Anyone who can reach the Neo4j instance directly (or run
  the official server themselves) is bounded only by Neo4j's own auth — treat DB
  credentials accordingly.
- **Fail-open on missing ACLs is real.** A record carrying a protected label but
  no `Permissions.Read` flows to everyone as reference data. `protected_labels`
  in `bundle.yaml` makes `scripts/validate_bundle.py` fail on exactly that, so it
  is caught in CI rather than in production. Run it after every data load.
- Defence in depth on the generated fragment: a blocklist rejects `RETURN`,
  writes, `CALL apoc|gds|dbms|db.` and `EXISTS/COUNT/COLLECT/CALL {…}` subquery
  expressions (an inference channel — they can test for data that never enters
  the filter), **and** execution happens in a Neo4j read transaction, so a write
  is refused by the server (`AccessMode`) even if a fragment slipped past.
- **Residual limitation:** inline pattern predicates in `WHERE` can still act as
  an existence oracle. Curated (YAML) tools avoid this class entirely because a
  human authored the query — prefer them in production, and treat the open-ended
  tool as an exploration path.
- `NEO4J_MCP_ALLOW_IMPERSONATION` is for demos. Leave it unset in production so a
  caller cannot choose their own principal.
