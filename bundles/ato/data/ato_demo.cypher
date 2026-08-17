// =====================================================================
//  Account-Takeover (ATO) demo dataset generator
//  Target: Neo4j 5.x
//
//  Run (from the repo root):
//    cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
//                 -d "$NEO4J_DATABASE" -f data/ato_demo.cypher
//
//  Idempotent: the script deletes ONLY its own subset first (every node it
//  creates carries `source:'ato-demo'`), so you can re-run it freely. It does
//  NOT touch any pre-existing data.
//
//  Ground truth for scoring lives on Session:
//    - isFraud  : boolean  (the label to grade against)
//    - caseType : 'normal' | 'ato-classic' | 'ato-mule' | 'legit-travel'
//  Detection Cypher must NEVER read these — they exist only to score results.
//  IDs are deliberately neutral (no 'FRAUD'/'SUSPICIOUS' tells).
//
//  Schema enrichments introduced here (over the original model):
//    - USED_BY.firstSeen / USED_BY.isTrusted   -> new vs. trusted device
//    - Authentication.mfaUsed / .mfaResult      -> MFA bypass / step-down
//    - Authentication modelled for FAILED logins too (credential stuffing)
//    - IP.isVpn / .isTor / .riskScore, Device.isEmulator, Location.isHighRisk
//    - (:ChangeCredential {credentialType}) password-reset event in NEXT chain
//    - eventId on session sub-events (referenceable + cleanable)
// =====================================================================


// ---------- 0. Constraints & indexes (safe to re-run) ----------
CREATE CONSTRAINT customer_id  IF NOT EXISTS FOR (c:Customer)    REQUIRE c.customerId    IS UNIQUE;
CREATE CONSTRAINT session_id   IF NOT EXISTS FOR (s:Session)     REQUIRE s.sessionId     IS UNIQUE;
CREATE CONSTRAINT account_no   IF NOT EXISTS FOR (a:Account)     REQUIRE a.accountNumber IS UNIQUE;
CREATE CONSTRAINT device_id    IF NOT EXISTS FOR (d:Device)      REQUIRE d.deviceId      IS UNIQUE;
CREATE CONSTRAINT ip_addr      IF NOT EXISTS FOR (i:IP)          REQUIRE i.ipAddress     IS UNIQUE;
CREATE CONSTRAINT txn_id       IF NOT EXISTS FOR (t:Transaction) REQUIRE t.transactionId IS UNIQUE;
CREATE CONSTRAINT country_code IF NOT EXISTS FOR (c:Country)     REQUIRE c.code          IS UNIQUE;
CREATE INDEX email_addr IF NOT EXISTS FOR (e:Email) ON (e.address);
CREATE INDEX phone_num  IF NOT EXISTS FOR (p:Phone) ON (p.number);


// ---------- 1. Reset ONLY this demo's data ----------
MATCH (n {source:'ato-demo'}) DETACH DELETE n;


// ---------- 2. Reference data: countries ----------
UNWIND [
  {code:'GB', name:'United Kingdom'},
  {code:'US', name:'United States'},
  {code:'NG', name:'Nigeria'}
] AS c
MERGE (n:Country {code:c.code})
  ON CREATE SET n.name = c.name, n.source = 'ato-demo'
  ON MATCH  SET n.name = coalesce(n.name, c.name);


// ---------- 3. Locations (+ LOCATED_IN country) ----------
UNWIND [
  {city:'London',     cc:'GB', lat:51.5072, lon:-0.1276, pc:'EC1A 1BB', risk:false},
  {city:'Manchester', cc:'GB', lat:53.4808, lon:-2.2426, pc:'M1 1AA',   risk:false},
  {city:'New York',   cc:'US', lat:40.7128, lon:-74.0060,pc:'10001',    risk:false},
  {city:'Lagos',      cc:'NG', lat:6.5244,  lon:3.3792,  pc:'100001',   risk:true}
] AS L
MATCH (co:Country {code:L.cc})
MERGE (loc:Location {city:L.city, country:co.name})
  ON CREATE SET loc.latitude=L.lat, loc.longitude=L.lon, loc.postCode=L.pc,
                loc.isHighRisk=L.risk, loc.createdAt=datetime('2026-01-01T00:00:00Z'),
                loc.source='ato-demo'
