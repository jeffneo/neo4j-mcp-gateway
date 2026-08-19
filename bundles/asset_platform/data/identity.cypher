// =====================================================================
//  ASSET PLATFORM — roles, scopes and barriers.
//
//  This file used to hold the whole identity graph. It no longer holds the org
//  hierarchy or coverage, and that division is the point: those two facts have
//  AUTHORITATIVE UPSTREAM PROVIDERS, so they are ingested as projections of the
//  views that own them rather than authored here.
//
//    business_hierarchy view  ->  scripts/ingest_business_hierarchy.py
//                                 (:Employee) with rankLevel, (:OrgUnit) tree,
//                                 IN_UNIT, PART_OF, REPORTS_TO
//    coverage_teams view      ->  scripts/ingest_coverage_teams.py
//                                 (:CoverageTeam), MEMBER_OF, COVERS {window}
//
//  What is left here is what neither view provides, and it is worth naming
//  because it is where the entitlement policy actually lives:
//
//    (:Role)-[:SCOPED_TO {validFrom, validTo}]->(:Sector)   research scope
//    (:ClientOrg)-[:RESTRICTED_FOR]->(:Desk)                a barrier
//    (:Employee)-[:HAS_ROLE]->(:Role)                       role assignment
//    the client-side population, which is not in an HR feed at all
//
//  THREE SOURCES, THREE OWNERS, THREE REFRESH CYCLES. That is the real shape of
//  this problem, and a single seed file hid it. SCOPED_TO and RESTRICTED_FOR
//  exist ONLY to express entitlement — they are the tightest-change-control
//  edges in the graph, and nothing in a business feed should be able to write
//  them. IN_UNIT and COVERS are business facts that rules happen to traverse,
//  which is a weaker and more dangerous position; see docs/entitlement-edges.md.
//
//  LOAD ORDER — each step depends on the one before:
//    1. data/platform.cypher                      business graph
//    2. scripts/ingest_business_hierarchy.py      people and org
//    3. scripts/ingest_coverage_teams.py          coverage
//    4. data/identity.cypher                      THIS FILE
//    5. data/trading.cypher                       trades and compensation
//  scripts/load_asset_platform.sh runs all five in order.
//
//  Idempotent: everything created here is tagged source:'ap-identity' and wiped
//  first. Employees are NOT — they belong to the hierarchy view, and this file
//  only attaches roles to them.
// =====================================================================

CREATE CONSTRAINT ap_cu_email   IF NOT EXISTS FOR (c:ClientUser) REQUIRE c.email IS UNIQUE;
CREATE CONSTRAINT ap_role_name  IF NOT EXISTS FOR (r:Role)       REQUIRE r.name IS UNIQUE;
CREATE INDEX      ap_desk_name  IF NOT EXISTS FOR (d:Desk)       ON (d.name);

MATCH (n {source:'ap-identity'}) DETACH DELETE n;

UNWIND ['EMEA', 'APAC'] AS r
CREATE (:Region {name:r, source:'ap-identity'});

// ---------- Roles and client roles ----------
// `name` is what identity resolution turns into a principal, so these are the
// strings that appear in an access-control list.
UNWIND [
  {name:'research-energy',      kind:'research', label:'Energy research'},
  {name:'research-tech',        kind:'research', label:'Technology research'},
  {name:'sales-emea',           kind:'sales',    label:'EMEA institutional sales'},
  {name:'sales-apac',           kind:'sales',    label:'APAC institutional sales'},
  {name:'markets-trading',      kind:'trading',  label:'Markets trading'},
  {name:'compliance-review',    kind:'control',  label:'Supervisory review'},
  {name:'research-supervisor',  kind:'control',  label:'Research supervision'},
  {name:'sales-supervisor',     kind:'control',  label:'Institutional client supervision'},
  {name:'markets-supervisor',   kind:'control',  label:'Markets supervision'},
  {name:'platform-admin',       kind:'control',  label:'Platform administration'}
] AS r
CREATE (:Role {name:r.name, kind:r.kind, label:r.label, source:'ap-identity'});

UNWIND [
  {name:'portfolio-manager', label:'Client portfolio manager'},
  {name:'client-analyst',    label:'Client analyst'}
] AS r
CREATE (:ClientRole {name:r.name, label:r.label, source:'ap-identity'});

