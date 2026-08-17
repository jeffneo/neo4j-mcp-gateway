// =====================================================================
//  IAM demo dataset — query-mediation model (matches the IAM MCP fork).
//  Idempotent: every node carries source:'iam-demo' and is wiped first.
//
//  Load:
//    cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
//                 -d "$NEO4J_DATABASE" -f bundles/iam/data/iam_demo.cypher
//
//  Shape:
//    (:User {email, name, AdGroupList:[...]})-[:MEMBER_OF]->(:AdGroup {name})
//    (:Service)-[:EXECUTES]->(:Job { `Permissions.Read`:[...], ... })
//
//  Permissions are list-valued props on domain nodes. A user's effective
//  principals = their identity + 'everyone' + AdGroupList + MEMBER_OF groups.
//
//  Expected readable job counts via secure-read-cypher (Permissions.Read):
//    johnny.kinnaird@neo4j.com -> 4   (everyone, group1, group2)
//    michael.moore@neo4j.com   -> 3   (everyone, group2)
//    sarah.lee@neo4j.com       -> 1   (everyone only)
//  admin-secrets (Permissions.Read=['admins']) is visible to none of them.
// =====================================================================

CREATE CONSTRAINT iam_user_email IF NOT EXISTS FOR (u:User) REQUIRE u.email IS UNIQUE;
CREATE CONSTRAINT iam_job_name  IF NOT EXISTS FOR (j:Job)  REQUIRE j.name  IS UNIQUE;

MATCH (n {source:'iam-demo'}) DETACH DELETE n;

// AD groups (identity metadata only)
UNWIND ['group1', 'group2'] AS gname
CREATE (:AdGroup {name: gname, source: 'iam-demo'});

// Users: direct AdGroupList + MEMBER_OF edges to AdGroup nodes
UNWIND [
  {email:'johnny.kinnaird@neo4j.com', name:'Johnny Kinnaird', ad:['everyone','group1','group2'], memberOf:['group1','group2']},
  {email:'michael.moore@neo4j.com',   name:'Michael Moore',   ad:['everyone','group2'],          memberOf:['group2']},
  {email:'sarah.lee@neo4j.com',       name:'Sarah Lee',       ad:['everyone'],                    memberOf:[]}
] AS u
CREATE (user:User {email:u.email, name:u.name, AdGroupList:u.ad, source:'iam-demo'})
WITH user, u
UNWIND u.memberOf AS gname
MATCH (g:AdGroup {name: gname})
MERGE (user)-[:MEMBER_OF]->(g);

// Jobs with list-valued CRUD permissions, each executed by a scheduler service
UNWIND [
  {name:'nightly-etl',    read:['everyone']},
  {name:'payments-recon', read:['group1']},
  {name:'hr-export',      read:['group2']},
  {name:'security-audit', read:['group1','group2']},
  {name:'admin-secrets',  read:['admins']}
] AS j
MERGE (scheduler:Service {name:'scheduler'}) ON CREATE SET scheduler.source = 'iam-demo'
CREATE (job:Job {name: j.name, source: 'iam-demo'})
SET job.`Permissions.Read`   = j.read,
    job.`Permissions.Create` = ['group1'],
    job.`Permissions.Update` = ['group1'],
    job.`Permissions.Delete` = ['group1']
CREATE (scheduler)-[:EXECUTES]->(job);

// Summary
MATCH (n {source:'iam-demo'})
RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC;