MERGE (loc)-[:LOCATED_IN]->(co);


// ---------- 4. ISPs ----------
UNWIND ['BT Group','Comcast','AnonVPN Networks'] AS ispName
MERGE (isp:ISP {name:ispName})
  ON CREATE SET isp.createdAt=datetime('2026-01-01T00:00:00Z'), isp.source='ato-demo';


// ---------- 5. IPs (+ ISP + Location) ----------
//  Home IPs are low-risk; the two Lagos IPs are the attacker infrastructure.
UNWIND [
  {ip:'81.100.10.5',  isp:'BT Group',        city:'London',     vpn:false, tor:false, risk:5},
  {ip:'81.100.10.6',  isp:'BT Group',        city:'London',     vpn:false, tor:false, risk:5},
  {ip:'81.100.10.7',  isp:'BT Group',        city:'London',     vpn:false, tor:false, risk:5},
  {ip:'81.100.10.8',  isp:'BT Group',        city:'London',     vpn:false, tor:false, risk:5},
  {ip:'81.100.20.9',  isp:'BT Group',        city:'Manchester', vpn:false, tor:false, risk:5},
  {ip:'81.100.20.10', isp:'BT Group',        city:'Manchester', vpn:false, tor:false, risk:5},
  {ip:'71.200.30.4',  isp:'Comcast',         city:'New York',   vpn:false, tor:false, risk:15},
  {ip:'196.216.99.7', isp:'AnonVPN Networks',city:'Lagos',      vpn:true,  tor:false, risk:88},
  {ip:'196.216.99.8', isp:'AnonVPN Networks',city:'Lagos',      vpn:true,  tor:true,  risk:95}
] AS X
MATCH (isp:ISP {name:X.isp})
MATCH (loc:Location {city:X.city})
MERGE (ip:IP {ipAddress:X.ip})
  ON CREATE SET ip.createdAt=datetime('2026-01-01T00:00:00Z'),
                ip.isVpn=X.vpn, ip.isTor=X.tor, ip.riskScore=X.risk, ip.source='ato-demo'
MERGE (ip)-[:IS_ALLOCATED_TO]->(isp)
MERGE (ip)-[:LOCATED_IN]->(loc);


// ---------- 6. External beneficiary accounts ----------
//  Two benign payees (established) + one high-risk mule + one attacker payee.
MATCH (gb:Country {code:'GB'}), (us:Country {code:'US'})
MERGE (p1:Account {accountNumber:'ACC-PAYEE-1'}) ON CREATE SET p1:External, p1.accountType='external', p1.source='ato-demo'
MERGE (p2:Account {accountNumber:'ACC-PAYEE-2'}) ON CREATE SET p2:External, p2.accountType='external', p2.source='ato-demo'
MERGE (p1)-[:IS_HOSTED]->(gb)
MERGE (p2)-[:IS_HOSTED]->(us);

MATCH (ng:Country {code:'NG'})
MERGE (mule:Account {accountNumber:'ACC-MULE-1'}) ON CREATE SET mule:External:HighRiskJurisdiction, mule.accountType='external', mule.source='ato-demo'
MERGE (att:Account  {accountNumber:'ACC-ATT-1'})  ON CREATE SET att:External:HighRiskJurisdiction,  att.accountType='external',  att.source='ato-demo'
MERGE (mule)-[:IS_HOSTED]->(ng)
MERGE (att)-[:IS_HOSTED]->(ng);


