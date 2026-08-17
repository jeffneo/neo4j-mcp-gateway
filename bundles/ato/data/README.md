# ATO demo data

> New here? Start with [`../DEMO.md`](../DEMO.md) for the quickest path and the
> validated regression record.

`ato_demo.cypher` generates a small, self-contained **account-takeover** dataset
with enough *legitimate baseline* that anomalies are actually detectable — plus a
deliberate false-positive so you can talk about precision, not just recall.

## Load it

```bash
cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
             -d "$NEO4J_DATABASE" -f bundles/ato/data/ato_demo.cypher
```

Every node it creates carries `source:'ato-demo'`; the script deletes that subset
at the top, so **re-running is safe and idempotent** and it never touches other
data. To remove the demo data entirely:

```cypher
MATCH (n {source:'ato-demo'}) DETACH DELETE n;
```

## What's in it

| Customers | 6 (`CUST-1001`…`1006`) with trusted devices, home IPs, PII, and weeks of normal logins/payments |
| --- | --- |
| **Case A — classic ATO** | `CUST-1004`: new emulator device + Lagos VPN IP, failed-login burst → MFA bypass → password reset + email/phone/address swap → new payee → £15k out |
| **Case B — mule ring** | `CUST-1005` + `CUST-1006`: shared attacker device/IP, both funnel to the **same** high-risk account `ACC-MULE-1` |
| **False positive** | `CUST-1003`: logs in from New York (new geo) but on their **own trusted device**, MFA passes, no contact changes, small purchase to an existing payee |

### Ground truth (for scoring only — detection queries must NOT read these)
- `Session.isFraud` — boolean label
- `Session.caseType` — `normal` | `ato-classic` | `ato-mule` | `legit-travel`

### Schema enrichments this data introduces
`USED_BY.firstSeen` / `.isTrusted`, `Authentication.mfaUsed` / `.mfaResult` (+ modelled
failed logins), `IP.isVpn` / `.isTor` / `.riskScore`, `Device.isEmulator`,
`Location.isHighRisk`, and a `(:ChangeCredential)` password-reset event in the `NEXT` chain.

## Example detection queries (read no ground-truth fields)

**1. Score every session by ATO signals** — cleanly ranks the 3 real ATO sessions
(9–10) above the false positive and normals (0):

```cypher
MATCH (c:Customer)-[:HAS_SESSION]->(s:Session)
OPTIONAL MATCH (s)-[:SESSION_USES_DEVICE]->(d:Device)-[u:USED_BY]->(c)
OPTIONAL MATCH (s)-[:USES_IP]->(ip:IP)
WITH c, s, coalesce(u.isTrusted,false) AS deviceTrusted, coalesce(ip.riskScore,0) AS ipRisk,
     COUNT { MATCH (s)-[:HAS_AUTHENTICATION]->(a) WHERE a.status='failed' }      AS fails,
     COUNT { MATCH (s)-[:HAS_AUTHENTICATION]->(a) WHERE a.mfaResult='bypassed' } AS mfaBypass,
     COUNT { MATCH (s)-[:HAS_CHANGE_CREDENTIAL]->(x) }                           AS pwReset,
     COUNT { MATCH (s)-[:HAS_CHANGE_EMAIL|HAS_CHANGE_PHONE|HAS_CHANGE_ADDRESS]->(x) } AS contactChg,
     COUNT { MATCH (s)-[:HAS_ADD_EXTERNAL_ACCOUNT]->()-[:ADD_ACCOUNT]->(:HighRiskJurisdiction) } AS hrPayee
RETURN s.sessionId AS session,
       (CASE WHEN NOT deviceTrusted THEN 2 ELSE 0 END)
     + (CASE WHEN ipRisk >= 80 THEN 2 ELSE 0 END)
     + (CASE WHEN fails >= 2 THEN 1 ELSE 0 END)
     + mfaBypass + pwReset
     + (CASE WHEN contactChg > 0 THEN 1 ELSE 0 END)
     + 2*hrPayee AS riskScore
ORDER BY riskScore DESC;
```

**2. Find mule hubs** — one high-risk beneficiary drained by multiple customers:

```cypher
MATCH (benef:HighRiskJurisdiction)<-[:BENEFITS_TO]-(t:Transaction)<-[:PERFORMS]-(:Account)<-[:HAS_ACCOUNT]-(c:Customer)
WITH benef, collect(DISTINCT c.customerId) AS victims, count(t) AS txns, sum(t.amount) AS totalOut
WHERE size(victims) > 1
RETURN benef.accountNumber AS muleAccount, victims, txns, totalOut;
```

---

# Demo script (presenter runbook)

A ~5-minute account-takeover walkthrough driven entirely through the gateway's
MCP tools, with a couple of raw `read-cypher` drill-downs. Each step lists a
**say it** line and the **tool call** (as a natural-language prompt to the
assistant, plus the underlying tool + args).

