// =====================================================================
//  ASSET PLATFORM — the identity and entitlement graph.
//
//  Two caller classes, which is the structural difference from every other
//  bundle here:
//
//    (:Employee)    internal — research, sales, compliance
//    (:ClientUser)  external — staff of a client organisation, on the platform
//
//  Their entitlements are decided by different rules, so the same query returns
//  different rows for the two classes and neither rule may apply to the other.
//
//  THE ENTITLEMENT LAYER IS EXPLICIT, and that is the point of this file. The
//  business graph records what exists; this records what it entitles anyone to:
//
//    (:Role)-[:SCOPED_TO {validFrom, validTo}]->(:Sector)   research scope
//    (:Employee)-[:COVERS]->(:ClientOrg)                    coverage
//    (:ClientOrg)-[:RESTRICTED_FOR]->(:Desk)                a barrier
//
//  Without those three, no amount of org structure decides anything: a role
//  node with an id says nothing about what the role may read.
//
//  Load this SECOND, after platform.cypher:
//    cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
//                 -f bundles/asset_platform/data/identity.cypher
//
//  Idempotent: everything is tagged source:'ap-identity' and wiped first.
// =====================================================================

CREATE CONSTRAINT ap_emp_email  IF NOT EXISTS FOR (e:Employee)   REQUIRE e.email IS UNIQUE;
CREATE CONSTRAINT ap_cu_email   IF NOT EXISTS FOR (c:ClientUser) REQUIRE c.email IS UNIQUE;
CREATE CONSTRAINT ap_role_name  IF NOT EXISTS FOR (r:Role)       REQUIRE r.name IS UNIQUE;
CREATE INDEX      ap_desk_name  IF NOT EXISTS FOR (d:Desk)       ON (d.name);

MATCH (n {source:'ap-identity'}) DETACH DELETE n;

// ---------- Internal org structure: Desk -> BusinessUnit -> Division ----------
UNWIND [
  {desk:'Equity Research EMEA',      unit:'Global Research',      division:'Securities'},
  {desk:'Institutional Sales EMEA',  unit:'Institutional Client', division:'Securities'},
  {desk:'Institutional Sales APAC',  unit:'Institutional Client', division:'Securities'},
  {desk:'Control Room',              unit:'Compliance',           division:'Legal & Compliance'}
] AS o
MERGE (dv:Division {name:o.division}) ON CREATE SET dv.source = 'ap-identity'
MERGE (bu:BusinessUnit {name:o.unit}) ON CREATE SET bu.source = 'ap-identity'
MERGE (dk:Desk {name:o.desk})         ON CREATE SET dk.source = 'ap-identity'
MERGE (bu)-[:PART_OF]->(dv)
MERGE (dk)-[:PART_OF]->(bu);

UNWIND ['EMEA', 'APAC'] AS r
CREATE (:Region {name:r, source:'ap-identity'});

// ---------- Roles and client roles ----------
// `name` is what identity resolution turns into a principal, so these are the
// strings that appear in an access-control list.
UNWIND [
  {name:'research-energy',   kind:'research', label:'Energy research'},
  {name:'research-tech',     kind:'research', label:'Technology research'},
  {name:'sales-emea',        kind:'sales',    label:'EMEA institutional sales'},
  {name:'sales-apac',        kind:'sales',    label:'APAC institutional sales'},
  {name:'compliance-review', kind:'control',  label:'Supervisory review'},
  {name:'platform-admin',    kind:'control',  label:'Platform administration'}
] AS r
CREATE (:Role {name:r.name, kind:r.kind, label:r.label, source:'ap-identity'});

UNWIND [
  {name:'portfolio-manager', label:'Client portfolio manager'},
  {name:'client-analyst',    label:'Client analyst'}
] AS r
CREATE (:ClientRole {name:r.name, label:r.label, source:'ap-identity'});

// ---------- Internal people ----------
UNWIND [
  {email:'ella.moreau@bank.com',    name:'Ella Moreau',      role:'research-energy',
   desk:'Equity Research EMEA',     region:'EMEA'},
  {email:'raj.patel@bank.com',      name:'Raj Patel',        role:'research-tech',
   desk:'Equity Research EMEA',     region:'EMEA'},
  {email:'oscar.lindgren@bank.com', name:'Oscar Lindgren',   role:'sales-emea',
   desk:'Institutional Sales EMEA', region:'EMEA'},
  {email:'sam.okoye@bank.com',      name:'Sam Okoye',        role:'sales-emea',
   desk:'Institutional Sales EMEA', region:'EMEA'},
  {email:'yuki.tanaka@bank.com',    name:'Yuki Tanaka',      role:'sales-apac',
   desk:'Institutional Sales APAC', region:'APAC'},
  {email:'dana.whitfield@bank.com', name:'Dana Whitfield',   role:'compliance-review',
   desk:'Control Room',             region:'EMEA'}
] AS u
MATCH (r:Role {name:u.role}), (d:Desk {name:u.desk}), (rg:Region {name:u.region})
CREATE (e:Employee {email:u.email, name:u.name, source:'ap-identity'})
CREATE (e)-[:HAS_ROLE]->(r)
CREATE (e)-[:WORKS_FOR]->(d)
CREATE (e)-[:WORKS_IN_REGION]->(rg);

// ---------- Client people ----------
// JOIN POINT: WORKS_FOR reaches a ClientOrg created by platform.cypher.
UNWIND [
  {email:'mia.torres@northwind.com', name:'Mia Torres',  role:'portfolio-manager',
   org:'Northwind Asset Management', region:'EMEA'},
  {email:'liam.becker@kestrel.com',  name:'Liam Becker', role:'client-analyst',
   org:'Kestrel Capital',            region:'EMEA'}
] AS u
MATCH (cr:ClientRole {name:u.role}), (rg:Region {name:u.region})
CREATE (c:ClientUser {email:u.email, name:u.name, source:'ap-identity'})
CREATE (c)-[:HAS_CLIENT_ROLE]->(cr)
CREATE (c)-[:WORKS_IN_REGION]->(rg);
MATCH (c:ClientUser {email:'mia.torres@northwind.com'}), (o:ClientOrg {name:'Northwind Asset Management'})
MERGE (c)-[:WORKS_FOR]->(o);
MATCH (c:ClientUser {email:'liam.becker@kestrel.com'}), (o:ClientOrg {name:'Kestrel Capital'})
MERGE (c)-[:WORKS_FOR]->(o);

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

// ---------- ENTITLEMENT: coverage of a client organisation ----------
UNWIND [
  {who:'oscar.lindgren@bank.com', org:'Northwind Asset Management'},
  {who:'oscar.lindgren@bank.com', org:'Kestrel Capital'},
  {who:'yuki.tanaka@bank.com',    org:'Aster Pension Trust'},
  {who:'sam.okoye@bank.com',      org:'Rivermark Industries'}
] AS c
MATCH (e:Employee {email:c.who}), (o:ClientOrg {name:c.org})
MERGE (e)-[:COVERS]->(o);

// ---------- ENTITLEMENT: a barrier ----------
// Rivermark is restricted for the desk Sam sits on. He genuinely covers it, so
// this WITHDRAWS access he holds rather than failing to grant it.
MATCH (o:ClientOrg {name:'Rivermark Industries'}), (d:Desk {name:'Institutional Sales EMEA'})
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