// ---------- 7. Customers + internal accounts + trusted devices ----------
UNWIND [
  {cid:'CUST-1001', acc:'ACC-1001', dev:'DEV-1001', dtype:'iPhone 14',         ua:'iOS/17 Safari'},
  {cid:'CUST-1002', acc:'ACC-1002', dev:'DEV-1002', dtype:'Samsung Galaxy S22',ua:'Android/13 Chrome'},
  {cid:'CUST-1003', acc:'ACC-1003', dev:'DEV-1003', dtype:'iPhone 13',         ua:'iOS/16 Safari'},
  {cid:'CUST-1004', acc:'ACC-1004', dev:'DEV-1004', dtype:'Pixel 7',           ua:'Android/14 Chrome'},
  {cid:'CUST-1005', acc:'ACC-1005', dev:'DEV-1005', dtype:'iPhone 15',         ua:'iOS/18 Safari'},
  {cid:'CUST-1006', acc:'ACC-1006', dev:'DEV-1006', dtype:'MacBook Pro',       ua:'macOS Chrome/120'}
] AS C
MERGE (cust:Customer {customerId:C.cid}) ON CREATE SET cust.source='ato-demo'
MERGE (a:Account {accountNumber:C.acc})
  ON CREATE SET a:Internal, a.accountType='current',
                a.openedDate=datetime('2024-06-01T00:00:00Z'), a.source='ato-demo'
MERGE (cust)-[r:HAS_ACCOUNT]->(a) ON CREATE SET r.role='owner', r.since=datetime('2024-06-01T00:00:00Z')
MERGE (d:Device {deviceId:C.dev})
  ON CREATE SET d.deviceType=C.dtype, d.userAgent=C.ua, d.isEmulator=false,
                d.createdAt=datetime('2026-01-10T00:00:00Z'), d.source='ato-demo'
MERGE (d)-[u:USED_BY]->(cust)
  ON CREATE SET u.firstSeen=datetime('2026-01-15T00:00:00Z'), u.isTrusted=true,
                u.lastUsed=datetime('2026-07-20T00:00:00Z')
WITH a
MATCH (gb:Country {code:'GB'})
MERGE (a)-[:IS_HOSTED]->(gb);


// ---------- 8. Customer PII (Email / Phone / Address) ----------
UNWIND [
  {cid:'CUST-1001', email:'user1001@example.com', phone:'+447700900101', addr:'10 King St',    town:'London',     region:'Greater London',  pc:'EC1A 1BB', lat:51.5155, lon:-0.0922},
  {cid:'CUST-1002', email:'user1002@example.com', phone:'+447700900102', addr:'4 Deansgate',    town:'Manchester', region:'Greater Manchester',pc:'M3 2EN',  lat:53.4783, lon:-2.2500},
  {cid:'CUST-1003', email:'user1003@example.com', phone:'+447700900103', addr:'22 Baker St',    town:'London',     region:'Greater London',  pc:'W1U 3BW', lat:51.5205, lon:-0.1568},
  {cid:'CUST-1004', email:'user1004@example.com', phone:'+447700900104', addr:'8 Abbey Rd',     town:'London',     region:'Greater London',  pc:'NW8 9AY', lat:51.5321, lon:-0.1776},
  {cid:'CUST-1005', email:'user1005@example.com', phone:'+447700900105', addr:'15 Oxford Rd',   town:'Manchester', region:'Greater Manchester',pc:'M1 5QA',  lat:53.4720, lon:-2.2400},
  {cid:'CUST-1006', email:'user1006@example.com', phone:'+447700900106', addr:'3 Camden High St',town:'London',    region:'Greater London',  pc:'NW1 7JE', lat:51.5390, lon:-0.1426}
] AS P
MATCH (c:Customer {customerId:P.cid})
MERGE (e:Email {address:P.email})
  ON CREATE SET e.domain='example.com', e.emailType='personal', e.createdAt=datetime('2024-06-01T00:00:00Z'), e.source='ato-demo'
MERGE (ph:Phone {number:P.phone})
  ON CREATE SET ph.countryCode='GB', ph.createdAt=datetime('2024-06-01T00:00:00Z'), ph.source='ato-demo'
