// =====================================================================
//  IAM demo dataset (small, illustrative starting point — expand freely).
//  Idempotent: every node carries source:'iam-demo' and is wiped first.
//
//  Load:
//    cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
//                 -d "$NEO4J_DATABASE" -f bundles/iam/data/iam_demo.cypher
//
//  Planted patterns (ground truth for the exercises):
//    - U-1001 Alice : segregation-of-duty violation (initiate + approve payments)
//    - U-1002 Bob   : privileged (Administrator -> admin on a CRITICAL resource)
//    - U-1003 Carol : disabled account that still holds a privileged role
// =====================================================================

CREATE CONSTRAINT iam_user IF NOT EXISTS FOR (u:User) REQUIRE u.userId IS UNIQUE;
CREATE CONSTRAINT iam_role IF NOT EXISTS FOR (r:Role) REQUIRE r.name IS UNIQUE;
CREATE CONSTRAINT iam_res  IF NOT EXISTS FOR (x:Resource) REQUIRE x.name IS UNIQUE;

MATCH (n {source:'iam-demo'}) DETACH DELETE n;

// Resources (with sensitivity)
UNWIND [
  {name:'PaymentsSystem', sens:'high'},
  {name:'Reports',        sens:'low'},
  {name:'IAMConsole',     sens:'critical'},
  {name:'HRData',         sens:'high'}
] AS r
CREATE (:Resource {name:r.name, sensitivity:r.sens, source:'iam-demo'});

// Permissions -> Resource
UNWIND [
  {action:'create_payment',  res:'PaymentsSystem'},
  {action:'approve_payment', res:'PaymentsSystem'},
  {action:'read_reports',    res:'Reports'},
  {action:'admin',           res:'IAMConsole'},
  {action:'read_hr',         res:'HRData'}
] AS p
MATCH (res:Resource {name:p.res})
CREATE (perm:Permission {action:p.action, source:'iam-demo'})-[:ON]->(res);

// Roles (privileged flag) -> Permission
UNWIND [
  {role:'PaymentInitiator', priv:false, action:'create_payment'},
  {role:'PaymentApprover',  priv:false, action:'approve_payment'},
  {role:'Analyst',          priv:false, action:'read_reports'},
  {role:'Administrator',    priv:true,  action:'admin'},
  {role:'HRViewer',         priv:false, action:'read_hr'}
] AS r
MERGE (role:Role {name:r.role}) ON CREATE SET role.privileged=r.priv, role.source='iam-demo'
WITH role, r
MATCH (perm:Permission {action:r.action})
MERGE (role)-[:ALLOWS]->(perm);

// Segregation-of-duty conflict: initiating and approving payments
MATCH (a:Role {name:'PaymentInitiator'}), (b:Role {name:'PaymentApprover'})
MERGE (a)-[:CONFLICTS_WITH]->(b);

// Groups -> Roles
UNWIND [
  {grp:'Finance',  role:'PaymentInitiator'},
  {grp:'Finance',  role:'Analyst'},
  {grp:'Platform', role:'Administrator'}
] AS g
MERGE (grp:Group {name:g.grp}) ON CREATE SET grp.source='iam-demo'
WITH grp, g
MATCH (role:Role {name:g.role})
MERGE (grp)-[:GRANTS]->(role);

// Users, group memberships, and direct role grants
MERGE (alice:User {userId:'U-1001'}) ON CREATE SET alice.name='Alice', alice.status='active', alice.source='iam-demo';
MERGE (bob:User   {userId:'U-1002'}) ON CREATE SET bob.name='Bob',     bob.status='active',   bob.source='iam-demo';
MERGE (carol:User {userId:'U-1003'}) ON CREATE SET carol.name='Carol', carol.status='disabled', carol.source='iam-demo';
MERGE (dan:User   {userId:'U-1004'}) ON CREATE SET dan.name='Dan',     dan.status='active',   dan.source='iam-demo';

MATCH (alice:User {userId:'U-1001'}), (fin:Group {name:'Finance'}), (appr:Role {name:'PaymentApprover'})
MERGE (alice)-[:MEMBER_OF]->(fin)
MERGE (alice)-[:HAS_ROLE]->(appr);          // + PaymentInitiator via Finance => SoD

MATCH (bob:User {userId:'U-1002'}), (plat:Group {name:'Platform'})
MERGE (bob)-[:MEMBER_OF]->(plat);           // Administrator via Platform

MATCH (carol:User {userId:'U-1003'}), (init:Role {name:'PaymentInitiator'})
MERGE (carol)-[:HAS_ROLE]->(init);          // disabled but still entitled

MATCH (dan:User {userId:'U-1004'}), (fin:Group {name:'Finance'})
MERGE (dan)-[:MEMBER_OF]->(fin);            // baseline: initiator + analyst, no conflict

// Quick summary
MATCH (n {source:'iam-demo'})
RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC;
