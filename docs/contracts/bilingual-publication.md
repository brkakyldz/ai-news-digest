# Bilingual publication contract v2

Requirements: R3 and R10 in `october-issues.md`; issue #10 and ADR 0035.
This contract is frozen before the wave-2 implementation lanes start.

## Stored objects

- New Summary rows are canonical English, `language='en'`. Existing TR/EN rows
  remain unchanged. Existing articles that already have only a legacy TR summary
  are not automatically bought again as English backfill.
- `Summary.content_hash` is SHA-256 of the UTF-8 JSON list
  `[title_local, summary, why_it_matters]`, ensure_ascii=False and separators
  `(',', ':')`. It is derived, not persisted on Summary.
- New `SummaryTranslation` / `summary_translations` has integer id, summary_id
  FK, language, title_local, summary, why_it_matters, canonical_hash (64 chars),
  model, tokens_in, tokens_out, est_cost_usd and UTC created_at. Unique
  `(summary_id, language)`, language currently `tr`. It translates only prose,
  never copies ranking/classification/tag data into a second source of truth.
- `Bulletin.bilingual` is non-null boolean, false for existing rows.
  `Bulletin.editor_note_tr` and `BulletinItem.reason_tr` are nullable text.
  New complete publications have language en and bilingual true. They have one
  item set, one agreement value and one version namespace, with TR text beside it.
- An additive Alembic revision after 0008 creates these objects and leaves all
  existing rows, verdict IDs, historical checks and model metadata intact.

## Paid pipeline and publication

All new digest entry points summarize/rank in English, independent of UI or
legacy `language`/`out` arguments. Keep existing callable/CLI arguments accepted
where needed for compatibility, but explain that new digests always produce both
languages. The confirmation has no paid language selector; it states EN + TR.
Model choices ms/mr remain independent; the selected summary model translates.

The graph is collect -> dedupe -> enrich -> summarize fan-out -> rank -> translate
-> persist. With no new candidates, it still reaches rank over the existing
English pool; this is required to retry after translation failure without buying
summaries again. With no rankable stories, it publishes no new empty bulletin.

The translation step produces a valid TR translation for every selected English
summary plus the editor note and nonempty item reasons. Matching canonical_hash
reuses a committed translation; mismatch replaces it. Existing translation rows
with blank narrative text never count as complete. All selected summaries are
translated even when their low tier collapses prose visually.

Use structured output schemas with stable names for the deterministic provider:

```text
StoryTranslation {title_local: nonblank str, summary: nonblank str,
                  why_it_matters: nonblank str}
EditorialTranslation {editor_note: nonblank str,
                      reasons: [{summary_id: int, reason: nonblank str}]}
```

The editorial prompt includes JSON `editor_note` and `reasons` objects keyed by
canonical summary_id. Responses must cover each expected reason exactly once,
with no invented IDs or duplicates. Empty source reasons may stay empty; an empty
source editor note when no stories exist requires no call. A selected fallback
ranking still has its canonical fallback editorial text translated normally.
Validate completeness before publishing, including at the persist boundary.
Do not let a bypassing caller publish a bilingual flag with missing translations.

Translate only selected stories. Completed per-story translations commit as
reusable work. A translation exception or incomplete editorial result is a hard
run error; it never enters the soft summary-error channel. No new Bulletin or
BulletinItem is committed, and the previous publication remains current.

Record summary fan-out before translation; do not write the same step twice.
The translation step accounts newly paid calls and editorial work on both success
and failure. Failed runs aggregate completed paid step costs into the Run. Cache
hits contribute no new tokens/cost. Product reports continue to sum Run costs
once, so versions or language toggles never duplicate spend. The cost ceiling
includes translation even when there are zero new summary candidates, uses the
existing English pool/top_n for selected-story bounds, and guards before paid
calls. It must not estimate a zero-candidate retry as free ranking/translation.

## Read contract

`lang` chooses both UI and available stored prose. For a new bilingual bulletin,
EN/TR return identical story IDs, positions and tiers; only prose/reasons/note
change. Never filter missing translations out of the selected list.
Legacy single-language prose remains readable in either shell with its true
language labelled. Extra unselected English stories without a cached TR version
also retain membership and get an honest English fallback marker. No read pays
for translation. Topic counts reflect exactly the relevant visible story IDs in
the chosen scope, independent of language.

Projection uses a translation only if canonical_hash matches Summary.content_hash
and all fields are nonblank. Invalid/stale cache falls back visibly, never claims
to be Turkish. Summary/verdict identity remains canonical in both views.
Turkish search matches stored Turkish fields and projects them without duplicating
canonical IDs; canonical FTS remains usable. A small escaped LIKE search over
translation prose is sufficient; stale translations do not create phantom hits.
Archive selection and links refer to the same Bulletin ID in both languages.

Repair invalidates the repaired summary's translation atomically with new prose;
failed repair preserves the previous cache. Demo export/seed roundtrips translated
rows and new columns; schema-1 bundled demo remains loadable without invented
translations. Quality checks and judge inputs keep canonical English; a quality
number must not claim translation accuracy.

## Independent black-box harness

Use the wave-1 CLI/HTTP/raw-SQL fixture harness against isolated DBs. Seed the
bundled demo to initialize all tables, disable sources, then remove its editorial
fixtures or shift them outside the candidate window. Add synthetic articles with
usable English bodies, nonduplicate titles and fresh timestamps. A locally hosted
OpenAI-compatible provider is a fixture of the external dependency, never a mock
of product functions. Set OPENAI_BASE_URL to its loopback /v1 endpoint, dummy
OPENAI_API_KEY, OPENAI_MAX_RETRIES=0 and short OPENAI_TIMEOUT_SECONDS.
No external publisher, Tavily or real OpenAI calls are allowed.

Verified M0: the existing installed LangChain honors OPENAI_BASE_URL, posts to
`/v1/chat/completions` with response_format.type=json_schema, parses a real
ArticleSummary and reads provider usage. Spike lives outside the repository at
`C:/scratch/2026-10-23_search-agent-issues/provider_standin_spike.py`; zero paid calls.

The fixture dispatches by request response_format.json_schema.name:
ArticleSummary, RankedDigest, StoryTranslation, EditorialTranslation. Return
ordinary OpenAI chat completion envelopes with content JSON and usage. Record
requests, return deterministic English canonical/translated Turkish prose, shuffle
rank picks only as the table's supplied candidate numbers require, and simulate
one controlled translation error/missing-field response. Test successful EN/TR
identity, editorial coverage, no publication on failure, visible recorded failure
spend, retry reuse with zero summary rebuy, old history readability, translated
search, same topic counts and unchanged read-side request counts.

Wave-2 independent scenarios H11 onward are authored before either wave-2 lane
builds. Builders never edit them. Final verification executes every expected
scenario; skipped scenarios are not passes.