CREATE (ad:Address {addressLine1:P.addr, addressLine2:'', postTown:P.town, region:P.region,
                    postCode:P.pc, latitude:P.lat, longitude:P.lon,
                    createdAt:datetime('2024-06-01T00:00:00Z'), source:'ato-demo'})
MERGE (c)-[:HAS_EMAIL   {since:datetime('2024-06-01T00:00:00Z')}]->(e)
MERGE (c)-[:HAS_PHONE   {since:datetime('2024-06-01T00:00:00Z')}]->(ph)
CREATE (c)-[:HAS_ADDRESS {addedAt:datetime('2024-06-01T00:00:00Z'), isCurrent:true, lastChangedAt:datetime('2024-06-01T00:00:00Z')}]->(ad)
WITH ad
MATCH (gb:Country {code:'GB'})
MERGE (ad)-[:LOCATED_IN {createdAt:datetime('2024-06-01T00:00:00Z')}]->(gb);


// ---------- 9. Baseline legitimate sessions (the "normal" to detect against) ----------
//  Each is a clean login: home IP, trusted device, MFA passed. caseType='normal'.
UNWIND [
  {cid:'CUST-1001', sid:'SESS-1001-1', ts:'2026-05-05T08:15:00Z', ip:'81.100.10.5',  dev:'DEV-1001'},
  {cid:'CUST-1001', sid:'SESS-1001-2', ts:'2026-06-10T18:40:00Z', ip:'81.100.10.5',  dev:'DEV-1001'},
  {cid:'CUST-1002', sid:'SESS-1002-1', ts:'2026-05-07T07:50:00Z', ip:'81.100.20.9',  dev:'DEV-1002'},
  {cid:'CUST-1002', sid:'SESS-1002-2', ts:'2026-06-12T20:05:00Z', ip:'81.100.20.9',  dev:'DEV-1002'},
  {cid:'CUST-1003', sid:'SESS-1003-1', ts:'2026-05-09T09:30:00Z', ip:'81.100.10.6',  dev:'DEV-1003'},
  {cid:'CUST-1003', sid:'SESS-1003-2', ts:'2026-06-15T12:10:00Z', ip:'81.100.10.6',  dev:'DEV-1003'},
  {cid:'CUST-1004', sid:'SESS-1004-1', ts:'2026-05-11T08:00:00Z', ip:'81.100.10.7',  dev:'DEV-1004'},
  {cid:'CUST-1004', sid:'SESS-1004-2', ts:'2026-06-18T19:25:00Z', ip:'81.100.10.7',  dev:'DEV-1004'},
  {cid:'CUST-1005', sid:'SESS-1005-1', ts:'2026-05-13T10:45:00Z', ip:'81.100.20.10', dev:'DEV-1005'},
  {cid:'CUST-1005', sid:'SESS-1005-2', ts:'2026-06-20T21:15:00Z', ip:'81.100.20.10', dev:'DEV-1005'},
  {cid:'CUST-1006', sid:'SESS-1006-1', ts:'2026-05-15T08:35:00Z', ip:'81.100.10.8',  dev:'DEV-1006'},
  {cid:'CUST-1006', sid:'SESS-1006-2', ts:'2026-06-22T17:55:00Z', ip:'81.100.10.8',  dev:'DEV-1006'}
] AS S
MATCH (c:Customer {customerId:S.cid})
MATCH (ip:IP {ipAddress:S.ip})
MATCH (d:Device {deviceId:S.dev})
CREATE (sess:Session {sessionId:S.sid, status:'success', createdAt:datetime(S.ts),
                      isFraud:false, caseType:'normal', source:'ato-demo'})
CREATE (c)-[:HAS_SESSION]->(sess)
MERGE (sess)-[:USES_IP]->(ip)
MERGE (sess)-[:SESSION_USES_DEVICE]->(d)
CREATE (auth:Authentication {eventId:S.sid+'-ok', method:'password', status:'success',
                             mfaUsed:true, mfaResult:'passed', createdAt:datetime(S.ts), source:'ato-demo'})
