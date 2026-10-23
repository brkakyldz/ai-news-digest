# Wave-two coverage and contract gaps

| Scenario | Requirements | Observable behavior covered |
|---|---|---|
| H11 | R10 | Successful digest stores English canonical summaries and complete Turkish per-summary prose; publication has bilingual English bulletin, translated editor note and reasons; EN/TR archive membership, order and tiers agree. |
| H12 | R10 | Incomplete editorial translation records an error run and paid cost, leaves the previous bulletin current, and writes no items for the failed run. |
| H13 | R10 | Retry ranks the existing English pool, buys no summaries, reuses committed story translations without new translation calls or duplicated translation rows/cost. |
| H14 | R10 | Legacy single-language bulletin remains readable under both UI languages with an honest language label; an unselected English summary without Turkish cache remains present and visibly English. |
| H15 | R10 | Search in Turkish finds stored Turkish prose while returning the canonical summary ID once. |
| H16 | R3/R10 | EN/TR views expose identical relevant story IDs and topic counts; the topic count matches the filtered story IDs in both languages. |
| H17 | R10 | Dashboard, archive, search and run-detail reads do not increase local provider request count. |
| H18 | R10 | A simulated 0008 schema upgrades additively, keeps legacy summary/bulletin/item/verdict identities, and does not invent translations for seeded legacy records. |
| H19 | R10 | The public `ainews demo export --out PATH` command serializes a translated summary, editorial note, item reason, and bilingual metadata. |

## Boundaries in the contract

- ArticleSummary and RankedDigest are named, but their exact response fields and prompt candidate-number representation are not specified. The stand-in builds schema-shaped deterministic objects from the request's JSON Schema and prompt; it does not claim to model provider semantics beyond those public schemas.
- The contract requires demo export/seed roundtrips for translated rows, but specifies only the public export command (`ainews demo export --out PATH`) and not an import path or artifact format. H19 verifies that the exported JSON preserves translated narrative and bilingual columns; a custom exported-artifact seed/import roundtrip cannot be asserted without a documented import interface.
- The migration contract identifies the prior revision as 0008 and additive behavior; it does not specify a standalone migration CLI command. H18 invokes the documented sources CLI after constructing a 0008 database state, which exercises startup migration behavior.
- The harness does not make external publisher, Tavily, or OpenAI calls. The local OpenAI stand-in is the only network endpoint and binds to loopback.
