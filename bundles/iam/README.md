# IAM bundle — query-mediated access control

An IAM-aware bundle: SSO/identity names the user, the graph resolves their
effective groups, and **`secure-read-cypher`** composes an authorization wrapper
around a generated Cypher fragment before Neo4j runs it. Adapted from the Go
IAM MCP fork to this gateway's Python pattern.

## Model

```
(:User {email, name, AdGroupList:[...]})-[:MEMBER_OF]->(:AdGroup {name})
(:Service)-[:EXECUTES]->(:Job { `Permissions.Read`:[...], `Permissions.Create`:[...], ... })
```

Permissions are **list-valued properties on domain nodes**. A user's effective
principals = their identity + `everyone` + `AdGroupList` + groups via `MEMBER_OF`.
A row is readable only if one of its `Permissions.Read` entries is in that set.

## Tools (code-backed, in `pytools/iam_tools.py`)

- **`resolve-identity`** — resolve the current principal and expand its IAM groups
  into `authzPrincipals`. Call this first.
- **`secure-read-cypher`** — run a read-only MATCH fragment (no `RETURN`) inside the
  IAM wrapper; pass `protectedVariables` (e.g. `["j"]`) and an optional `finalReturn`
  for the projection/aggregate (which runs *after* filtering).
- Raw **`read-cypher`** is **hidden** (`bundle.yaml` → `downstream.hide`) because it
  would bypass the filter; `write-cypher` is hidden via `downstream.read_only: true`.

## Run it

```bash
# 1. load demo data (Aura or any APOC-enabled Neo4j; the official downstream needs APOC)
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" -d "$NEO4J_DATABASE" \
  -f bundles/iam/data/iam_demo.cypher

# 2. identity comes from env (STDIO clients); enable test impersonation to switch users
export NEO4J_MCP_PRINCIPAL="michael.moore@neo4j.com"
export NEO4J_MCP_ALLOW_IMPERSONATION=true

# 3. try the tools without MCP
ACTIVE_BUNDLE=iam uv run python scripts/try_tool.py --list      # (pytools aren't listed here; see note)
ACTIVE_BUNDLE=iam uv run neo4j-mcp-gateway --list-bundles

# 4. serve it
ACTIVE_BUNDLE=iam uv run neo4j-mcp-gateway     # or --bundle iam
```

> `scripts/try_tool.py` runs **YAML** tools; the IAM tools are code-backed, so
> exercise them through the MCP Inspector or a client (or unit-test the handlers).

## Connection

Env only. Inherit the root `.env`, or drop a git-ignored `.env` here (see
`.env.example`) to point IAM at its own Neo4j instance — no credentials in YAML.

## Demo principals (seeded)

| Principal | Effective groups | Readable jobs |
| --- | --- | --- |
| `johnny.kinnaird@neo4j.com` | everyone, group1, group2 | 4 |
| `michael.moore@neo4j.com` | everyone, group2 | 3 |
| `sarah.lee@neo4j.com` | everyone | 1 |

`admin-secrets` (`Permissions.Read=['admins']`) is readable by none of them.

## Example prompt shape (Claude)

> "Use resolve-identity first. Then for job access, call secure-read-cypher with
> `query: MATCH (a)-[:EXECUTES]->(j:Job)`, `protectedVariables: ["j"]`, and
> `finalReturn: RETURN count(DISTINCT j) AS readableExecutedJobCount`."