CREATE (sess)-[:HAS_AUTHENTICATION]->(auth)
MERGE (c)-[:CONNECTS]->(auth);


// ---------- 10. Baseline legitimate transactions (established payees, normal amounts) ----------
UNWIND [
  {from:'ACC-1001', to:'ACC-PAYEE-1', amt:65.0,  ts:'2026-05-06T09:00:00Z', tid:'TXN-1001-1', type:'payment',  msg:'Utility bill'},
  {from:'ACC-1001', to:'ACC-PAYEE-2', amt:220.0, ts:'2026-06-11T13:20:00Z', tid:'TXN-1001-2', type:'purchase', msg:'Electronics'},
  {from:'ACC-1002', to:'ACC-PAYEE-1', amt:48.0,  ts:'2026-05-08T10:10:00Z', tid:'TXN-1002-1', type:'payment',  msg:'Utility bill'},
  {from:'ACC-1003', to:'ACC-PAYEE-2', amt:310.0, ts:'2026-06-16T15:45:00Z', tid:'TXN-1003-1', type:'purchase', msg:'Furniture'},
  {from:'ACC-1004', to:'ACC-PAYEE-1', amt:75.0,  ts:'2026-05-12T08:30:00Z', tid:'TXN-1004-1', type:'payment',  msg:'Utility bill'},
  {from:'ACC-1004', to:'ACC-PAYEE-2', amt:540.0, ts:'2026-06-19T11:05:00Z', tid:'TXN-1004-2', type:'purchase', msg:'Home appliance'},
  {from:'ACC-1005', to:'ACC-PAYEE-1', amt:90.0,  ts:'2026-05-14T09:15:00Z', tid:'TXN-1005-1', type:'payment',  msg:'Utility bill'},
  {from:'ACC-1006', to:'ACC-PAYEE-2', amt:130.0, ts:'2026-06-23T16:35:00Z', tid:'TXN-1006-1', type:'purchase', msg:'Clothing'}
] AS T
MATCH (from:Account {accountNumber:T.from})
MATCH (to:Account   {accountNumber:T.to})
CREATE (tx:Transaction {transactionId:T.tid, amount:T.amt, currency:'GBP', date:datetime(T.ts),
                        type:T.type, message:T.msg, source:'ato-demo'})
CREATE (from)-[:PERFORMS]->(tx)
CREATE (tx)-[:BENEFITS_TO]->(to);


// ================================================================
//  ATO CASE A — classic single-victim takeover (CUST-1004)
//  New emulator device + Lagos VPN IP, failed-login burst -> MFA bypass,
//  password reset + contact swaps (victim lock-out), new payee, £15k out.
// ================================================================
MATCH (c:Customer {customerId:'CUST-1004'})
MATCH (victimAcc:Account {accountNumber:'ACC-1004'})
MATCH (payee:Account {accountNumber:'ACC-ATT-1'})
MATCH (ip:IP {ipAddress:'196.216.99.7'})
MATCH (c)-[:HAS_EMAIL]->(oldEmail:Email)
MATCH (c)-[:HAS_PHONE]->(oldPhone:Phone)
MATCH (c)-[:HAS_ADDRESS]->(oldAddr:Address)
// attacker device: brand-new, untrusted, emulator
MERGE (dev:Device {deviceId:'DEV-ATT-1'})
  ON CREATE SET dev.deviceType='Android (emulator)', dev.userAgent='okhttp/4.9',
                dev.isEmulator=true, dev.createdAt=datetime('2026-07-28T02:09:00Z'), dev.source='ato-demo'
MERGE (dev)-[u:USED_BY]->(c)
  ON CREATE SET u.firstSeen=datetime('2026-07-28T02:14:00Z'), u.isTrusted=false, u.lastUsed=datetime('2026-07-28T02:14:00Z')
