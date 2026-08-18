# Client platform bundle

An institutional client platform: clients consume research, analytics, execution
and data/API products, and the commercial question is **up/cross-sell** — which
product to offer which client next. Entitlement-mediated throughout.

**To run it against Aura, follow
[`docs/entitlement-testing-tutorial.md`](../../docs/entitlement-testing-tutorial.md).**

## Model

```
(:User)-[:MEMBER_OF]->(:AdGroup)          identity: people and entitlement groups
(:User)-[:COVERS]->(:Client)              named individual coverage
(:User)-[:LOGGED]->(:Interaction)         authorship
(:Client)-[:COVERED_BY]->(:AdGroup)       team coverage
(:Client)-[:SUBSCRIBES_TO]->(:Product)    what a client already has
(:UsageSummary)-[:FOR_CLIENT|FOR_PRODUCT]->()   engagement
(:Interaction)-[:WITH_CLIENT]->(:Client)  meetings and calls
(:Opportunity)-[:FOR_CLIENT|FOR_PRODUCT]->()    the cross-sell pipeline
```

**Permissioned:** `Interaction`, `Opportunity`, `UsageSummary`.
**Reference data:** `Client`, `Product`, `ResearchNote` — a product catalogue is
not sensitive; what a particular client pays for, was pitched, or discussed, is.

Three kinds of entitlement group, because they behave differently:

| Group | Kind | Expressed as |
| --- | --- | --- |
| `coverage-*` | relationship-derived | a **path** to the client |
| `product-*` | role-based | a **list** entry |
| `platform-admin`, `compliance-review` | role-based | a **list** entry |

Hence `grant_model: both` — not as a migration step, but because a real model
contains both kinds.

## Tools

| Tool | Answers |
| --- | --- |
| `client_portfolio` | Which clients do I cover, and how much do they buy? |
| `client_engagement` | How much is a client actually using each product? |
| `cross_sell_candidates` | What do comparable clients use that this one does not? |
| `client_opportunities` | What is in the pipeline, and what is it worth? |
| `coverage_directory` | Which teams exist and who is in them? |

Plus the engine's `resolve-identity`, `explain-access` and `secure-read-cypher`.

## The demo beat

Run `client_opportunities` as different people. Same tool, same question:

| Caller | Sees | Pipeline |
| --- | --- | --- |
| `evan.brooks` (NAMR coverage) | 1 opportunity | $120,000 |
| `lena.fischer` (EMEA AM coverage) | 2 opportunities | $600,000 |
| `nadia.haddad` (analytics specialist) | 3 opportunities | $640,000 |
| `peter.lindqvist` (supervision) | 4 opportunities | $900,000 |

The totals differ because the aggregate is computed **after** filtering. Note
also that the specialist's access has a different *shape* from coverage — she
sees her product family across every team.

## Two things worth knowing

**A permissioned node reached by `OPTIONAL MATCH` does not become null when the
caller cannot read it — the filter drops the whole row.** Mixing reference data
and permissioned data in one query therefore loses rows the caller was entitled
to. That is why `cross_sell_candidates` does not join to `Opportunity`; the
pipeline is a separate tool.

**`client_portfolio` scopes to the caller's coverage in its `match`, using the
`caller` variable, rather than via `anchor:`.** That narrowing is the question
itself — a business scoping decision. An `anchor:` is a performance optimisation
that must not change which rows are entitled, and the conformance harness checks
it. For anchoring proper, see `covered_client_trades` in the `iam` bundle.
