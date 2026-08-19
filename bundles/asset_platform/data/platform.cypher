// =====================================================================
//  ASSET PLATFORM — the business graph.
//
//  An institutional platform where research, meetings and client interactions
//  are all ABOUT assets, and assets are classified under a single sector
//  taxonomy. Entitlement to content follows from three different kinds of fact:
//  which sectors your role is scoped to, which client organisations you cover,
//  and what you personally authored or attended.
//
//  DESIGN NOTES, because two choices here are deliberate and load-bearing:
//
//  1. ONE TAXONOMY, ONE DIRECTION, LEAF-ONLY CLASSIFICATION.
//     (:Asset)-[:CLASSIFIED_AS]->(:SubIndustry)-[:NARROWER_THAN]->(:Industry)
//     -[:NARROWER_THAN]->(:Sector).  Assets attach at the LEAF only, and
//     NARROWER_THAN always points child -> parent. Sector membership is derived
//     by traversal rather than stored, which keeps the hierarchy load-bearing —
//     if the shortcut existed, most rules would take it and the traversal would
//     never be exercised by a test.
//
//  2. RELATIONSHIP TYPES CARRY ONE MEANING EACH. Classification, hierarchy and
//     org structure are CLASSIFIED_AS, NARROWER_THAN and PART_OF rather than one
//     shared "belongs to". A single overloaded type wired in both directions
//     makes a bounded traversal impossible: any variable-length walk reaches
//     most of the graph, and an entitlement rule written over it silently grants
//     far more than intended.
//
//  Load this FIRST — it has no dependency on the identity graph. Records carry
//  authorEmail / participantEmails / signupEmails as PROPERTIES, which
//  identity.cypher promotes to relationships.
//
//    cypher-shell -a "$NEO4J_URI" -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
//                 -f bundles/asset_platform/data/platform.cypher
//
//  Idempotent: everything is tagged source:'ap-platform' and wiped first.
//
//  WHICH RECORDS ARE PERMISSIONED
//    Document, Interaction and Meeting carry `Permissions.Read`.
//
//    Asset is NOT permissioned, and that choice is worth stating. A public
//    securities universe is not the secret; the research, the client
//    conversations and the meeting invitations about it are. Making Asset
//    permissioned looked reasonable and broke every coverage rule that joins to
//    it: a caller entitled to an interaction but not to the asset's sector lost
//    the whole row, because the filter drops a row when ANY variable in scope
//    fails. Reference data must be reference data.
//
//    The sector taxonomy, asset classes, issuers and client organisations are
//    reference data for the same reason.
// =====================================================================

CREATE CONSTRAINT ap_asset_id    IF NOT EXISTS FOR (a:Asset)       REQUIRE a.ticker IS UNIQUE;
CREATE CONSTRAINT ap_doc_id      IF NOT EXISTS FOR (d:Document)    REQUIRE d.docId IS UNIQUE;
CREATE CONSTRAINT ap_int_id      IF NOT EXISTS FOR (i:Interaction) REQUIRE i.interactionId IS UNIQUE;
CREATE CONSTRAINT ap_mtg_id      IF NOT EXISTS FOR (m:Meeting)     REQUIRE m.meetingId IS UNIQUE;
CREATE CONSTRAINT ap_org_name    IF NOT EXISTS FOR (o:ClientOrg)   REQUIRE o.name IS UNIQUE;
CREATE INDEX      ap_sector_name IF NOT EXISTS FOR (s:Sector)      ON (s.name);

MATCH (n {source:'ap-platform'}) DETACH DELETE n;

// ---------- Sector taxonomy: three levels, child -> parent ----------
UNWIND ['Energy', 'Technology'] AS s
CREATE (:Sector {name:s, source:'ap-platform'});

UNWIND [
  {name:'Oil & Gas',        sector:'Energy'},
  {name:'Utilities',        sector:'Energy'},
  {name:'Software',         sector:'Technology'},
  {name:'Hardware',         sector:'Technology'}
] AS i
MATCH (s:Sector {name:i.sector})
CREATE (:Industry {name:i.name, source:'ap-platform'})-[:NARROWER_THAN]->(s);

UNWIND [
  {name:'Offshore Drilling',  industry:'Oil & Gas'},
  {name:'Refining',           industry:'Oil & Gas'},
  {name:'Power Generation',   industry:'Utilities'},
  {name:'Enterprise Software',industry:'Software'},
  {name:'Semiconductors',     industry:'Hardware'}
] AS si
MATCH (i:Industry {name:si.industry})
CREATE (:SubIndustry {name:si.name, source:'ap-platform'})-[:NARROWER_THAN]->(i);

// ---------- Asset attributes (reference data) ----------
UNWIND ['Equity', 'Credit'] AS c
CREATE (:AssetClass {name:c, source:'ap-platform'});
UNWIND ['Meridian Offshore plc', 'Calder Refining Co', 'Aurora Grid AB',
        'Northstar Software Inc', 'Vantage Semiconductor NV'] AS n
CREATE (:Issuer {name:n, source:'ap-platform'});