// fraudulent session
CREATE (s:Session {sessionId:'SESS-A-1004', status:'success', createdAt:datetime('2026-07-28T02:14:00Z'),
                   isFraud:true, caseType:'ato-classic', source:'ato-demo'})
CREATE (c)-[:HAS_SESSION]->(s)
MERGE (s)-[:USES_IP]->(ip)
MERGE (s)-[:SESSION_USES_DEVICE]->(dev)
// credential-stuffing: 3 failed attempts then a success that bypasses MFA
CREATE (f1:Authentication {eventId:'SESS-A-1004-f1', method:'password', status:'failed', mfaUsed:false, mfaResult:'not_reached', createdAt:datetime('2026-07-28T02:10:00Z'), source:'ato-demo'})
CREATE (f2:Authentication {eventId:'SESS-A-1004-f2', method:'password', status:'failed', mfaUsed:false, mfaResult:'not_reached', createdAt:datetime('2026-07-28T02:11:30Z'), source:'ato-demo'})
CREATE (f3:Authentication {eventId:'SESS-A-1004-f3', method:'password', status:'failed', mfaUsed:false, mfaResult:'not_reached', createdAt:datetime('2026-07-28T02:12:45Z'), source:'ato-demo'})
CREATE (ok:Authentication {eventId:'SESS-A-1004-ok', method:'password', status:'success', mfaUsed:true, mfaResult:'bypassed', createdAt:datetime('2026-07-28T02:14:00Z'), source:'ato-demo'})
CREATE (s)-[:HAS_AUTHENTICATION]->(f1)
CREATE (s)-[:HAS_AUTHENTICATION]->(f2)
CREATE (s)-[:HAS_AUTHENTICATION]->(f3)
CREATE (s)-[:HAS_AUTHENTICATION]->(ok)
MERGE (c)-[:CONNECTS]->(ok)
// attacker-controlled new contact details
CREATE (newEmail:Email {address:'d4v3.secure@mail-proton-x.co', domain:'mail-proton-x.co', emailType:'personal', createdAt:datetime('2026-07-28T02:15:00Z'), source:'ato-demo'})
CREATE (newPhone:Phone {number:'+2348030000000', countryCode:'NG', createdAt:datetime('2026-07-28T02:14:40Z'), source:'ato-demo'})
CREATE (newAddr:Address {addressLine1:'12 Marina Rd', addressLine2:'', postTown:'Lagos', region:'Lagos', postCode:'100001', latitude:6.4531, longitude:3.3958, createdAt:datetime('2026-07-28T02:15:20Z'), source:'ato-demo'})
// lock-out events (password reset + contact swaps), time-ordered along NEXT
CREATE (cc:ChangeCredential {eventId:'SESS-A-1004-cc', credentialType:'password', createdAt:datetime('2026-07-28T02:14:20Z'), source:'ato-demo'})
CREATE (cp:ChangePhone      {eventId:'SESS-A-1004-cp', createdAt:datetime('2026-07-28T02:14:40Z'), source:'ato-demo'})
CREATE (ce:ChangeEmail      {eventId:'SESS-A-1004-ce', createdAt:datetime('2026-07-28T02:15:00Z'), source:'ato-demo'})
CREATE (ca:ChangeAddress    {eventId:'SESS-A-1004-ca', createdAt:datetime('2026-07-28T02:15:20Z'), source:'ato-demo'})
CREATE (s)-[:HAS_CHANGE_CREDENTIAL]->(cc)
CREATE (s)-[:HAS_CHANGE_PHONE]->(cp)
CREATE (s)-[:HAS_CHANGE_EMAIL]->(ce)
CREATE (s)-[:HAS_CHANGE_ADDRESS]->(ca)
CREATE (cp)-[:OLD_PHONE]->(oldPhone)
CREATE (cp)-[:NEW_PHONE]->(newPhone)
CREATE (ce)-[:OLD_EMAIL]->(oldEmail)
CREATE (ce)-[:NEW_EMAIL]->(newEmail)
CREATE (ca)-[:OLD_ADDRESS]->(oldAddr)
CREATE (ca)-[:NEW_ADDRESS]->(newAddr)
// add attacker payee + rapid high-value transfer
CREATE (ae:AddExternalAccount {eventId:'SESS-A-1004-ae', createdAt:datetime('2026-07-28T02:15:40Z'), source:'ato-demo'})
CREATE (s)-[:HAS_ADD_EXTERNAL_ACCOUNT]->(ae)
CREATE (ae)-[:ADD_ACCOUNT]->(payee)
CREATE (tr:Transfer {eventId:'SESS-A-1004-tr', createdAt:datetime('2026-07-28T02:16:00Z'), source:'ato-demo'})
CREATE (s)-[:HAS_TRANSFER]->(tr)
CREATE (tx:Transaction {transactionId:'TXN-A-1004', amount:15000.0, currency:'GBP', date:datetime('2026-07-28T02:16:00Z'), type:'transfer', message:'Account consolidation', source:'ato-demo'})
CREATE (tr)-[:HAS_TRANSACTION]->(tx)
CREATE (victimAcc)-[:PERFORMS]->(tx)
CREATE (tx)-[:BENEFITS_TO]->(payee)
// intra-session sequence
CREATE (ok)-[:NEXT]->(cc)-[:NEXT]->(cp)-[:NEXT]->(ce)-[:NEXT]->(ca)-[:NEXT]->(ae)-[:NEXT]->(tr);


