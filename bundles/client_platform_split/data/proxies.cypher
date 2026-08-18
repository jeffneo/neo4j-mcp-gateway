// =====================================================================
//  PROXY NODES for the data constituent.
//
//  A relationship never spans two graphs, but a NODE can exist in both. That is
//  Neo4j's documented "proxy node" pattern, and it is what lets a path grant be
//  evaluated when identity lives in a different database: the traversal is cut
//  at a node present on both sides, and the identifier crosses as a value.
//
//  Load order, into the DATA constituent:
//    1. platform.cypher   (from ../../client_platform/data/)
//    2. this file
//  and load identity.cypher into the IDENTITY constituent.
//
//  WHAT GOES WHERE, and why this division is defensible:
//
//    identity constituent          data constituent
//    --------------------          ----------------
//    (:User) full attributes       (:User {email})      <- proxy, identifier only
//    (:AdGroup) full attributes    (:AdGroup {name})    <- proxy, identifier only
//    (:User)-[:MEMBER_OF]->(:AdGroup)
//                                  (:Client)-[:COVERED_BY]->(:AdGroup)
//                                  (:User)-[:LOGGED]->(:Interaction)
//
//  Membership stays in identity — it is the high-churn half, the part that
//  changes when someone moves desks. What replicates here is coverage and
//  authorship, which are facts ABOUT the business records: "which team covers
//  this client" and "who logged this interaction" belong with the client and the
//  interaction.
//
//  Both edges below are derived from properties platform.cypher already wrote,
//  so this file needs nothing from the identity graph.
//
//  Idempotent: everything is tagged source:'cp-proxy' and wiped first.
// =====================================================================

MATCH (n {source:'cp-proxy'}) DETACH DELETE n;

// ---------- AdGroup proxies: name only ----------
// The cut point for group-routed grants. The prelude resolves the caller to a
// list of group names in the identity constituent; the grant then re-roots here.
MATCH (c:Client) WHERE c.coverageTeam IS NOT NULL
WITH collect(DISTINCT c.coverageTeam) AS teams
UNWIND teams + ['product-research','product-analytics','product-execution',
                'platform-admin','compliance-review'] AS name
MERGE (g:AdGroup {name:name})
ON CREATE SET g.source = 'cp-proxy';

// ---------- User proxies: email only ----------
// The cut point for caller-direct grants such as authorship. No attributes, no
// memberships — just enough to be matched by principalId.
MATCH (i:Interaction) WHERE i.loggedByEmail IS NOT NULL
WITH collect(DISTINCT i.loggedByEmail) AS emails
UNWIND emails AS email
MERGE (u:User {email:email})
ON CREATE SET u.source = 'cp-proxy';

// ---------- Coverage: a fact about the client ----------
MATCH (c:Client) WHERE c.coverageTeam IS NOT NULL
MATCH (g:AdGroup {name: c.coverageTeam})
MERGE (c)-[:COVERED_BY]->(g);

// ---------- Authorship: a fact about the interaction ----------
MATCH (i:Interaction) WHERE i.loggedByEmail IS NOT NULL
MATCH (u:User {email: i.loggedByEmail})
MERGE (u)-[:LOGGED]->(i);

MATCH (n {source:'cp-proxy'})
RETURN labels(n)[0] AS label, count(*) AS proxies ORDER BY label;
