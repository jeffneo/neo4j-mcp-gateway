// =====================================================================
//  ASSET PLATFORM — trade and compensation records.
//
//  Communications are the easy half. These two labels are the hard half, and
//  they are here because entitlement over TRADE data is a different problem from
//  entitlement over documents: a trade is reachable by half a dozen unrelated
//  routes at once, and one of them is a THRESHOLD rather than a path.
//
//  SEVEN MECHANISMS DECIDE WHO MAY READ A TRADE. Each is a separate rule in
//  bundle.yaml, and each is isolated by at least one conformance case:
//
//    1. the booker            (:Trade)-[:BOOKED_BY]->(:Employee)
//    2. the booking desk      (:Trade)-[:BOOKED_ON]->(:Desk)
//    3. the management line   REPORTS_TO above the booker
//    4. counterparty coverage the caller's team currently covers the account
//    5. supervision by rank   IN_UNIT above the desk, AND rankLevel >= 5
//    6. a notional limit      the desk route stops at 50m unless rank >= 5
//    7. a restricted account  RESTRICTED_FOR withdraws all of the above
//
//  Mechanisms 5 and 6 are why security.identity.caller_attributes exists. "MD or
//  above" is an ORDERING, and a set of principal names cannot express one — you
//  would have to mint a principal per rank and re-issue it on every promotion.
//
//  COMPENSATION is the smallest complete statement of the same problem, and it is
//  the example that comes up first in every conversation about this: everyone
//  sees their own, a manager sees their reports', and only a managing director
//  sees the unit's. In a relational world that is a JOIN between the
//  compensation table and the business_hierarchy view, hand-written per
//  application. Here it is three declared rules over edges that already exist,
//  enforced identically for every query that touches the label.
//
//  WHY COMPENSATION CARRIES NO ACCESS-CONTROL LIST. Every other protected label
//  here has one, so supervision can read it. Compensation deliberately does not:
//  it is governed by path grants ALONE, which makes it the case that proves the
//  path model standing on its own. It is therefore absent from
//  security.protected_labels — that list is a fail-closed check that a label
//  which SHOULD carry a list actually does, and compensation should not.
//
//  Load this LAST — it joins to employees, desks and client organisations from
//  all four earlier steps.
//
//  Idempotent: everything is tagged source:'ap-trading' and wiped first.
// =====================================================================

CREATE CONSTRAINT ap_trade_id IF NOT EXISTS FOR (t:Trade)        REQUIRE t.tradeId IS UNIQUE;
CREATE CONSTRAINT ap_comp_id  IF NOT EXISTS FOR (c:Compensation) REQUIRE c.compId IS UNIQUE;
CREATE INDEX      ap_trade_notional IF NOT EXISTS FOR (t:Trade)  ON (t.notional);

MATCH (n {source:'ap-trading'}) DETACH DELETE n;

// ---------- Trades ----------
// The access-control list names supervisory review only. The booker is NOT in it,
// which is deliberate: the booker's entitlement is a fact about the graph
// (BOOKED_BY), and materialising it as a list entry would be the duplication this
// design exists to remove — two recordings of one fact, free to drift apart.
UNWIND [
  // Tomas's own book. 12m is inside the desk limit, so his whole desk sees it.
  {id:'TRD-1', booker:'tomas.vogel@bank.com',     desk:'DESK-EQD-EMEA',
   cpty:'Aster Pension Trust',        asset:'MEROF', notional: 12000000, side:'BUY'},
  // ABOVE the 50m desk limit. Felipe booked it; the only route to it for anyone
  // else below MD rank is the management line, which is what isolates that rule.
  {id:'TRD-2', booker:'felipe.souza@bank.com',    desk:'DESK-EQD-EMEA',
   cpty:'Kestrel Capital',            asset:'NRTHS', notional: 80000000, side:'SELL'},
  // A RESTRICTED counterparty for the booking desk. Tomas booked it himself and
  // is denied anyway — granted, then withdrawn.
  {id:'TRD-3', booker:'tomas.vogel@bank.com',     desk:'DESK-EQD-EMEA',
   cpty:'Rivermark Industries',       asset:'AURGD', notional:  5000000, side:'BUY'},
  // A different desk under the same business unit: reachable by supervision but
  // not by anyone on the derivatives desk.
  {id:'TRD-4', booker:'ingrid.svensson@bank.com', desk:'DESK-RATES-EMEA',
   cpty:'Aster Pension Trust',        asset:'CALRF', notional: 30000000, side:'SELL'},
  // The COVERAGE isolation case. The EMEA institutional team covers Northwind and
  // nothing else reaches this trade for them: not the desk, not the booker, not
  // the management line, not rank.
  {id:'TRD-5', booker:'ingrid.svensson@bank.com', desk:'DESK-RATES-EMEA',
   cpty:'Northwind Asset Management', asset:'VNTSC', notional:  8000000, side:'BUY'}
] AS t
MATCH (e:Employee {email:t.booker}), (d:OrgUnit {unitId:t.desk}),
      (o:ClientOrg {name:t.cpty}), (a:Asset {ticker:t.asset})
CREATE (trade:Trade {tradeId:t.id, notional:t.notional, side:t.side,
                     tradeDate: date('2026-06-15'), source:'ap-trading'})
SET trade.`Permissions.Read` = ['compliance-review']
CREATE (trade)-[:BOOKED_BY]->(e)
CREATE (trade)-[:BOOKED_ON]->(d)
CREATE (trade)-[:WITH_COUNTERPARTY]->(o)
CREATE (trade)-[:ON_ASSET]->(a);

// ---------- Compensation ----------
// One record per employee the hierarchy view knows about, so the population is
// derived from the feed rather than restated here. `totalComp` is the value the
// entitlement is protecting; the amounts are arbitrary.
MATCH (e:Employee) WHERE e.employeeId IS NOT NULL
WITH e ORDER BY e.employeeId
CREATE (c:Compensation {
  compId: 'COMP-' + e.employeeId,
  fiscalYear: 2026,
  baseSalary: 60000 + coalesce(e.rankLevel, 1) * 45000,
  bonus:      20000 + coalesce(e.rankLevel, 1) * 60000,
  source: 'ap-trading'
})
SET c.totalComp = c.baseSalary + c.bonus
CREATE (c)-[:COMPENSATION_OF]->(e);

MATCH (n {source:'ap-trading'})
RETURN labels(n)[0] AS label, count(*) AS count ORDER BY count DESC, label;
