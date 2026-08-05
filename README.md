# Neo4j MCP Gateway

A single local **MCP gateway** for Neo4j. Run it once, connect from VS Code and
Claude Desktop, and get **two categories of tools behind one stdio endpoint**:

1. **Generic querying** — *proxied* from the official
   [neo4j/mcp](https://github.com/neo4j/mcp) server (schema introspection +
   read/write Cypher + GDS). These are **not reimplemented**: the gateway spawns
   the supported server as a downstream child and re-exposes its tools unchanged
   (`get-schema`, `read-cypher`, `write-cypher`, `list-gds-procedures`).
2. **Use-case tools** — parameterized, purpose-built tools defined as **YAML
   files** in [`tools/`](tools/). Adding one is: drop in a new `*.yaml` and
   restart. They run their own parameterized Cypher and are namespaced
   (`usecase_*`) so they never collide with the proxied tools.

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
| `TOOLS_DIR` | `tools` | Where YAML use-case tools are discovered |
| `USECASE_PREFIX` | `usecase_` | Namespace prefix for YAML tool names |

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
  --method tools/call --tool-name usecase_search_movies_by_actor --tool-arg actor="Keanu Reeves"
```

Or launch the Inspector UI (drop `--cli`) and browse/click the tools.

---

## Adding a use-case tool (the whole point)

1. Create `tools/my_tool.yaml`:

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
  single **Ctrl+C** now stops it immediately. (Earlier it took several Ctrl+C
  because the shutdown waited on the downstream child; the gateway now installs
  a fast SIGINT/SIGTERM handler that exits at once and lets the child close via
  stdin-EOF.) `kill <pid>` (SIGTERM) also works instantly.

If you ever see leftover `neo4j-mcp-server` processes from older sessions:

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

### Sharing this as a lab demo

The repo is self-contained — an attendee only needs, per machine:

```bash
git clone <repo-url> neo4j-mcp-gateway
cd neo4j-mcp-gateway
cp .env.example .env          # fill in their Neo4j URI / user / password / database
uv sync                       # creates the venv; uvx fetches the downstream on first run
code .                        # open THIS folder in VS Code, then MCP: List Servers → Start
```

Prerequisites they need installed: **Python 3.11+**, **[uv](https://docs.astral.sh/uv/)**,
and (for the Inspector smoke test) **Node/npx**. No absolute paths to edit for the
VS Code flow; only the Claude Desktop config needs their own two paths.

## Demo data (account-takeover)

`data/ato_demo.cypher` seeds a small, self-contained **ATO** dataset — realistic
legitimate baseline, two fraud patterns (classic takeover + mule ring), and a
false-positive traveler for precision discussion. Load it with:

```bash
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" -d "$NEO4J_DATABASE" -f data/ato_demo.cypher
```

It's idempotent and namespaced (`source:'ato-demo'`), so it won't disturb other
data. See [`data/README.md`](data/README.md) for the roster, the ground-truth
scoring fields, and copy-paste detection queries.

**Running the lab:**
- [`data/README.md`](data/README.md) — the **presenter runbook** (drives the
  tools explicitly; good with the MCP Inspector).
- [`data/demo_prompts.md`](data/demo_prompts.md) — **conversational prompts** to
  paste into Claude Desktop so the model orchestrates the tools itself. This is
  the intended payoff of the lab.

---

## Project layout

```
neo4j-mcp-gateway/
  gateway/
    server.py       # entrypoint: build proxy + load YAML tools + serve stdio
    proxy.py        # spawn & re-expose the official neo4j/mcp downstream
    yaml_tools.py   # YAML discovery, validation, MCP registration, Cypher execution
    config.py       # env-based config (.env)
  tools/
    example_movies.yaml         # canonical documented example
    high_risk_transactions.yaml # fraud-graph example (optional param)
    ato_session_triage.yaml     # ATO: risk-score every login session
    new_device_logins.yaml      # ATO: logins from untrusted devices
    mule_hubs.yaml              # ATO: shared high-risk beneficiaries (rings)
  data/
    ato_demo.cypher             # account-takeover demo dataset generator
    README.md                   # load steps + detection queries
  .vscode/mcp.json
  .env.example
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