// ---------- Role assignment and region ----------
// The people themselves come from the business_hierarchy view; what they are
// ENTITLED as does not, so it is joined on here by email.
UNWIND [
  {email:'ella.moreau@bank.com',     role:'research-energy',     region:'EMEA'},
  {email:'raj.patel@bank.com',       role:'research-tech',       region:'EMEA'},
  {email:'oscar.lindgren@bank.com',  role:'sales-emea',          region:'EMEA'},
  {email:'sam.okoye@bank.com',       role:'sales-emea',          region:'EMEA'},
  {email:'nina.holt@bank.com',       role:'sales-emea',          region:'EMEA'},
  {email:'yuki.tanaka@bank.com',     role:'sales-apac',          region:'APAC'},
  {email:'dana.whitfield@bank.com',  role:'compliance-review',   region:'EMEA'},
  {email:'priya.raman@bank.com',     role:'research-supervisor', region:'EMEA'},
  {email:'hana.kim@bank.com',        role:'sales-supervisor',    region:'EMEA'},
  {email:'noor.haddad@bank.com',     role:'markets-supervisor',  region:'EMEA'},
  {email:'tomas.vogel@bank.com',     role:'markets-trading',     region:'EMEA'},
  {email:'felipe.souza@bank.com',    role:'markets-trading',     region:'EMEA'},
  {email:'omar.faruq@bank.com',      role:'markets-trading',     region:'EMEA'},
  {email:'ingrid.svensson@bank.com', role:'markets-trading',     region:'EMEA'}
] AS u
MATCH (e:Employee {email:u.email}), (r:Role {name:u.role}), (rg:Region {name:u.region})
MERGE (e)-[:HAS_ROLE]->(r)
MERGE (e)-[:WORKS_IN_REGION]->(rg);

// ---------- Client people ----------
// Not in any HR feed: these are staff of a client organisation, provisioned by
// the platform itself. JOIN POINT: WORKS_FOR reaches a ClientOrg from
// platform.cypher.
UNWIND [
  {email:'mia.torres@northwind.com', name:'Mia Torres',  role:'portfolio-manager',
   org:'Northwind Asset Management', region:'EMEA'},
  {email:'liam.becker@kestrel.com',  name:'Liam Becker', role:'client-analyst',
   org:'Kestrel Capital',            region:'EMEA'}
] AS u
MATCH (cr:ClientRole {name:u.role}), (rg:Region {name:u.region}), (o:ClientOrg {name:u.org})
CREATE (c:ClientUser {email:u.email, name:u.name, source:'ap-identity'})
CREATE (c)-[:HAS_CLIENT_ROLE]->(cr)
CREATE (c)-[:WORKS_IN_REGION]->(rg)
CREATE (c)-[:WORKS_FOR]->(o);

// ---------- ENTITLEMENT: research scope, with a validity window ----------
// This is the fact that makes a Role mean something. Note it is dated: an
// entitlement graph is a statement about a moment in time, and the engine
// evaluates the window at query time rather than materialising it.
MATCH (r:Role {name:'research-energy'}), (s:Sector {name:'Energy'})
MERGE (r)-[:SCOPED_TO {validFrom: date('2026-01-01'), validTo: date('2026-12-31')}]->(s);
MATCH (r:Role {name:'research-tech'}), (s:Sector {name:'Technology'})
MERGE (r)-[:SCOPED_TO {validFrom: date('2026-01-01'), validTo: date('2026-12-31')}]->(s);
// An EXPIRED scope, so the window is demonstrably load-bearing rather than decorative.
MATCH (r:Role {name:'sales-apac'}), (s:Sector {name:'Technology'})
MERGE (r)-[:SCOPED_TO {validFrom: date('2024-01-01'), validTo: date('2024-12-31')}]->(s);

// ---------- ENTITLEMENT: barriers ----------
// Rivermark is restricted for two desks. Sam covers it and Tomas books against
// it, so both restrictions WITHDRAW access those callers genuinely hold rather
// than failing to grant it.
//
// A barrier is the one thing here that must NEVER be derived from a business
// feed. A missing grant edge under-grants and somebody complains; a missing
// barrier edge fails OPEN and nobody notices, because nothing is missing from
// anyone's results. Hence: authored, not ingested.
MATCH (o:ClientOrg {name:'Rivermark Industries'}), (d:Desk {name:'Institutional Sales EMEA'})
MERGE (o)-[:RESTRICTED_FOR]->(d);
MATCH (o:ClientOrg {name:'Rivermark Industries'}), (d:Desk {name:'Equity Derivatives EMEA'})
MERGE (o)-[:RESTRICTED_FOR]->(d);

// ---------- Seams: promote recorded properties to relationships ----------
MATCH (d:Document) WHERE d.authorEmail IS NOT NULL
MATCH (e:Employee {email: d.authorEmail})
MERGE (d)-[:AUTHORED_BY]->(e);

MATCH (i:Interaction) WHERE i.participantEmails IS NOT NULL
UNWIND i.participantEmails AS who
OPTIONAL MATCH (e:Employee {email: who})
OPTIONAL MATCH (c:ClientUser {email: who})
WITH i, coalesce(e, c) AS person WHERE person IS NOT NULL
MERGE (person)-[:PARTICIPATED_IN]->(i);

MATCH (m:Meeting) WHERE m.signupEmails IS NOT NULL
UNWIND m.signupEmails AS who
MATCH (c:ClientUser {email: who})
MERGE (c)-[:SIGNED_UP_FOR]->(m);

MATCH (n {source:'ap-identity'})
RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC, label;
