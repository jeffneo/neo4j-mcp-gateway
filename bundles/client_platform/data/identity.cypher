// =====================================================================
//  IDENTITY SUBGRAPH — who works here and what they cover.
//
//  Deliberately a SEPARATE file from platform.cypher so the two halves can be
//  loaded together or apart. That is the point of the tutorial: the identity
//  graph and the business graph are different assets with different owners,
//  different refresh cycles and different sensitivity.
//
//  Labels here: User, AdGroup, Desk.
//  Labels in platform.cypher: Client, Product, Interaction, Opportunity, Usage.
//  They meet at exactly two join points, both created HERE so the seam is
//  visible in one place:
//      (:User)-[:COVERS]->(:Client)              individual coverage
//      (:Client)-[:COVERED_BY]->(:AdGroup)       team coverage
//
//  Load this SECOND, after platform.cypher:
//    cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
//                 -d "$NEO4J_DATABASE" -f bundles/client_platform/data/identity.cypher
//
//  Idempotent: everything is tagged source:'cp-identity' and wiped first.
// =====================================================================

CREATE CONSTRAINT cp_user_email  IF NOT EXISTS FOR (u:User)    REQUIRE u.email IS UNIQUE;
CREATE CONSTRAINT cp_group_name  IF NOT EXISTS FOR (g:AdGroup) REQUIRE g.name  IS UNIQUE;

MATCH (n {source:'cp-identity'}) DETACH DELETE n;

// ---------- Entitlement groups ----------
// Three kinds, because they behave differently under mediation:
//   coverage-*  relationship-derived — expressible as a PATH to the client
//   product-*   role-based           — a specialist sees their product everywhere
//   control     role-based           — supervision and platform administration
UNWIND [
  {name:'coverage-emea-am',    kind:'coverage', label:'EMEA Asset Managers'},
  {name:'coverage-emea-hf',    kind:'coverage', label:'EMEA Hedge Funds'},
  {name:'coverage-namr-corp',  kind:'coverage', label:'North America Corporates'},
  {name:'product-research',    kind:'product',  label:'Research product specialists'},
  {name:'product-analytics',   kind:'product',  label:'Analytics product specialists'},
  {name:'product-execution',   kind:'product',  label:'Execution product specialists'},
  {name:'platform-admin',      kind:'control',  label:'Platform administration'},
  {name:'compliance-review',   kind:'control',  label:'Supervisory review'}
] AS g
CREATE (:AdGroup {name:g.name, kind:g.kind, label:g.label, source:'cp-identity'});

// ---------- Desks ----------
UNWIND ['Equities EMEA', 'Credit EMEA', 'Macro NAMR'] AS d
CREATE (:Desk {name:d, source:'cp-identity'});

// ---------- People ----------
// AdGroupList mirrors MEMBER_OF; both are honoured by identity resolution, so a
// deployment can feed one, the other, or both.
UNWIND [
  {email:'lena.fischer@bank.com',  name:'Lena Fischer',  role:'Client Coverage',
   desk:'Equities EMEA', groups:['coverage-emea-am']},
  {email:'marc.dubois@bank.com',   name:'Marc Dubois',   role:'Client Coverage',
   desk:'Equities EMEA', groups:['coverage-emea-am']},
  {email:'sofia.rossi@bank.com',   name:'Sofia Rossi',   role:'Client Coverage',
   desk:'Credit EMEA',   groups:['coverage-emea-hf']},
  {email:'evan.brooks@bank.com',   name:'Evan Brooks',   role:'Client Coverage',
   desk:'Macro NAMR',    groups:['coverage-namr-corp']},
  {email:'nadia.haddad@bank.com',  name:'Nadia Haddad',  role:'Product Specialist',
   desk:'Equities EMEA', groups:['product-analytics']},
  {email:'tomas.silva@bank.com',   name:'Tomas Silva',   role:'Product Specialist',
   desk:'Equities EMEA', groups:['product-execution']},
  {email:'grace.okonjo@bank.com',  name:'Grace Okonjo',  role:'Platform Admin',
   desk:'Equities EMEA', groups:['platform-admin']},
  {email:'peter.lindqvist@bank.com', name:'Peter Lindqvist', role:'Compliance',
   desk:'Credit EMEA',   groups:['compliance-review']}
] AS u
CREATE (user:User {
  email:u.email, name:u.name, role:u.role, desk:u.desk,
  AdGroupList: ['everyone'] + u.groups,
  source:'cp-identity'
})
WITH user, u
MATCH (d:Desk {name:u.desk}) MERGE (user)-[:ON_DESK]->(d)
WITH user, u
UNWIND u.groups AS gname
MATCH (g:AdGroup {name:gname})
MERGE (user)-[:MEMBER_OF]->(g);

// ---------- JOIN POINT 1: team coverage ----------
// Promotes Client.coverageTeam (written by platform.cypher) into a relationship.
// If the platform data lives in a DIFFERENT database there are no Client nodes
// here, so this matches nothing and is silently skipped — which is exactly the
// separated topology described in docs/entitlement-testing-tutorial.md.
MATCH (c:Client) WHERE c.coverageTeam IS NOT NULL
MATCH (g:AdGroup {name: c.coverageTeam})
MERGE (c)-[:COVERED_BY]->(g);

// ---------- JOIN POINT 3: who logged an interaction ----------
// A relationship-derived grant: the person who logged it can read it.
MATCH (i:Interaction) WHERE i.loggedByEmail IS NOT NULL
MATCH (u:User {email: i.loggedByEmail})
MERGE (u)-[:LOGGED]->(i);

// ---------- JOIN POINT 2: named individual coverage ----------
UNWIND [
  {user:'lena.fischer@bank.com', client:'Northwind Asset Management'},
  {user:'lena.fischer@bank.com', client:'Aster Pension Trust'},
  {user:'marc.dubois@bank.com',  client:'Northwind Asset Management'},
  {user:'sofia.rossi@bank.com',  client:'Kestrel Capital'},
  {user:'evan.brooks@bank.com',  client:'Rivermark Industries'},
  {user:'evan.brooks@bank.com',  client:'Calder Manufacturing'},
  {user:'evan.brooks@bank.com',  client:'Harbor Point Corp'}
] AS a
MATCH (u:User {email:a.user}), (c:Client {name:a.client})
MERGE (u)-[:COVERS]->(c);

MATCH (n {source:'cp-identity'})
RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC, label;
