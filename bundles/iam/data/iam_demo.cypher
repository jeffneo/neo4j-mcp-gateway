// =====================================================================
//  IAM demo dataset — tier-1 investment bank ENTITLEMENTS (read authorization).
//  Query-mediation model: permissions are list-valued properties on domain
//  nodes; secure-read-cypher filters rows against the caller's principals.
//
//  Load:
//    cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
//                 -d "$NEO4J_DATABASE" -f bundles/iam/data/iam_demo.cypher
//
//  Idempotent: every node carries source:'iam-demo' and is wiped first.
//
//  Shape:
//    (:User {email, name, desk, region, AdGroupList:[...]})-[:MEMBER_OF]->(:AdGroup)
//    (:User)-[:COVERS]->(:Client)                       coverage assignment
//    (:User)-[:PARTICIPANT_IN]->(:Communication)        who was in the room
//    (:Communication)-[:WITH_CLIENT]->(:Client)
//    (:Client)-[:SUBMITTED]->(:Request)                 RFQs / client requests
//    (:User)-[:BOOKED]->(:Trade)-[:FOR_CLIENT]->(:Client)
//    (:Trade)-[:BOOKED_ON]->(:Desk)
//    (:Deal)                                            wall-crossed, need-to-know
//
//  Entitlement principals used in `Permissions.Read`:
//    everyone                 firm-wide
//    sales-all                all sales staff
//    coverage-acme / -zenith  the coverage team for a specific client
//    desk-rates-emea          the desk that owns the risk
//    ops-settlements          middle office / settlements
//    compliance-supervision   supervisory review (comms, requests, trades)
//    control-room             information-barrier control room (private side)
//    deal-atlas               named deal team (need-to-know)
//    <user email>             an individual, e.g. a private 1:1 chat participant
//
//  DEMO SCENARIOS (see bundles/iam/README.md for the exact tool calls)
//    1. Private client chat  — only the participating salesperson (+ supervision)
//       can read COMM-1001. Joe and Sam cover the same client but CANNOT see it.
//    2. Shared coverage      — Joe and Sam both cover Acme, so both see ALL Acme
//       requests (coverage-acme), identical result sets.
//    3. Booked trade         — Anna books TRD-3001 for client Acme/George. Visible
//       to the booker, the coverage team, the owning desk, settlements ops, and
//       supervision — not to unrelated coverage (Priya) or the private side (David).
//    4. Information barrier  — Project Atlas is readable only by the named deal
//       team + control room. Even compliance supervision cannot see it.
// =====================================================================

CREATE CONSTRAINT iam_user_email  IF NOT EXISTS FOR (u:User)          REQUIRE u.email IS UNIQUE;
CREATE CONSTRAINT iam_group_name  IF NOT EXISTS FOR (g:AdGroup)       REQUIRE g.name  IS UNIQUE;
CREATE CONSTRAINT iam_client_name IF NOT EXISTS FOR (c:Client)        REQUIRE c.name  IS UNIQUE;
CREATE CONSTRAINT iam_comm_id     IF NOT EXISTS FOR (c:Communication) REQUIRE c.commId IS UNIQUE;
CREATE CONSTRAINT iam_request_id  IF NOT EXISTS FOR (r:Request)       REQUIRE r.requestId IS UNIQUE;
CREATE CONSTRAINT iam_trade_id    IF NOT EXISTS FOR (t:Trade)         REQUIRE t.tradeId IS UNIQUE;

MATCH (n {source:'iam-demo'}) DETACH DELETE n;

// ---------- Entitlement groups ----------
UNWIND [
  {name:'sales-all',              kind:'function'},
  {name:'coverage-acme',          kind:'coverage'},
  {name:'coverage-zenith',        kind:'coverage'},
  {name:'desk-rates-emea',        kind:'desk'},
  {name:'desk-fx-emea',           kind:'desk'},
  {name:'ops-settlements',        kind:'operations'},
  {name:'compliance-supervision', kind:'control'},
  {name:'control-room',           kind:'control'},
  {name:'deal-atlas',             kind:'need-to-know'}
] AS g
CREATE (:AdGroup {name:g.name, kind:g.kind, source:'iam-demo'});

// ---------- Desks ----------
UNWIND [
  {name:'Rates EMEA', code:'desk-rates-emea'},
  {name:'FX EMEA',    code:'desk-fx-emea'}
] AS d
CREATE (:Desk {name:d.name, code:d.code, source:'iam-demo'});

