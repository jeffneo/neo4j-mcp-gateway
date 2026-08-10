# Lab: build account-takeover tools on a Neo4j MCP gateway

In this lab you'll turn a graph of banking activity into a set of **MCP tools** an
AI assistant can call to hunt account-takeover (ATO) fraud. You will **not** write
any of the gateway plumbing — that's done for you. You write **YAML tool files**:
a bit of Cypher plus the parameters that become the tool's inputs.

By the end, an analyst will be able to *ask Claude* "which logins look like
takeovers, and is it a ring?" and Claude will call the tools you built.

## What's given vs. what you build

| Given (don't edit) | You build |
| --- | --- |
| `gateway/` — the MCP server (proxy + YAML loader) | `tools/*.yaml` — the use-case tools |
| `data/ato_demo.cypher` — the dataset | |
| `solutions/*.yaml` — reference answers (peek if stuck) | |

## 1. Setup

```bash
cp .env.example .env         # fill in your Neo4j URI / user / password / database
uv sync                      # install deps
# load the demo dataset (wipe first if this is a scratch instance):
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" -d "$NEO4J_DATABASE" -f data/ato_demo.cypher
```

Sanity check — this should print a JSON result, not an error:

```bash
uv run python scripts/try_tool.py customer_sessions customer_id=CUST-1004
```

## 2. Two-minute code tour (read only)

- **`gateway/config.py`** — find `NEO4J_MCP_CMD` (`uvx neo4j-mcp-server`). The gateway
  *proxies the official Neo4j MCP server* for generic Cypher and adds your curated
  tools beside it. You're not reinventing query execution — you're curating it.
- **`gateway/yaml_tools.py`** — `ToolSpec.input_schema()` turns your `parameters:`
  into an MCP **inputSchema**, and `_make_handler()` binds them to Cypher `$params`.
  That mapping is the entire idea of a use-case tool.

## 3. The dev loop (important)

Do **not** restart the gateway on every edit. Iterate like this:

```bash
uv run python scripts/try_tool.py <tool_name> [param=value ...]   # instant, no MCP
uv run python scripts/try_tool.py --list                          # see tools + params
```

It runs your tool's Cypher through the real loader/executor against Neo4j and
prints JSON. Only once a tool works do you (optionally) restart the gateway and
call it from the MCP Inspector or Claude Desktop.

## 4. Your first tool (the template)

Open [`tools/customer_sessions.yaml`](tools/customer_sessions.yaml). It's fully
commented — this is the shape every tool follows (`name`, `description`,
`parameters`, `cypher`, `read_only`). Run it, then read the Cypher and match each
`$param` to the `parameters:` list above it.

## 5. Exercises

Each stub is already in `tools/` with the right name, description, and parameters —
**you only write the `cypher:` block.** A `# HINT:` sits above it; the full answer
is in `solutions/`. Run the given command; match the expected result.

| # | Tool file | Goal | Test command | Expect |
| --- | --- | --- | --- | --- |
| 1 | `shared_device_accounts.yaml` | Devices used by >1 customer (shared attacker device) | `try_tool.py shared_device_accounts min_customers=2` | 1 device, 2 customers |
| 2 | `new_device_logins.yaml` | Logins from untrusted devices, filter by IP risk | `try_tool.py new_device_logins min_ip_risk=80` | 3 sessions |
| 3 | `contact_change_history.yaml` | Old-vs-new phone/email/address changes | `try_tool.py contact_change_history customer_id=CUST-1004` | 3 changes |
| 4 | `mule_hubs.yaml` | High-risk accounts drained by multiple victims | `try_tool.py mule_hubs min_victims=2` | `ACC-MULE-1`, £17,700 |
| 5 | `event_velocity.yaml` | Sessions with too-fast event chains (automation) | `try_tool.py event_velocity` | 3 sessions, 2–3.5/min |
| 6 | `ato_lifecycle.yaml` | Full access→change→payee→transfer chains | `try_tool.py ato_lifecycle` | 3 victims, minutes-to-payout |
| 7 | `ato_session_triage.yaml` | **Capstone:** score every session on all signals | `try_tool.py ato_session_triage min_risk=5` | 3 sessions score 9–10 |

> Prefix `try_tool.py` commands with `uv run python scripts/`. Stuck on one?
> `diff` your file against `solutions/<name>.yaml`, or run the reference directly:
> `TOOLS_DIR=solutions uv run python scripts/try_tool.py <name>`.

## 6. The payoff — talk to your tools

Restart the gateway once so the tools register, connect it in Claude Desktop
(see the main [README](README.md)), and run the conversational walkthrough in
[`data/demo_prompts.md`](data/demo_prompts.md). You'll never name a tool — the
model picks the ones you just built to investigate the takeovers, follow the
money, and brief you.
