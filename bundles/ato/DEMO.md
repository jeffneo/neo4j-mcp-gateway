# ATO demo — run book and regression record

Everything the account-takeover demo could do before the bundles refactor still
works, unchanged, on this branch. This file is both the **quickest path to a
working demo** and the **evidence** that the refactor didn't cost anything.

Validated on **Neo4j 5.26 Enterprise + APOC** (the official downstream server needs
`apoc.meta` for `get-schema`; Aura has it, a bare local Neo4j needs
`NEO4J_PLUGINS='["apoc"]'`).

---

## What changed, and what didn't

| | Before (old `main`) | Now |
| --- | --- | --- |
| Tool names | `usecase_ato_session_triage`, … | **identical** |
| Client config (Claude Desktop / VS Code) | `uv run … neo4j-mcp-gateway` | **identical** — `ato` is the default bundle, no env needed |
| Proxied official tools | `get-schema`, `read-cypher`, `write-cypher` | **identical** |
| Add a tool | drop a YAML in `tools/`, restart | same, in `bundles/ato/tools/` |
| Dataset path | `data/ato_demo.cypher` | `bundles/ato/data/ato_demo.cypher` |
| Docs path | `data/README.md`, `data/demo_prompts.md` | `bundles/ato/data/…` |

**The only breaking change is file paths.** No tool name, client config, or
behaviour changed. Anyone with an existing Claude Desktop entry can keep it.

---

## Run the demo

```bash
# 1. load the dataset (idempotent; namespaced source:'ato-demo')
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
             -d "$NEO4J_DATABASE" -f bundles/ato/data/ato_demo.cypher

# 2. sanity-check every tool without MCP (fast loop)
uv run python scripts/validate_bundle.py

# 3. serve it — no ACTIVE_BUNDLE needed, 'ato' is the default
uv run neo4j-mcp-gateway
```

Then follow one of:

- **[`data/README.md`](data/README.md)** — presenter runbook (tool-explicit; good with the MCP Inspector)
- **[`data/demo_prompts.md`](data/demo_prompts.md)** — conversational prompts for Claude Desktop (the payoff)

---

## Regression evidence

Each row below was executed against a live Enterprise instance on this branch.

### Discovery and wiring

| Check | Result |
| --- | --- |
| `--list-bundles` | `neo4j-ato-gateway`, `neo4j-iam-gateway` |
| Default bundle with no `ACTIVE_BUNDLE` | resolves to `ato`, `security: open` |
| Dataset loads from the documented path | 16 sessions (12 normal, 3 ATO, 1 travel) |
| `validate_bundle.py` | **7 tools, 0 failures** |
| Drop a new `*.yaml` into `tools/` | picked up and runs |

### MCP surface (via `npx @modelcontextprotocol/inspector --cli`, real downstream)

`tools/list` → **10 tools**, names unchanged from old `main`:

```
get-schema  read-cypher  write-cypher
usecase_ato_lifecycle          usecase_ato_session_triage
usecase_contact_change_history usecase_event_velocity
usecase_mule_hubs              usecase_new_device_logins
usecase_shared_device_accounts
```

`tools/call`:
- `get-schema` → returns the graph schema through the proxy
- `usecase_ato_session_triage min_risk=5` → `SESS-A-1004` (10), `SESS-B-1006` (9), `SESS-B-1005` (9)

> `list-gds-procedures` is absent because GDS isn't installed on the test instance —
> the official server drops it. Same behaviour as before; not a regression.

### The demo narrative (every documented figure re-verified)

| Act | Tool / query | Result |
| --- | --- | --- |
| 1 — Triage | `usecase_ato_session_triage` | 3 sessions score 9–10; the false-positive traveler scores 0 |
| 2 — New device | `usecase_new_device_logins` | 3 untrusted-device logins on VPN/Tor IPs |
| 3 — Lifecycle | `usecase_ato_lifecycle` | £15,000 / £9,500 / £8,200 at **2.0 / 2.5 / 2.5 min** access→payout |
| 3 — Velocity | `usecase_event_velocity` | 7 events @ 3.5/min; 5 events @ 2.0/min ×2 |
| 3 — Forensics | `usecase_contact_change_history CUST-1004` | phone `+447700900104→+2348030000000`, email → disposable, address London → Lagos |
| 3 — Kill chain | `read-cypher` (NEXT traversal) | `Authentication → ChangeCredential → ChangePhone → ChangeEmail → ChangeAddress → AddExternalAccount → Transfer` |
| 4 — Mule hub | `usecase_mule_hubs min_victims=2` | `ACC-MULE-1`, victims `CUST-1005`/`CUST-1006`, **£17,700** |
| 4 — Shared device | `usecase_shared_device_accounts` | `DEV-ATT-RING` across both ring victims |
| 5 — False positive | `read-cypher` (SESS-FP-1003) | trusted device, MFA `passed`, **0** changes |
| Reveal — grade | `read-cypher` on ground truth | 3 fraud (1 classic + 2 mule), 13 clean |

---

## What the refactor added for this bundle

Nothing you must use, but available:

- **`security.mode: open`** is now declared explicitly in
  [`bundle.yaml`](bundle.yaml) — a recorded decision (this audience is uniformly
  entitled to the fraud graph) rather than an unstated default. If ATO ever needed
  per-analyst entitlements, it flips to `mediated` without new code.
- **`scripts/validate_bundle.py`** — runs every tool against a live database and
  exits non-zero, so the demo can be smoke-tested in one command before you present.
- **Multi-bundle** — `ACTIVE_BUNDLE=ato,iam` serves both use cases from one
  endpoint (tools become `ato_*` / `iam_*`). Single-bundle names are unaffected.

## Gotchas

- The official downstream **exits if it can't reach Neo4j**, so bad credentials show
  up as *missing* `get-schema` / `*-cypher` tools rather than an error. Check the
  gateway's stderr.
- It also needs **APOC**. Without it the proxied tools disappear while the
  `usecase_*` tools keep working — a confusing half-broken state if you don't expect it.
- A new or edited YAML tool needs a **gateway restart** (and a client reconnect);
  `scripts/try_tool.py` needs neither, which is why it's the inner loop.
