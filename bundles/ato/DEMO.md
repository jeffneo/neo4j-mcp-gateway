# ATO demo — quick start

The fastest path to a working account-takeover demo. For the full narrative see
[`data/README.md`](data/README.md) (presenter runbook) or
[`data/demo_prompts.md`](data/demo_prompts.md) (conversational, for Claude Desktop).

## Prerequisites

A Neo4j instance with **APOC** installed — the official downstream server needs
`apoc.meta` for `get-schema`. Aura has it; a local Docker instance needs
`NEO4J_PLUGINS='["apoc"]'`. Credentials go in the repo-root `.env`.

## Run it

```bash
# 1. load the dataset (idempotent; everything it creates is tagged source:'ato-demo')
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
             -d "$NEO4J_DATABASE" -f bundles/ato/data/ato_demo.cypher

# 2. smoke-test every tool without MCP — do this before you present
uv run python scripts/validate_bundle.py

# 3. serve it. 'ato' is the default bundle, so no ACTIVE_BUNDLE is needed
uv run neo4j-mcp-gateway
```

Verify over MCP, or point Claude Desktop / VS Code at it (see the root
[README](../../README.md#client-configuration)):

```bash
npx @modelcontextprotocol/inspector --cli uv run neo4j-mcp-gateway --method tools/list
```

## What you get

**10 tools** — three proxied from the official Neo4j server (`get-schema`,
`read-cypher`, `write-cypher`) plus seven curated ATO tools:

| Tool | Answers |
| --- | --- |
| `usecase_ato_session_triage` | Which logins look like takeovers, and why? |
| `usecase_new_device_logins` | Which logins came from untrusted devices / risky IPs? |
| `usecase_ato_lifecycle` | Full access → contact change → payee → transfer chains |
| `usecase_event_velocity` | Which sessions moved too fast to be human? |
| `usecase_contact_change_history` | What was changed, old value vs new? |
| `usecase_mule_hubs` | Which accounts collect money from several victims? |
| `usecase_shared_device_accounts` | Which devices span multiple customers? |

> `list-gds-procedures` appears only when GDS is installed; the official server
> drops it otherwise.

## Expected results

The dataset plants three takeovers among twelve clean sessions plus one
false-positive traveler, so you can show both recall and precision.

| Step | Tool | Expect |
| --- | --- | --- |
| Triage | `usecase_ato_session_triage` (`min_risk=5`) | `SESS-A-1004` = 10, `SESS-B-1005/1006` = 9; everything else 0 |
| New device | `usecase_new_device_logins` (`min_ip_risk=80`) | 3 logins, VPN/Tor, one emulator |
| Lifecycle | `usecase_ato_lifecycle` | £15,000 / £9,500 / £8,200 at 2.0 / 2.5 / 2.5 minutes access→payout |
| Velocity | `usecase_event_velocity` | 7 events @ 3.5/min; 5 events @ 2.0/min ×2 |
| Forensics | `usecase_contact_change_history` (`customer_id=CUST-1004`) | phone → `+2348030000000`, email → disposable domain, address London → Lagos |
| Ring | `usecase_mule_hubs` (`min_victims=2`) | `ACC-MULE-1`, victims `CUST-1005`/`CUST-1006`, **£17,700** |
| Ring | `usecase_shared_device_accounts` | `DEV-ATT-RING` across both ring victims |
| Precision | ask about `SESS-FP-1003` | trusted device, MFA passed, **0** changes — correctly not flagged |

Ground truth for scoring lives on `Session.isFraud` / `Session.caseType`:
**3 fraud, 13 clean**. Detection tools never read those fields.

## Adding a tool

Drop a `*.yaml` into [`tools/`](tools/) and restart the gateway. Iterate without
restarting using the fast loop:

```bash
uv run python scripts/try_tool.py mule_hubs min_victims=2
```

Format reference: root [README](../../README.md#adding-a-use-case-tool-the-whole-point).

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Only `usecase_*` tools appear | The downstream couldn't reach Neo4j (it exits on failure) or APOC is missing. Check the gateway's stderr. |
| A new YAML tool doesn't show up | Tools load at startup and clients cache the list — restart the gateway and reconnect the client. |
| Tool returns a Neo4j error | The message includes the Neo4j error code; verify the Cypher and parameters with `scripts/try_tool.py`. |

## Security posture

This bundle declares `security.mode: open` in [`bundle.yaml`](bundle.yaml): tools
read the graph directly, with no per-caller entitlement filtering. That is a
deliberate choice — a fraud-ops team is uniformly entitled to the fraud graph. If
you need per-analyst restrictions, switch the bundle to `mediated`
(see [`docs/mediation-spec.md`](../../docs/mediation-spec.md)); no new code is required.