// ================================================================
//  ATO CASE B — mule ring: TWO victims funnel to ONE external account.
//  Shared attacker device + shared Lagos VPN/Tor IP; both payouts land on
//  ACC-MULE-1. This is the graph-native "hub" pattern.
// ================================================================
MATCH (mule:Account {accountNumber:'ACC-MULE-1'})
MATCH (ip:IP {ipAddress:'196.216.99.8'})
MERGE (dev:Device {deviceId:'DEV-ATT-RING'})
  ON CREATE SET dev.deviceType='Windows 10', dev.userAgent='HeadlessChrome/120',
                dev.isEmulator=false, dev.createdAt=datetime('2026-07-30T03:00:00Z'), dev.source='ato-demo'
WITH mule, ip, dev
UNWIND [
  {cid:'CUST-1005', sid:'SESS-B-1005', acc:'ACC-1005', amt:9500.0, ts:'2026-07-30T03:05:00Z', newEmail:'erin.helpdesk@quickmail-xy.net'},
  {cid:'CUST-1006', sid:'SESS-B-1006', acc:'ACC-1006', amt:8200.0, ts:'2026-07-30T03:22:00Z', newEmail:'frank.support@quickmail-xy.net'}
] AS V
MATCH (c:Customer {customerId:V.cid})
MATCH (victimAcc:Account {accountNumber:V.acc})
MATCH (c)-[:HAS_EMAIL]->(oldEmail:Email)
MERGE (dev)-[u:USED_BY]->(c)
  ON CREATE SET u.firstSeen=datetime(V.ts), u.isTrusted=false, u.lastUsed=datetime(V.ts)
CREATE (s:Session {sessionId:V.sid, status:'success', createdAt:datetime(V.ts),
                   isFraud:true, caseType:'ato-mule', source:'ato-demo'})
