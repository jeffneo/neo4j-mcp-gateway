// =====================================================================
//  CLIENT PLATFORM SUBGRAPH — the business data.
//
//  A digital platform through which institutional clients consume research,
//  analytics, execution and data/API products. The commercial question is
//  up/cross-sell: which clients should be offered which product next, based on
//  what they use, what comparable clients use, and what coverage has discussed.
//
//  Load this FIRST — it has no dependency on the identity graph. identity.cypher
//  then links people to these clients:
//    cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
//                 -d "$NEO4J_DATABASE" -f bundles/client_platform/data/platform.cypher
//
//  Idempotent: everything is tagged source:'cp-platform' and wiped first.
//
//  WHICH RECORDS ARE PERMISSIONED
//    Interaction, Opportunity, UsageSummary carry `Permissions.Read`.
//    Client, Product, ResearchNote are reference data and flow to everyone —
//    knowing a product catalogue exists is not sensitive; knowing what a
//    particular client pays for, was pitched, or discussed, is.
// =====================================================================

CREATE CONSTRAINT cp_client_name IF NOT EXISTS FOR (c:Client)      REQUIRE c.name IS UNIQUE;
CREATE CONSTRAINT cp_product_code IF NOT EXISTS FOR (p:Product)    REQUIRE p.code IS UNIQUE;
CREATE CONSTRAINT cp_interaction IF NOT EXISTS FOR (i:Interaction) REQUIRE i.interactionId IS UNIQUE;
CREATE CONSTRAINT cp_opportunity IF NOT EXISTS FOR (o:Opportunity) REQUIRE o.opportunityId IS UNIQUE;

MATCH (n {source:'cp-platform'}) DETACH DELETE n;

// ---------- Institutional clients ----------
// `coverageTeam` is the seam: platform data records WHICH team covers a client
// without needing the identity graph to be present. identity.cypher promotes it
// to a relationship when both halves live together.
UNWIND [
  {name:'Northwind Asset Management', segment:'Asset Manager', region:'EMEA', tier:'Platinum', aum:48000000000.0, team:'coverage-emea-am'},
  {name:'Kestrel Capital',            segment:'Hedge Fund',    region:'EMEA', tier:'Gold',     aum:6200000000.0,  team:'coverage-emea-hf'},
  {name:'Rivermark Industries',       segment:'Corporate',     region:'NAMR', tier:'Silver',   aum:0.0,           team:'coverage-namr-corp'},
  {name:'Aster Pension Trust',        segment:'Pension Fund',  region:'EMEA', tier:'Gold',     aum:21000000000.0, team:'coverage-emea-am'},
  {name:'Calder Manufacturing',       segment:'Corporate',     region:'NAMR', tier:'Gold',     aum:0.0,           team:'coverage-namr-corp'},
  {name:'Harbor Point Corp',          segment:'Corporate',     region:'NAMR', tier:'Silver',   aum:0.0,           team:'coverage-namr-corp'}
] AS c
CREATE (:Client {name:c.name, segment:c.segment, region:c.region, tier:c.tier,
                 aum:c.aum, coverageTeam:c.team, source:'cp-platform'});

// ---------- Product catalogue ----------
UNWIND [
  {code:'RES-PORTAL', name:'Research Portal',            family:'Research'},
  {code:'RES-TRADE',  name:'Trade Ideas Feed',           family:'Research'},
  {code:'ANL-EQ',     name:'Portfolio Analytics Equities', family:'Analytics'},
  {code:'ANL-CR',     name:'Portfolio Analytics Credit',   family:'Analytics'},
  {code:'ANL-VIEW',   name:'Market Dashboards',          family:'Analytics'},
  {code:'EXE-ALGO',   name:'Electronic Execution',       family:'Execution'},
  {code:'DAT-API',    name:'Market Data API',            family:'Data'},
  {code:'DAT-RISK',   name:'Risk Model API',             family:'Data'}
] AS p
CREATE (:Product {code:p.code, name:p.name, family:p.family, source:'cp-platform'});

// ---------- Subscriptions: what each client already has ----------
// Northwind is broad; Kestrel is execution-led; Rivermark is thin (the obvious
// cross-sell target); Aster overlaps with Northwind (its nearest peer).
UNWIND [
  {client:'Northwind Asset Management', codes:['RES-PORTAL','ANL-EQ','ANL-VIEW','EXE-ALGO','DAT-API']},
  {client:'Kestrel Capital',            codes:['EXE-ALGO','DAT-API','RES-TRADE']},
  {client:'Rivermark Industries',       codes:['RES-PORTAL']},
  {client:'Aster Pension Trust',        codes:['RES-PORTAL','ANL-EQ','ANL-CR']},
  {client:'Calder Manufacturing',       codes:['RES-PORTAL','ANL-VIEW','DAT-API','EXE-ALGO']},
  {client:'Harbor Point Corp',          codes:['RES-PORTAL','ANL-VIEW','DAT-RISK']}
] AS s
MATCH (c:Client {name:s.client})
UNWIND s.codes AS code
MATCH (p:Product {code:code})
MERGE (c)-[:SUBSCRIBES_TO {since:date('2025-01-15'), status:'active'}]->(p);