// ---------- Clients + client contacts ----------
CREATE (:Client {name:'Acme Corp',        lei:'LEI-ACME-001',   sector:'Industrials', source:'iam-demo'});
CREATE (:Client {name:'Zenith Industries', lei:'LEI-ZENITH-002', sector:'Energy',      source:'iam-demo'});

CREATE (:ClientContact {name:'George Wu',   email:'george.wu@acmecorp.com',      title:'Treasurer',       source:'iam-demo'});
CREATE (:ClientContact {name:'Hannah Blum', email:'hannah.blum@zenithind.com',   title:'Head of Funding', source:'iam-demo'});

MATCH (cc:ClientContact {email:'george.wu@acmecorp.com'}),   (c:Client {name:'Acme Corp'})        MERGE (cc)-[:WORKS_FOR]->(c);
MATCH (cc:ClientContact {email:'hannah.blum@zenithind.com'}), (c:Client {name:'Zenith Industries'}) MERGE (cc)-[:WORKS_FOR]->(c);

// ---------- Bank users (AdGroupList mirrors MEMBER_OF; both are honoured) ----------
UNWIND [
  {email:'anna.ross@bank.com',        name:'Anna Ross',        role:'Sales',      desk:'Rates EMEA', region:'EMEA',
   groups:['sales-all','coverage-acme','desk-rates-emea']},
  {email:'joe.hart@bank.com',         name:'Joe Hart',         role:'Sales',      desk:'Rates EMEA', region:'EMEA',
   groups:['sales-all','coverage-acme']},
  {email:'sam.diaz@bank.com',         name:'Sam Diaz',         role:'Sales',      desk:'Rates EMEA', region:'EMEA',
   groups:['sales-all','coverage-acme']},
  {email:'priya.natarajan@bank.com',  name:'Priya Natarajan',  role:'Sales',      desk:'FX EMEA',    region:'EMEA',
   groups:['sales-all','coverage-zenith']},
  {email:'tom.becker@bank.com',       name:'Tom Becker',       role:'Trader',     desk:'Rates EMEA', region:'EMEA',
   groups:['desk-rates-emea']},
  {email:'olu.adeyemi@bank.com',      name:'Olu Adeyemi',      role:'Operations', desk:'Settlements',region:'EMEA',
   groups:['ops-settlements']},
  {email:'maria.chen@bank.com',       name:'Maria Chen',       role:'Compliance', desk:'Supervision',region:'Global',
   groups:['compliance-supervision']},
  {email:'david.okafor@bank.com',     name:'David Okafor',     role:'Banker',     desk:'IBD',        region:'EMEA',
   groups:['control-room','deal-atlas']}
] AS u
CREATE (user:User {
  email:u.email, name:u.name, role:u.role, desk:u.desk, region:u.region,
  AdGroupList: ['everyone'] + u.groups,
  source:'iam-demo'
})
WITH user, u
UNWIND u.groups AS gname
MATCH (g:AdGroup {name:gname})
MERGE (user)-[:MEMBER_OF]->(g);

// ---------- Coverage assignments ----------
UNWIND [
  {user:'anna.ross@bank.com',       client:'Acme Corp'},
  {user:'joe.hart@bank.com',        client:'Acme Corp'},
  {user:'sam.diaz@bank.com',        client:'Acme Corp'},
  {user:'priya.natarajan@bank.com', client:'Zenith Industries'}
] AS cov
MATCH (u:User {email:cov.user}), (c:Client {name:cov.client})
MERGE (u)-[:COVERS]->(c);

// =====================================================================
//  SCENARIO 1 — client communications
//  A private 1:1 chat is entitled to its PARTICIPANTS only (+ supervision).
//  A shared desk email thread is entitled to the whole coverage team.
// =====================================================================
UNWIND [
  {commId:'COMM-1001', channel:'chat',  subject:'Pre-hedge colour ahead of 5y issuance',
   client:'Acme Corp',         contact:'george.wu@acmecorp.com',    participants:['anna.ross@bank.com'],
   read:['anna.ross@bank.com','compliance-supervision']},
  {commId:'COMM-1002', channel:'email', subject:'Acme — weekly rates market update',
   client:'Acme Corp',         contact:'george.wu@acmecorp.com',    participants:['anna.ross@bank.com','joe.hart@bank.com','sam.diaz@bank.com'],
   read:['coverage-acme','compliance-supervision']},
  {commId:'COMM-1003', channel:'chat',  subject:'Zenith FX hedging discussion',
   client:'Zenith Industries', contact:'hannah.blum@zenithind.com', participants:['priya.natarajan@bank.com'],
   read:['priya.natarajan@bank.com','compliance-supervision']}
] AS m
MATCH (cl:Client {name:m.client})
CREATE (c:Communication {
  commId:m.commId, channel:m.channel, subject:m.subject,
  sentAt: datetime('2026-07-15T09:30:00Z'), source:'iam-demo'
})
SET c.`Permissions.Read` = m.read
CREATE (c)-[:WITH_CLIENT]->(cl)
WITH c, m
MATCH (cc:ClientContact {email:m.contact}) MERGE (c)-[:WITH_CONTACT]->(cc)
WITH c, m
UNWIND m.participants AS p
MATCH (u:User {email:p})
MERGE (u)-[:PARTICIPANT_IN]->(c);