> Prefer to run it as a natural conversation where the model picks the tools
> (Claude Desktop / any MCP chat client)? See **[`demo_prompts.md`](demo_prompts.md)** —
> that's the intended payoff. This runbook is the tool-explicit version, handy
> with the MCP Inspector.

### Setup (once, before the demo)
1. **Wipe** the instance (dedicated demo DB only):
   `MATCH (n) DETACH DELETE n;`
2. **Load** the data: `cypher-shell ... -f bundles/ato/data/ato_demo.cypher`
3. **Restart** the gateway connector so the `usecase_*` tools register.

Expected baseline: 6 customers, 16 sessions (12 normal + 3 ATO + 1 travel).

---

### Act 1 — Triage: "which logins look like takeovers?"
> **Say:** "We're not searching for the word *fraud*. We score every session on
> what actually happened in the graph — device, location, auth, what changed."

- **Prompt:** *"Run the ATO session triage."*
- **Tool:** `usecase_ato_session_triage` (no args, or `min_risk: 5`)

> **Point at the result:** three sessions score 9–10; everything else is 0. The
> New York login (`SESS-FP-1003`) is **not** in the list — hold that thought.

### Act 2 — First signal: a device we've never trusted
> **Say:** "Every one of those started on a device that customer had never used
> before — from a VPN/Tor exit in a high-risk country."

- **Prompt:** *"Show new-device logins from risky IPs."*
- **Tool:** `usecase_new_device_logins` (`min_ip_risk: 80`)

> **Point:** emulator device, `isVpn`/`isTor` true, `deviceFirstSeen` = the
> moment of attack. In a graph, "new device" is one hop, not a batch job.

### Act 3 — The kill chain (the money shot)
> **Say:** "Follow what happened after login — one `NEXT` chain: bypass MFA,
> reset the password, swap the victim's phone/email/address so they can't get
> back in, add a brand-new payee, wire the money out. The graph gives us the
> whole lifecycle with the amount and how fast it happened."

- **Prompt:** *"Show me the complete account-takeover lifecycles — access through to the transfer."*
- **Tool:** `usecase_ato_lifecycle` → 3 victims, £15k/£9.5k/£8.2k, **2–2.5 min** access-to-payout, high-risk destination flagged.
- **Prompt:** *"Which of these look automated rather than human?"*
- **Tool:** `usecase_event_velocity` → same sessions at **2–3.5 events/min**.

> **Then the forensic beat — "what exactly did they change?"**
- **Prompt:** *"Show the contact-detail changes for customer CUST-1004, old vs new."*
- **Tool:** `usecase_contact_change_history` (`customer_id: CUST-1004`)
  → phone `+44…→+234…`, email → disposable domain, address London → Lagos.

🎤 That old-vs-new trail is what you hand the investigator to *restore* the victim.

### Act 4 — Connect the dots: it's a ring, not an incident
> **Say:** "Two *different* victims, two different sessions — but the money lands
> in the same account, reached from the same attacker device. That's the graph
> turning three isolated alerts into one ring."

- **Prompt:** *"Find money-mule hubs."*
- **Tool:** `usecase_mule_hubs` (`min_victims: 2`) → `ACC-MULE-1`, 2 victims, £17,700.
- **Prompt:** *"Is one device being used across multiple customers?"*
- **Tool:** `usecase_shared_device_accounts` → `DEV-ATT-RING` links CUST-1005 & CUST-1006 and their accounts.

### Act 5 — Precision: the login we *didn't* flag
> **Say:** "A rule that just fires on *login from a new country* would have paged
> someone for this. The graph clears it: it's the customer's own trusted device,
> MFA passed, nothing was changed, and the payment went to an existing payee."

- **Prompt:** *"Explain session SESS-FP-1003 — why is it low risk?"*
- **Tool:** `read-cypher`:

```cypher
MATCH (c:Customer)-[:HAS_SESSION]->(s:Session {sessionId:'SESS-FP-1003'})
MATCH (s)-[:SESSION_USES_DEVICE]->(d:Device)-[u:USED_BY]->(c)
MATCH (s)-[:HAS_AUTHENTICATION]->(a:Authentication)
RETURN d.deviceId AS device, u.isTrusted AS deviceTrusted, a.mfaResult AS mfa,
       COUNT { (s)-[:HAS_CHANGE_EMAIL|HAS_CHANGE_PHONE|HAS_CHANGE_ADDRESS|HAS_CHANGE_CREDENTIAL]->() } AS changes;
```

### The reveal — grade the triage
> **Say:** "Here's the ground truth we never let the detection query see. Our
> signal score matched it exactly — 3 fraud, 0 false positives."

- **Tool:** `read-cypher` (reads `isFraud`/`caseType` — for scoring only):

```cypher
MATCH (s:Session {source:'ato-demo'})
RETURN s.isFraud AS isFraud, s.caseType AS caseType, count(*) AS sessions
ORDER BY isFraud DESC, caseType;
```

**One-line close:** *"Same signals, but expressed as graph traversals — new
device, lock-out chain, shared mule — so takeovers and the rings behind them
fall out in a single query, not a data-pipeline project."*