// ---------- Usage: engagement per client/product ----------
// Permissioned: usage reveals how much a client is really paying attention.
UNWIND [
  {client:'Northwind Asset Management', code:'ANL-EQ',   sessions:412, apiCalls:0,      trend:'up'},
  {client:'Northwind Asset Management', code:'EXE-ALGO', sessions:88,  apiCalls:154000, trend:'flat'},
  {client:'Northwind Asset Management', code:'RES-PORTAL', sessions:239, apiCalls:0,    trend:'up'},
  {client:'Kestrel Capital',            code:'EXE-ALGO', sessions:140, apiCalls:982000, trend:'up'},
  {client:'Kestrel Capital',            code:'DAT-API',  sessions:12,  apiCalls:640000, trend:'up'},
  {client:'Rivermark Industries',       code:'RES-PORTAL', sessions:19, apiCalls:0,     trend:'down'},
  {client:'Aster Pension Trust',        code:'ANL-CR',   sessions:96,  apiCalls:0,      trend:'flat'},
  {client:'Aster Pension Trust',        code:'ANL-EQ',   sessions:141, apiCalls:0,      trend:'up'},
  {client:'Calder Manufacturing',       code:'ANL-VIEW', sessions:203, apiCalls:0,      trend:'up'},
  {client:'Harbor Point Corp',          code:'ANL-VIEW', sessions:167, apiCalls:0,      trend:'flat'}
] AS u
MATCH (c:Client {name:u.client}), (p:Product {code:u.code})
CREATE (usage:UsageSummary {
  usageId: 'USE-' + u.code + '-' + toString(id(c)),
  period:'2026-Q2', sessions:u.sessions, apiCalls:u.apiCalls, trend:u.trend,
  source:'cp-platform'
})
SET usage.`Permissions.Read` = [c.coverageTeam, 'product-' + toLower(p.family), 'platform-admin', 'compliance-review']
CREATE (usage)-[:FOR_CLIENT]->(c)
CREATE (usage)-[:FOR_PRODUCT]->(p);

// ---------- Interactions: meetings and calls with the client ----------
// Permissioned to the coverage team plus supervision. The person who logged it
// also sees it, which is a PATH rather than a list entry — see bundle.yaml.
UNWIND [
  {id:'INT-2001', client:'Northwind Asset Management', by:'lena.fischer@bank.com',
   kind:'meeting', subject:'Q2 platform review and analytics roadmap'},
  {id:'INT-2002', client:'Northwind Asset Management', by:'marc.dubois@bank.com',
   kind:'call',    subject:'Credit analytics trial request'},
  {id:'INT-2003', client:'Kestrel Capital',            by:'sofia.rossi@bank.com',
   kind:'meeting', subject:'Execution latency review'},
  {id:'INT-2004', client:'Rivermark Industries',       by:'evan.brooks@bank.com',
   kind:'call',    subject:'Treasury reporting requirements'},
  {id:'INT-2005', client:'Aster Pension Trust',        by:'lena.fischer@bank.com',
   kind:'meeting', subject:'Dashboards walkthrough'},
  // Logged by an analytics specialist who does NOT cover this client: the ACL
  // below reaches only the coverage team and supervision, so her access comes
  // solely from the LOGGED relationship — a grant the list model cannot express.
  {id:'INT-2006', client:'Kestrel Capital',            by:'nadia.haddad@bank.com',
   kind:'meeting', subject:'Analytics onboarding session'}
] AS i
MATCH (c:Client {name:i.client})
CREATE (int:Interaction {
  interactionId:i.id, kind:i.kind, subject:i.subject,
  occurredAt: datetime('2026-05-12T10:00:00Z'), source:'cp-platform'
})
SET int.`Permissions.Read` = [c.coverageTeam, 'compliance-review']
CREATE (int)-[:WITH_CLIENT]->(c)
SET int.loggedByEmail = i.by;   // promoted to a (:User)-[:LOGGED]-> edge by identity.cypher

// ---------- Opportunities: the cross-sell pipeline ----------
// Commercially sensitive — pricing and stage. Coverage team, the relevant product
// specialists, platform admin and supervision.
UNWIND [
  {id:'OPP-3001', client:'Rivermark Industries',       code:'ANL-VIEW', stage:'Qualified',
   value:120000.0, rationale:'Peers in segment use dashboards; engagement is declining'},
  {id:'OPP-3002', client:'Northwind Asset Management', code:'ANL-CR',   stage:'Proposal',
   value:340000.0, rationale:'Requested a credit analytics trial on a coverage call'},
  {id:'OPP-3003', client:'Kestrel Capital',            code:'ANL-EQ',   stage:'Discovery',
   value:180000.0, rationale:'Heavy execution and data usage without analytics'},
  {id:'OPP-3004', client:'Aster Pension Trust',        code:'EXE-ALGO', stage:'Qualified',
   value:260000.0, rationale:'Nearest peer executes electronically; this client does not'}
] AS o
MATCH (c:Client {name:o.client}), (p:Product {code:o.code})
CREATE (opp:Opportunity {
  opportunityId:o.id, stage:o.stage, value:o.value, currency:'USD',
  rationale:o.rationale, source:'cp-platform'
})
SET opp.`Permissions.Read` = [c.coverageTeam, 'product-' + toLower(p.family), 'platform-admin', 'compliance-review']
CREATE (opp)-[:FOR_CLIENT]->(c)
CREATE (opp)-[:FOR_PRODUCT]->(p);

// ---------- Published research: the firm-wide baseline ----------
UNWIND [
  {id:'NOTE-9001', title:'EMEA equity flows: quarterly review'},
  {id:'NOTE-9002', title:'Credit spreads and issuance outlook'}
] AS n
CREATE (note:ResearchNote {noteId:n.id, title:n.title,
                           publishedAt: datetime('2026-06-01T06:00:00Z'), source:'cp-platform'})
SET note.`Permissions.Read` = ['everyone'];

MATCH (n {source:'cp-platform'})
RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC, label;
