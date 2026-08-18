# Neo4j MCP Gateway

A single local **MCP gateway** for Neo4j. Run it once, connect from VS Code and
Claude Desktop, and get **two categories of tools behind one stdio endpoint**:

1. **Generic querying** — *proxied* from the official
   [neo4j/mcp](https://github.com/neo4j/mcp) server (schema introspection +
   read/write Cypher + GDS). These are **not reimplemented**: the gateway spawns
   the supported server as a downstream child and re-exposes its tools unchanged
   (`get-schema`, `read-cypher`, `write-cypher`, `list-gds-procedures`).
2. **Use-case tools** — parameterized, purpose-built tools defined as **YAML
   files**, shipped in swappable **[bundles](#bundles-swappable-use-cases)**
   (`bundles/<name>/tools/`). Adding one is: drop in a new `*.yaml` and restart.
   They run their own parameterized Cypher and are namespaced (`usecase_*`) so
   they never collide with the proxied tools.

The point: keep the official, supported server intact for generic work, while
making it trivial to add and iterate curated use-case tools.

```
        ┌──────────────────────── neo4j-mcp-gateway (this repo) ─────────────────────────┐
        │                                                                                 │
 VS Code│  ┌───────────────┐   mount    ┌──────────────────────────────┐  stdio (child)  │
 Claude ─┼─▶│ FastMCP server │◀──────────│ FastMCP proxy (create_proxy) │─────────────────┼─▶ official neo4j/mcp
 Desktop│  │  (stdio)       │            └──────────────────────────────┘                 │   (uvx / docker / binary)
 (stdio)│  │                │   add_tool ┌──────────────────────────────┐  bolt           │
        │  │                │◀──────────│ YAML tools (neo4j driver)     │─────────────────┼─▶ Neo4j
        │  └───────────────┘            └──────────────────────────────┘                 │
        └─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Bundles (swappable use cases)

The `gateway/` package is a **domain-agnostic engine**. Everything use-case-specific
lives in a **bundle** — a self-contained folder under `bundles/`:

```
bundles/<name>/
  bundle.yaml     # metadata only (name, description, model instructions,
                  #   REQUIRED security.mode, downstream.*, tool prefix) — NO secrets
  .env            # optional, git-ignored: this bundle's Neo4j connection override
  tools/*.yaml    # static parameterized use-case tools
  pytools/*.py    # optional code-backed tools (build_tools(ctx) -> [Tool]) for
                  #   logic that isn't static Cypher
  data/*.cypher   # demo dataset generator(s) + demo docs
```

Pick the active bundle with `ACTIVE_BUNDLE` (or `--bundle`). The engine points the
tools, data path, downstream connection, and the server's model-facing
`instructions` at that bundle.

**Connection is env-only** — URI / username / password / database are read from
the root `.env`, then a bundle's git-ignored `.env` **overrides** them. Nothing
connection-related lives in `bundle.yaml`, so a bundle can target an entirely
separate Neo4j instance (e.g. a different Aura) with no secrets in committed files.

```bash
uv run neo4j-mcp-gateway --list-bundles          # ato, iam, …
ACTIVE_BUNDLE=iam uv run neo4j-mcp-gateway         # serve a specific bundle
uv run python scripts/new_bundle.py <name>         # scaffold a new bundle
ACTIVE_BUNDLE=<name> uv run python scripts/validate_bundle.py   # run all its tools
```

### Serving several bundles at once

`ACTIVE_BUNDLE` accepts a comma list. Each bundle keeps its **own connection**, so
they may sit on different databases or entirely different Neo4j instances:

```bash
ACTIVE_BUNDLE=ato,iam uv run neo4j-mcp-gateway     # or --bundle ato,iam
```

Tools are then namespaced per bundle (`ato_mule_hubs`, `iam_client_activity`,
`iam_secure-read-cypher`), and each bundle keeps its own security posture — in the
example above `ato_read-cypher` stays available while `iam_read-cypher` is hidden.
Bundles sharing a datasource share one downstream official server rather than
spawning a redundant child. A single bundle behaves exactly as before (no bundle
prefix), so nothing changes unless you opt in.

> **Safety rule:** the gateway **refuses to start** if an `open` bundle and a
> `mediated` bundle resolve to the same database. The open bundle's unfiltered
> tools would read the very rows the mediated bundle protects, and no amount of
> tool-hiding fixes that. Give them separate databases, make both mediated, or run
> separate gateway processes.

Two costs worth knowing: every bundle's `instructions` are concatenated (which
dilutes tool selection — keep it to a handful of bundles), and each distinct
datasource spawns its own downstream child.

**Swap without editing files** — register one client entry per bundle, each pinned
via `env`:

```json
"mcpServers": {
  "neo4j-ato": { "command": "uv", "args": ["run","--directory","/ABS/PATH","neo4j-mcp-gateway"], "env": {"ACTIVE_BUNDLE":"ato"} },
  "neo4j-iam": { "command": "uv", "args": ["run","--directory","/ABS/PATH","neo4j-mcp-gateway"], "env": {"ACTIVE_BUNDLE":"iam"} }
}
```

### Access mode is a required declaration

Every `bundle.yaml` **must** state `security.mode` — there is no default, so
"unfiltered" is a recorded decision rather than something that happens by omission:

```yaml
security:
  mode: open        # tools read directly (all consumers uniformly entitled)
  # mode: mediated  # every read is entitlement-filtered against the caller
```

Under `mediated`, the engine:

1. registers **`resolve-identity`** and (optionally) **`secure-read-cypher`** —
   entitlement mediation is an engine capability, so no bundle ships security code;
2. wraps **every curated YAML tool** in the authorization prelude + entitlement
   filter, so the same tool returns different rows per caller;
3. **auto-hides raw `read-cypher`**, which would bypass the filter, and defaults
   the downstream to read-only;
4. requires tools to use the mediated authoring form (below) and to be read-only.

Mediated tools declare the split explicitly rather than having the engine parse
Cypher to find the `RETURN` — getting that wrong would be a security bug:

```yaml
match: |                # no RETURN
  MATCH (t:Trade)-[:FOR_CLIENT]->(c:Client {name: $client})
scope: [t, c]           # variables carried into the return; ALL are filtered
protect: [t]            # optional: strict — must carry an ACL or the row is dropped
return: |               # runs AFTER filtering, so aggregates are per-entitlement
  RETURN t.tradeId AS tradeId, t.notional AS notional
```

**Postures.** A mediated bundle can publish curated tools *only* by setting
`security.expose_open_query_tool: false` (or `EXPOSE_OPEN_QUERY_TOOL=false` at
runtime). The open-ended `secure-read-cypher` is then never registered, so no
Cypher is generated at runtime — the stance for regulated workflows. The env
variable can only tighten: a bundle declaring curated-only cannot be re-opened
from the shell.

Declare `protected_labels` so [`scripts/validate_bundle.py`](scripts/validate_bundle.py)
fails when a business record is missing its access-control list — otherwise such a
record silently flows to everyone. The validator also persona-diffs mediated tools
to prove the filter actually discriminates between callers.

New to this? [`docs/entitlement-model-brief.md`](docs/entitlement-model-brief.md)
explains the model conceptually in about three minutes. Full reference and known
limits: [`docs/mediation-spec.md`](docs/mediation-spec.md). What each entitlement
model costs the data pipeline — what must be ingested, by whom, and what breaks
when it is late: [`docs/data-ingestion.md`](docs/data-ingestion.md).

> **Note:** `get-schema` stays exposed even under `mediated`, because
> text-to-Cypher needs it. It reveals structure (labels, relationship types,
> property keys) but no row data. Add it to `downstream.hide` if your deployment
> treats the schema itself as sensitive.

### Audit logging

Set a path and every tool call appends one JSON object — who called, as whom,
which tool, in which bundle, the outcome, and **how many rows survived the
filter**:

```bash
NEO4J_MCP_AUDIT_LOG=/var/log/neo4j-mcp/audit.jsonl
NEO4J_MCP_AUDIT_ARGUMENTS=true    # optional: also record argument VALUES
```

```json
{"ts":"2026-08-18T19:18:36.407+00:00","event":"tool_call","tool":"client_opportunities",
 "bundle":"client_platform","mode":"mediated","identitySource":"graph","grantModel":"both",
 "principal":"evan.brooks@bank.com","principalSource":"impersonation-request",
 "impersonated":true,"argumentNames":["client"],"durationMs":18.1,"outcome":"ok","rows":1}
```

**Row contents are never logged.** An audit log that copies the rows it audits is
a second, less-protected replica of the data the filter exists to restrict —
usually on a filesystem with weaker controls, often shipped to an aggregator a
different team can read. The record carries the row *count* and nothing about the
rows. Argument values are the judgement call and are off by default; argument
*names* are always recorded, so you can see which question was asked without its
subject.

`impersonated` is top-level rather than something to infer: running as another
principal is a privileged action and is the first thing a reviewer looks for.
Proxied tools are covered too, so an `open` bundle's raw `read-cypher` is audited
on the same terms — and a call to a hidden tool is recorded as a rejection.

A bundle can declare `security.require_audit: true`, and the gateway then
**refuses to start** without a log path — the same fail-closed stance as
`security.mode`.

### Where identity lives

By default the identity graph sits beside the data and the prelude traverses it
in the same statement. `security.identity.source` moves it:

```yaml
security:
  identity:
    source: graph        # default — identity beside the data, one statement
    # source: composite  # identity and data in separate databases, joined by a
    #   identity_graph: fed.identity        #   composite database. Still ONE
    #   data_graph: fed.data                #   statement and one transaction.
    # source: remote     # identity resolved over a SECOND connection, from
    #   remote_env_prefix: IDENTITY         #   IDENTITY_NEO4J_URI etc. in .env
```

`composite` and `remote` make the identity store independent — its own instance,
credentials and lifecycle, shareable across domains. Neither gives the data query
a caller *node*, so both forbid anchoring and tools that reference `caller`.

**Path grants survive both**, because a grant does not need the caller *node* —
it needs a value derived from the caller. A relationship cannot span two
databases, but a *traversal* can be cut at a node present in both: Neo4j's
documented **proxy node** pattern. The engine finds that cut itself and re-roots
the data-side half at the proxy, so patterns are authored once and mean the same
thing co-located or split. A grant that cannot be cut safely — an identity
relationship appearing after the boundary, which would deny silently — is
rejected at load. See GRANT_SPLITTING in
[`gateway/mediation.py`](gateway/mediation.py).

| | `composite` | `remote` |
| --- | --- | --- |
| Path grants (`grant_model: path` / `both`) | ✅ | ✅ |
| Anchoring | ✅ | ✅ |
| Round trips | 1 (one statement, one transaction) | 2, with a consistency window |
| Needs a composite database | yes | no — any two connections |
| Proxy nodes in the data database | required | required |

Anchors split by the same rule, so the performance lever survives too — measured
at ~17x either side of the split on 100,000 rows
([`scripts/bench_separation.py`](scripts/bench_separation.py)). What both give up
is tools that reference `caller` in their match; express that scoping with an
anchor or a parameter instead.

**Cutting at a property instead of a proxy node** removes the proxies entirely.
Where the boundary is already recorded as a property — the covering team on the
Client, the author on the Interaction — declare it and the grant compares that
property instead of traversing to a proxy:

```yaml
identity:
  boundary_properties:
    Client: coverageTeam
    Interaction: loggedByEmail
```

The `client_platform_split` bundle runs this way against a database holding **no
`AdGroup` or `User` nodes at all**, with results identical to co-located across
24 comparisons. Each grant also loses a hop, and a boundary property on the row
itself collapses to a bare comparison with no subquery.

> The property and the relationship are two recordings of one fact and can drift
> apart. Nothing detects that from the pattern alone, so keep a `differential:`
> conformance case proving they agree on real data.

### Downstream identity: making native rules apply to the end user

Native database rules (RBAC, property rules, ABAC-assigned roles) are evaluated
against the account that **connects**. A gateway holding one service connection
gets them evaluated against the service account, so no per-user rule applies at
all. Two ways to close that, per session:

```bash
NEO4J_MCP_ACCESS_TOKEN=<jwt>       # the caller's token authenticates the session
NEO4J_MCP_DB_IMPERSONATION=true    # service account impersonates the principal
```

With a token, **the database validates it** — signature, issuer, audience,
expiry — and maps its claims to roles. The gateway never inspects it, which is
the point: token validation belongs to something built for it. Setting both is
refused, since a token already asserts who the caller is.

These compose with mediation rather than replacing it. Measured with a real PBAC
rule (`FOR (o:Opportunity) WHERE o.stage = 'Proposal'`) on a real native role:

| | Rows |
| --- | --- |
| service account — mediation only (her coverage) | 2 |
| impersonated — mediation ∩ PBAC | **1** |

The caller sees the **intersection**, which is what a layered model should do.

> One deployment note found the hard way: with identity co-located, the
> impersonated user also needs read access to the identity graph, or the
> authorization prelude resolves nothing and every query returns zero rows. It
> fails closed, but it looks like an entitlement bug. `identity.source: remote`
> avoids it — identity resolution uses its own connection and only the data query
> is impersonated.

Adding another source (an external entitlement service, LDAP, token
introspection) means implementing `IdentitySource` and registering it — see
[`gateway/identity_sources.py`](gateway/identity_sources.py).

Want to try the entitlement model on Aura?
[`docs/entitlement-testing-tutorial.md`](docs/entitlement-testing-tutorial.md)
walks through it across six identity/data topologies.

Shipped bundles: **`ato`** (account-takeover; 7 YAML tools; `mode: open`),
**`client_platform`** (institutional client platform, up/cross-sell;
`mode: mediated`), **`client_platform_split`** (the same platform with identity
in a separate database — see
[`security.identity.source`](#where-identity-lives)) and
**`iam`** (investment-bank entitlements; `mode: mediated`, curated tools filtered
per caller, raw `read-cypher` auto-hidden). Neither bundle contains security
code — a bundle declares a policy and the engine enforces it.

---

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** (`brew install uv` / `pipx install uv`)
- A reachable **Neo4j** instance (local, Docker, or Aura) with credentials
- The official downstream server is fetched automatically on first run via
  `uvx neo4j-mcp-server` — no manual install. (Docker / a built Go binary also
  work; see [`.env.example`](.env.example).)

> **Note:** the official server *verifies Neo4j connectivity at startup and
> exits if it cannot connect.* If your credentials are wrong or Neo4j is
> unreachable, the proxied `get-schema` / `*-cypher` tools will not appear —
> check the gateway's stderr log. The YAML use-case tools still load regardless
> and report connection problems as clean per-call errors.

---

## Setup

```bash
# from the project root
cp .env.example .env
# edit .env with your Neo4j URI / user / password / database
uv sync
```

`.env` (git-ignored) holds the real credentials. The **same** credentials flow
to both the downstream official server and the YAML tool executor.

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j bolt URI (shared) |
| `NEO4J_USERNAME` | `neo4j` | Neo4j user (shared) |
| `NEO4J_PASSWORD` | `password` | Neo4j password (shared) |
| `NEO4J_DATABASE` | `neo4j` | Target database (shared) |
| `NEO4J_MCP_CMD` | `uvx neo4j-mcp-server` | How to launch the official downstream server |
| `NEO4J_READ_ONLY` | *(unset)* | `true` disables downstream `write-cypher` |
| `NEO4J_TELEMETRY` | `false` | Downstream telemetry opt-in |
| `ACTIVE_BUNDLE` | `ato` | Bundle(s) to serve — comma list for several at once |
| `EXPOSE_OPEN_QUERY_TOOL` | *(bundle)* | Set `false` to drop `secure-read-cypher` (tighten only) |
| `USECASE_PREFIX` | `usecase_` | Tool-name prefix (also settable in `bundle.yaml`) |

Tools and data come from the active bundle (`bundles/<ACTIVE_BUNDLE>/`); the
database and model instructions can be declared in its `bundle.yaml`.

---

## Run

```bash
uv run neo4j-mcp-gateway
# equivalent:
uv run python -m gateway.server
```

The gateway serves over **stdio** — that's what editors launch. On startup it
logs (to **stderr**) the downstream command, the mounted official tools, and the
YAML use-case tools it registered.

### Verify with the MCP Inspector

```bash
# List the union of tools (official proxied + YAML use-case)
npx @modelcontextprotocol/inspector --cli uv run neo4j-mcp-gateway --method tools/list

# Call a generic proxied tool
npx @modelcontextprotocol/inspector --cli uv run neo4j-mcp-gateway \
  --method tools/call --tool-name get-schema

# Call a YAML use-case tool
npx @modelcontextprotocol/inspector --cli uv run neo4j-mcp-gateway \
  --method tools/call --tool-name usecase_ato_session_triage --tool-arg min_risk=5
```

Or launch the Inspector UI (drop `--cli`) and browse/click the tools.

---

## Adding a use-case tool (the whole point)

1. Create `bundles/<active-bundle>/tools/my_tool.yaml` (e.g. `bundles/ato/tools/my_tool.yaml`):

   ```yaml
   name: recent_transactions_for_customer
   description: Recent transactions performed by a customer's accounts.
   parameters:
     - name: customer_id
       type: string
       description: Customer.customerId
       required: true
     - name: limit
       type: integer
       description: Max rows to return
       required: false
       default: 25
   cypher: |
     MATCH (c:Customer {customerId: $customer_id})-[:HAS_ACCOUNT]->(:Account)
           -[:PERFORMS]->(t:Transaction)
     RETURN t.transactionId AS id, t.amount AS amount, t.date AS date
     ORDER BY t.date DESC
     LIMIT $limit
   read_only: true   # set false to run in write mode
   ```

2. Restart the gateway (see [Restarting](#restarting-to-pick-up-new-tools)). It
   appears as `usecase_recent_transactions_for_customer`.

> Tools are discovered once at startup and MCP clients cache the tool list, so a
> new/edited YAML file needs a restart to show up — saving alone is not enough.

**Schema reference**

| Field | Required | Notes |
| --- | --- | --- |
| `name` | ✅ | Alphanumeric/underscore. Final tool name is `<USECASE_PREFIX><name>`. |
| `description` | ✅ | Shown to the model. |
| `parameters` | — | List of `{name, type, description, required, default}`. |
| `parameters[].type` | — | `string` · `integer` · `number` · `boolean` · `array` · `object` (default `string`). |
| `cypher` | ✅ | Parameters bind to `$name` placeholders. |
| `read_only` | — | `true` (default) → read transaction; `false` → write transaction. |

Malformed files fail loudly at startup with a message naming the file. Results
are returned as JSON: `{ "count": N, "records": [ ... ] }`, with Neo4j temporal /
spatial / graph values converted to JSON-friendly forms.

**Fast dev loop** — test a tool without MCP or a gateway restart:

```bash
uv run python scripts/try_tool.py --list
uv run python scripts/try_tool.py mule_hubs min_victims=2
```

`scripts/try_tool.py` runs one tool's Cypher through the same loader/executor the
gateway uses, straight against Neo4j — so you get instant feedback while writing
YAML. Use the MCP Inspector to check the tool over MCP, and a client (Claude
Desktop) for the final integration.

---

## Restarting to pick up new tools

Adding or editing a YAML tool requires a restart. The cleanest way depends on how
the gateway is running:

- **In VS Code / Claude Desktop (normal use):** don't kill it in a terminal — let
  the client restart it, which stops the process by closing its stdin (a clean,
  instant shutdown).
  - **VS Code:** open `.vscode/mcp.json` and click **Restart** on the server, or
    run *MCP: List Servers → neo4j-gateway → Restart* from the command palette.
  - **Claude Desktop:** toggle the connector off/on (or quit and reopen Claude).
- **Running it yourself in a terminal (e.g. testing with the Inspector):** a
  single **Ctrl+C** stops it immediately — the gateway installs a fast
  SIGINT/SIGTERM handler that exits at once and lets the downstream child close
  via stdin-EOF, rather than blocking on an async teardown. `kill <pid>`
  (SIGTERM) works the same way.

If you ever see leftover `neo4j-mcp-server` processes from an earlier session:

```bash
pgrep -fl 'neo4j-mcp-server|neo4j-mcp-gateway'   # inspect first
pkill -f 'neo4j-mcp-server'                       # then clean up stale ones
```

> Heads-up: `pkill` will also stop the instance your editor is actively using, so
> restart that connector afterwards.

## Client configuration

Both clients launch the gateway over stdio. Credentials are read from this repo's
`.env` (no secrets in the client config).

### VS Code — `.vscode/mcp.json` (portable, already in this repo)

```json
{
  "servers": {
    "neo4j-gateway": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "${workspaceFolder}", "neo4j-mcp-gateway"]
    }
  }
}
```

Nothing is machine-specific here: `${workspaceFolder}` resolves automatically.
For the CodeLens **Start/Restart** buttons (and for `${workspaceFolder}`) to work,
**open this repo folder as the workspace root** (`File → Open Folder → the
`neo4j-mcp-gateway` folder`), not a parent directory — VS Code only reads
`.vscode/mcp.json` from the opened folder's root.

- **Start/stop it:** click **Start** on the CodeLens above `"neo4j-gateway"`, or
  Command Palette → **MCP: List Servers → neo4j-gateway → Start**.
- **Use it:** in Copilot Chat switch to **Agent** mode, open the 🛠️ tools picker,
  and enable the `neo4j-gateway` tools.
- **If VS Code can't find `uv`:** it was launched without your shell `PATH`.
  Either start VS Code from a terminal (`cd neo4j-mcp-gateway && code .`), install
  `uv` to a system-wide location, or replace `"uv"` with the absolute path from
  `which uv`.

### Claude Desktop — `claude_desktop_config.json`

macOS: `~/Library/Application Support/Claude/claude_desktop_config.json` ·
Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Claude Desktop has no `${workspaceFolder}` and does **not** inherit your shell
`PATH`, so both paths must be absolute. Fill in your own with
`which uv` (the `uv` path) and `pwd` (this repo's path):

```json
{
  "mcpServers": {
    "neo4j-gateway": {
      "command": "/ABSOLUTE/PATH/TO/uv",
      "args": ["run", "--directory", "/ABSOLUTE/PATH/TO/neo4j-mcp-gateway", "neo4j-mcp-gateway"]
    }
  }
}
```

> Tip: you can add an `"env": { "NEO4J_URI": "…", "NEO4J_PASSWORD": "…" }` block
> here instead of using `.env` if you prefer per-client credentials.

### Sharing this repo

The repo is self-contained — a new user only needs, per machine:

```bash
git clone <repo-url> neo4j-mcp-gateway
cd neo4j-mcp-gateway
cp .env.example .env          # fill in their Neo4j URI / user / password / database
uv sync                       # creates the venv; uvx fetches the downstream on first run
code .                        # open THIS folder in VS Code, then MCP: List Servers → Start
```

Prerequisites: **Python 3.11+**, **[uv](https://docs.astral.sh/uv/)**,
and (for the Inspector smoke test) **Node/npx**. No absolute paths to edit for the
VS Code flow; only the Claude Desktop config needs their own two paths.

## Demo data (account-takeover)

`bundles/ato/data/ato_demo.cypher` seeds a small, self-contained **ATO** dataset —
realistic legitimate baseline, two fraud patterns (classic takeover + mule ring),
and a false-positive traveler for precision discussion. Load it with:

```bash
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" -d "$NEO4J_DATABASE" -f bundles/ato/data/ato_demo.cypher
```

It's idempotent and namespaced (`source:'ato-demo'`), so it won't disturb other
data. See [`bundles/ato/data/README.md`](bundles/ato/data/README.md) for the roster,
the ground-truth scoring fields, and copy-paste detection queries.

**ATO demo:** [`bundles/ato/DEMO.md`](bundles/ato/DEMO.md) — quickest path to a
working demo: load, verify, serve, and the results to expect.

**Demo docs:**
- [`bundles/ato/data/README.md`](bundles/ato/data/README.md) — the **presenter
  runbook** (drives the tools explicitly; good with the MCP Inspector).
- [`bundles/ato/data/demo_prompts.md`](bundles/ato/data/demo_prompts.md) —
  **conversational prompts** to paste into Claude Desktop so the model orchestrates
  the tools itself — the assistant picks the tools and narrates the investigation.

---

## Project layout

```
neo4j-mcp-gateway/
  gateway/            # ENGINE (domain-agnostic; never changes per use case)
    server.py         # entrypoint: build proxy + load bundle tools + serve stdio
    proxy.py          # spawn & re-expose the official neo4j/mcp downstream
    yaml_tools.py     # YAML discovery, validation, MCP registration, Cypher execution
    mediation.py      # entitlement mediation: prelude + filter composition
    security_tools.py # resolve-identity / secure-read-cypher for mediated bundles
    pytools.py        # load code-backed bundle tools (build_tools(ctx))
    middleware.py     # HideToolsMiddleware (hide proxied tools, e.g. read-cypher)
    bundles.py        # bundle manifest parsing + discovery
    config.py         # env + active-bundle resolution
  scripts/
    try_tool.py       # fast dev loop: run one tool, no MCP/restart
    new_bundle.py     # scaffold a new bundle from bundles/_template
    validate_bundle.py# run every tool in a bundle against a live DB
  bundles/            # SWAPPABLE use cases (pick one with ACTIVE_BUNDLE)
    _template/        # skeleton copied by new_bundle.py
    ato/              # account-takeover bundle
      bundle.yaml     #   metadata + non-secret config
      tools/*.yaml    #   the 7 ATO tools
      data/           #   ato_demo.cypher + demo docs
    iam/              # investment-bank entitlements bundle
      bundle.yaml     #   security.mode: mediated + protected_labels
      tools/*.yaml    #   curated mediated tools (match/scope/return)
      data/iam_demo.cypher
      data/iam_demo.cypher
  .vscode/mcp.json
  .env.example        # root creds/defaults (per-bundle .env overrides)
  pyproject.toml
  README.md
```

## Design notes / extending

- **Namespacing** — official tools keep their original names; YAML tools are
  prefixed (`usecase_`), so names can never collide.
- **Lazy driver** — the YAML executor connects to Neo4j on first tool call, so
  the gateway starts and lists tools even if Neo4j is briefly down; connection
  errors surface as clean tool errors.
- **Retrieval-ready** — the YAML registry (`load_tool_specs` in
  [`yaml_tools.py`](gateway/yaml_tools.py)) is cleanly separated from execution,
  so a future vector-index / kNN routing layer could sit in front of it without
  touching the executor. (Not implemented — out of scope for now.)
- **Extending routing** — to add non-YAML tools, register them on the `gateway`
  server in [`server.py`](gateway/server.py) with `gateway.add_tool(...)`.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Only `usecase_*` tools appear | Downstream couldn't reach Neo4j and exited. Fix `NEO4J_URI`/creds; check gateway stderr. |
| `uvx` slow on first run | It downloads the official server wheel once, then caches it. |
| Claude Desktop can't start it | Use the **absolute** path to `uv` in `command`. |
| YAML tool returns an error | The message includes the Neo4j error code — verify the Cypher and params. |