// =====================================================================
//  SCENARIO 2 — client requests (RFQs)
//  Entitled to the client's coverage team, the pricing desk, and supervision,
//  so every salesperson covering that client sees the same request set.
// =====================================================================
UNWIND [
  {requestId:'REQ-2001', client:'Acme Corp',         product:'5y GBP IRS',   notional:75000000.0,
   read:['coverage-acme','desk-rates-emea','compliance-supervision']},
  {requestId:'REQ-2002', client:'Acme Corp',         product:'EUR/GBP fwd',  notional:20000000.0,
   read:['coverage-acme','desk-rates-emea','compliance-supervision']},
  {requestId:'REQ-2003', client:'Zenith Industries', product:'3y USD IRS',   notional:40000000.0,
   read:['coverage-zenith','compliance-supervision']}
] AS r
MATCH (cl:Client {name:r.client})
CREATE (req:Request {
  requestId:r.requestId, product:r.product, notional:r.notional, currency:'GBP',
  receivedAt: datetime('2026-07-16T10:05:00Z'), status:'quoted', source:'iam-demo'
})
SET req.`Permissions.Read` = r.read
CREATE (cl)-[:SUBMITTED]->(req);

// =====================================================================
//  SCENARIO 3 — booked trades
//  Anna books TRD-3001 on behalf of Acme (George Wu). Entitlement spans the
//  booking salesperson, the coverage team, the owning desk, settlements, and
//  supervision — the realistic need-to-know set for a client trade.
// =====================================================================
UNWIND [
  {tradeId:'TRD-3001', client:'Acme Corp',         contact:'george.wu@acmecorp.com',    booker:'anna.ross@bank.com',
   desk:'desk-rates-emea', product:'5y GBP IRS', notional:75000000.0,
   read:['anna.ross@bank.com','coverage-acme','desk-rates-emea','ops-settlements','compliance-supervision']},
  {tradeId:'TRD-3002', client:'Zenith Industries', contact:'hannah.blum@zenithind.com', booker:'priya.natarajan@bank.com',
   desk:'desk-fx-emea',    product:'USD/EUR fwd', notional:40000000.0,
   read:['priya.natarajan@bank.com','coverage-zenith','desk-fx-emea','ops-settlements','compliance-supervision']}
] AS t
MATCH (cl:Client {name:t.client}), (u:User {email:t.booker}), (d:Desk {code:t.desk})
CREATE (tr:Trade {
  tradeId:t.tradeId, product:t.product, notional:t.notional, currency:'GBP',
  bookedAt: datetime('2026-07-16T14:20:00Z'), status:'booked', source:'iam-demo'
})
SET tr.`Permissions.Read` = t.read
CREATE (u)-[:BOOKED]->(tr)
CREATE (tr)-[:FOR_CLIENT]->(cl)
CREATE (tr)-[:BOOKED_ON]->(d)
WITH tr, t
MATCH (cc:ClientContact {email:t.contact}) MERGE (tr)-[:ON_BEHALF_OF]->(cc);

// =====================================================================
//  SCENARIO 4 — information barrier (wall-crossed deal, need-to-know only)
// =====================================================================
CREATE (deal:Deal {
  dealId:'DEAL-4001', codename:'Project Atlas', stage:'wall-crossed',
  openedAt: datetime('2026-06-02T08:00:00Z'), source:'iam-demo'
})
SET deal.`Permissions.Read` = ['deal-atlas','control-room'];

MATCH (deal:Deal {dealId:'DEAL-4001'}), (cl:Client {name:'Acme Corp'})
MERGE (deal)-[:INVOLVES_CLIENT]->(cl);

// ---------- Firm-wide published research (the 'everyone' baseline) ----------
CREATE (n:ResearchNote {noteId:'RES-5001', title:'EMEA rates daily commentary',
                        publishedAt: datetime('2026-07-16T06:45:00Z'), source:'iam-demo'})
SET n.`Permissions.Read` = ['everyone'];

// ---------- Summary ----------
MATCH (n {source:'iam-demo'})
RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC, label;