// ---------- Assets, classified at the LEAF only ----------
UNWIND [
  {ticker:'MEROF', name:'Meridian Offshore',   sub:'Offshore Drilling',   class:'Equity', issuer:'Meridian Offshore plc'},
  {ticker:'CALRF', name:'Calder Refining',     sub:'Refining',            class:'Equity', issuer:'Calder Refining Co'},
  {ticker:'AURGD', name:'Aurora Grid',         sub:'Power Generation',    class:'Credit', issuer:'Aurora Grid AB'},
  {ticker:'NRTHS', name:'Northstar Software',  sub:'Enterprise Software', class:'Equity', issuer:'Northstar Software Inc'},
  {ticker:'VNTSC', name:'Vantage Semi',        sub:'Semiconductors',      class:'Equity', issuer:'Vantage Semiconductor NV'}
] AS a
MATCH (si:SubIndustry {name:a.sub}), (ac:AssetClass {name:a.class}), (iss:Issuer {name:a.issuer})
CREATE (asset:Asset {ticker:a.ticker, name:a.name, source:'ap-platform'})
CREATE (asset)-[:CLASSIFIED_AS]->(si)
CREATE (asset)-[:OF_CLASS]->(ac)
CREATE (asset)-[:ISSUED_BY]->(iss);

// ---------- Client organisations (reference data) ----------
// Distinct from the internal org structure, which lives in identity.cypher. A
// client's employer and a trading desk are different kinds of thing and sharing
// one label for both makes any rule scoped to "your organisation" ambiguous.
UNWIND ['Northwind Asset Management', 'Kestrel Capital',
        'Aster Pension Trust', 'Rivermark Industries'] AS o
CREATE (:ClientOrg {name:o, source:'ap-platform'});

// ---------- Research documents ----------
// `authorEmail` is promoted to (:Document)-[:AUTHORED_BY]->(:Employee) by
// identity.cypher. The ACL names the author individually, which is the entry a
// list model has to write and later revoke for every document.
UNWIND [
  {id:'DOC-1', title:'Offshore drilling capex outlook',    asset:'MEROF',
   author:'ella.moreau@bank.com', to:'Northwind Asset Management', embargoed:false},
  {id:'DOC-2', title:'Enterprise software renewal cycles', asset:'NRTHS',
   author:'raj.patel@bank.com',   to:'Kestrel Capital',            embargoed:false},
  // Embargoed: the author holds a genuine grant and is denied anyway.
  {id:'DOC-3', title:'Refining margins — pre-publication',  asset:'CALRF',
   author:'ella.moreau@bank.com', to:'Northwind Asset Management', embargoed:true},
  // Reachable by the SECTOR SCOPE ALONE. The energy researcher did not write it,
  // does not cover the organisation it went to, and is not named in its ACL. The
  // only route is: role -> Energy -> Utilities -> Power Generation -> AURGD.
  // Two taxonomy hops, which is what makes this a real test of the hierarchy.
  {id:'DOC-4', title:'Power generation capacity build',     asset:'AURGD',
   author:'yuki.tanaka@bank.com', to:'Aster Pension Trust', embargoed:false}
] AS d
MATCH (a:Asset {ticker:d.asset}), (o:ClientOrg {name:d.to})
CREATE (doc:Document {docId:d.id, title:d.title, embargoed:d.embargoed,
                      authorEmail:d.author, source:'ap-platform'})
SET doc.`Permissions.Read` = [d.author, 'compliance-review']
CREATE (doc)-[:WAS_ABOUT]->(a)
CREATE (doc)-[:DISTRIBUTED_TO]->(o);

// ---------- Client interactions ----------
UNWIND [
  {id:'INT-1', subject:'Drilling capex discussion', asset:'MEROF',
   org:'Northwind Asset Management', people:['oscar.lindgren@bank.com','mia.torres@northwind.com']},
  // With a client organisation that is restricted for the covering desk.
  {id:'INT-2', subject:'Treasury hedging review',   asset:'AURGD',
   org:'Rivermark Industries',       people:['sam.okoye@bank.com']}
] AS i
MATCH (a:Asset {ticker:i.asset}), (o:ClientOrg {name:i.org})
CREATE (int:Interaction {interactionId:i.id, subject:i.subject,
                         participantEmails:i.people, source:'ap-platform'})
SET int.`Permissions.Read` = ['compliance-review']
CREATE (int)-[:WITH_ORG]->(o)
CREATE (int)-[:WAS_ABOUT]->(a);

// ---------- Corporate access meetings ----------
UNWIND [
  {id:'MTG-1', topic:'Northstar Software management briefing', asset:'NRTHS',
   signups:['mia.torres@northwind.com']}
] AS m
MATCH (a:Asset {ticker:m.asset})
CREATE (mtg:Meeting {meetingId:m.id, topic:m.topic, signupEmails:m.signups,
                     source:'ap-platform'})
SET mtg.`Permissions.Read` = ['compliance-review']
CREATE (mtg)-[:WAS_ABOUT]->(a);

MATCH (n {source:'ap-platform'})
RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC, label;