CREATE (c)-[:HAS_SESSION]->(s)
MERGE (s)-[:USES_IP]->(ip)
MERGE (s)-[:SESSION_USES_DEVICE]->(dev)
CREATE (ok:Authentication {eventId:V.sid+'-ok', method:'password', status:'success', mfaUsed:true, mfaResult:'bypassed', createdAt:datetime(V.ts), source:'ato-demo'})
CREATE (s)-[:HAS_AUTHENTICATION]->(ok)
MERGE (c)-[:CONNECTS]->(ok)
CREATE (cc:ChangeCredential {eventId:V.sid+'-cc', credentialType:'password', createdAt:datetime(V.ts)+duration('PT1M'), source:'ato-demo'})
CREATE (s)-[:HAS_CHANGE_CREDENTIAL]->(cc)
CREATE (newEmail:Email {address:V.newEmail, domain:'quickmail-xy.net', emailType:'personal', createdAt:datetime(V.ts)+duration('PT1M30S'), source:'ato-demo'})
CREATE (ce:ChangeEmail {eventId:V.sid+'-ce', createdAt:datetime(V.ts)+duration('PT1M30S'), source:'ato-demo'})
CREATE (s)-[:HAS_CHANGE_EMAIL]->(ce)
CREATE (ce)-[:OLD_EMAIL]->(oldEmail)
CREATE (ce)-[:NEW_EMAIL]->(newEmail)
CREATE (ae:AddExternalAccount {eventId:V.sid+'-ae', createdAt:datetime(V.ts)+duration('PT2M'), source:'ato-demo'})
CREATE (s)-[:HAS_ADD_EXTERNAL_ACCOUNT]->(ae)
CREATE (ae)-[:ADD_ACCOUNT]->(mule)
CREATE (tr:Transfer {eventId:V.sid+'-tr', createdAt:datetime(V.ts)+duration('PT2M30S'), source:'ato-demo'})
CREATE (s)-[:HAS_TRANSFER]->(tr)
CREATE (tx:Transaction {transactionId:V.sid+'-tx', amount:V.amt, currency:'GBP', date:datetime(V.ts)+duration('PT2M30S'), type:'transfer', message:'Urgent supplier payment', source:'ato-demo'})
CREATE (tr)-[:HAS_TRANSACTION]->(tx)
CREATE (victimAcc)-[:PERFORMS]->(tx)
CREATE (tx)-[:BENEFITS_TO]->(mule)
CREATE (ok)-[:NEXT]->(cc)-[:NEXT]->(ce)-[:NEXT]->(ae)-[:NEXT]->(tr);


// ================================================================
//  FALSE POSITIVE — legit travel (CUST-1003)
//  New geo (New York) but OWN trusted device, MFA passed, no contact
//  changes, small purchase to an established payee. A naive "login from
//  new country" rule fires here; the graph context should exonerate it.
// ================================================================
MATCH (c:Customer {customerId:'CUST-1003'})
MATCH (acc:Account {accountNumber:'ACC-1003'})
MATCH (payee:Account {accountNumber:'ACC-PAYEE-2'})
MATCH (ip:IP {ipAddress:'71.200.30.4'})
MATCH (dev:Device {deviceId:'DEV-1003'})
CREATE (s:Session {sessionId:'SESS-FP-1003', status:'success', createdAt:datetime('2026-07-25T15:00:00Z'),
                   isFraud:false, caseType:'legit-travel', source:'ato-demo'})
CREATE (c)-[:HAS_SESSION]->(s)
MERGE (s)-[:USES_IP]->(ip)
MERGE (s)-[:SESSION_USES_DEVICE]->(dev)
CREATE (ok:Authentication {eventId:'SESS-FP-1003-ok', method:'password', status:'success', mfaUsed:true, mfaResult:'passed', createdAt:datetime('2026-07-25T15:00:00Z'), source:'ato-demo'})
CREATE (s)-[:HAS_AUTHENTICATION]->(ok)
MERGE (c)-[:CONNECTS]->(ok)
CREATE (tx:Transaction {transactionId:'TXN-FP-1003', amount:120.0, currency:'USD', date:datetime('2026-07-25T15:04:00Z'), type:'purchase', message:'Hotel booking', source:'ato-demo'})
CREATE (acc)-[:PERFORMS]->(tx)
CREATE (tx)-[:BENEFITS_TO]->(payee);


// ---------- Done. Quick sanity summary. ----------
MATCH (s:Session {source:'ato-demo'})
RETURN s.caseType AS caseType, s.isFraud AS isFraud, count(*) AS sessions
ORDER BY isFraud DESC, caseType;
