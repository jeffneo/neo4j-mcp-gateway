# Conversational demo — investigating ATO from a chat client

This is the payoff of the lab: instead of calling tools by name, an analyst just
**talks to Claude** (Claude Desktop, or any MCP chat client connected to the
gateway) and the model decides which gateway tool to use. Same graph, same tools
as the Inspector runbook — but now it feels like asking a colleague.

### Before you start
- Load the demo data (`data/ato_demo.cypher`) and **restart the gateway** so the
  `usecase_*` tools register.
- In the client, be in **Agent mode** with the `neo4j-gateway` tools enabled.
- Tip: if the model answers from memory instead of using a tool, nudge it with
  *"use the graph tools"* — and to keep the demo honest, tell it once:
  *"don't rely on any field that literally labels something as fraud — reason
  from behaviour."* (That keeps it off the `isFraud` / `caseType` ground truth.)

---

## Option A — Guided investigation (best for a live audience)

Paste these one at a time. Each shows **→ what it should trigger** and **🎤 what
to point out**.

**1. Orient**
> "What kind of data is in this graph? Give me the main node types and how they connect."

→ `get-schema`.  🎤 Set the scene: customers, accounts, sessions, devices, transfers.

**2. Triage**
> "We think we've had some account takeovers in the last few weeks. Look at the login sessions and tell me which ones are highest-risk, and *why*."

→ `usecase_ato_session_triage`.  🎤 Three sessions score 9–10, each with its reasons — and it never grepped for the word "fraud."

**3. First signal**
> "For those risky sessions, were the logins coming from devices or locations these customers hadn't used before?"

→ `usecase_new_device_logins`.  🎤 New/untrusted devices, VPN/Tor exits in a high-risk country, first-seen = the moment of attack.

**4. Drill into the worst one**
> "Take the top session. Walk me through exactly what the attacker did after logging in — step by step, in order."

→ `read-cypher` (the model writes a `NEXT`-chain traversal).  🎤 The lock-out chain: MFA bypass → password reset → swap phone/email/address → add payee → wire £15k, all in ~2 minutes.

**5. Is it a ring?**
> "Are any of these connected — same device, or money going to the same account? Could this be one operation rather than separate incidents?"

→ `usecase_mule_hubs` (and often a `read-cypher` for the shared device).  🎤 Two victims → one mule account, reached from one attacker device. The graph turns three alerts into one ring.

**6. Precision check (the one you *don't* freeze)**
> "Was there a login that looks suspicious on the surface — like from a new country — that's actually a legitimate customer? I don't want to lock someone out."

→ `read-cypher` / reasoning.  🎤 The New York login: own trusted device, MFA passed, nothing changed, normal payee. Context clears it.

**7. Brief it**
> "Summarise this as a short fraud-ops briefing: what happened, which customers and accounts, how much moved, and what I should do next."

→ synthesis (no tool).  🎤 The analyst's deliverable, straight from the graph.

---

## Option B — One-shot autonomous (the "wow")

A single kickoff prompt that lets the model plan and chain the tools itself:

> "You are a fraud investigator with live access to our retail-banking graph
> through the connected Neo4j tools. Investigate whether we've had any
> account-takeover attacks in the last month. Use the tools to gather evidence,
> follow the money, and work out whether any incidents are linked into a ring.
> Then give me a briefing: the specific customers, accounts, amounts, and
> recommended actions. Do **not** rely on any field that simply labels something
> as fraud — reason from the behaviour in the graph. Read-only, please."

🎤 Let it run and narrate its own tool calls — triage, then new-device, then the
mule hub, then a summary. This is the moment the room understands "MCP + graph."

---

## Alternative phrasings (if a tool doesn't trigger)

- Triage: *"Score the recent logins for takeover risk."* / *"Which sessions should a fraud analyst look at first?"*
- New device: *"Show me logins from devices we don't trust for that customer."*
- Mule hub: *"Which accounts are receiving money from more than one victim?"* / *"Find the mule accounts."*

## Notes for the presenter
- **Generic vs curated:** sometimes the model will write its own `read-cypher`
  instead of the purpose-built tool. That's a feature — show both: the curated
  `usecase_*` tools give consistent, blessed answers; `read-cypher` shows the
  open-ended power underneath. That contrast *is* the gateway's pitch.
- **Read-only safety:** the gateway also exposes `write-cypher`. For an
  unattended/autonomous run, set `NEO4J_READ_ONLY=true` in `.env` and restart —
  that removes the write tool entirely so nothing can mutate the graph mid-demo.
