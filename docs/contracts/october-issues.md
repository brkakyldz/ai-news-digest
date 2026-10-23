# October issue contract v1

Requirement source: GitHub `brkakyldz/ai-research-agent` issues #2–#5 and #7–#11.
This contract defines observable behavior, not implementation instructions.

## Acceptance IDs

- R2: `TIMEZONE=Europe/Istambul` with scheduler enabled serves the dashboard and
  health endpoint; dates/scheduler use UTC fallback. Valid IANA names still work.
- R3: Topic pill counts in `/?all=1` equal the visible relevant stories after
  clicking the tag; irrelevant summaries do not contribute phantom tags/counts.
- R4: Product spend is the sum of actual digest-run costs in the reporting
  time window once per run. Repeated bulletin versions never duplicate shared
  summary costs. `--bulletin` narrows detail only. Eval spend remains separate.
- R5: `ainews prune` and `--dry-run` refuse with nonzero exit for any persisted
  running collect or digest, even without an in-process slot. Article deletion
  checks and writes are serialized in SQLite. Completed runs permit normal
  pruning; summaries, bulletins, verdicts and eval history are preserved.
- R7: `ainews eval --help` has no rank-stability command. Invoking that removed
  command fails without spending. Legacy eval rows survive. Production agreement
  remains visible, including missing agreement.
- R8: Story feedback accepts only wrong with optional reason/note. Reasons are
  wrong_fact, not_news, duplicate, wrong_place. `POST /verdict` rejects ok with
  HTTP 422; repeated wrong submissions without note/reason preserve previous
  annotations. Old ok rows remain in storage but create no active positive action
  or calibration claim. No TNR or precision is reported.
- R9: New report and run-detail quality output contains actionable content-floor,
  numeral grounding/recall, word budget, tag usefulness, production agreement,
  product/eval costs and categorized wrong feedback. No sentence histogram, raw
  importance distribution, fallback overlap, unrepresented-five, tier/note shape
  or standalone rank experiment. Existing dated records are read-only history.
- R11: New default summarization/ranking is gpt-6-luna, explicit valid model
  overrides still work, and request parameters match its supported API contract.
  Prices use verified current rates. The independent judge default is retained.
- R10 (wave 2): Every successful new bulletin contains complete English and
  Turkish narrative fields, editor note and item reasons with identical IDs,
  membership, positions and tiers. The language switch selects matching stored
  content plus UI. Translation failure publishes nothing new and is visible as
  an error run. Cached completed translations are reused; costs are not duplicated.
  Legacy single-language bulletins remain readable with an honest language label.

## Black-box harness surfaces

Use Python pytest, subprocess and stdlib HTTP/SQLite only; no imports from ainews,
no monkeypatching/mocking product functions. Run against temporary databases and
ephemeral loopback ports. Never read `.env` or a developer database. Provide only
dummy API credentials in offline scenarios; no real network/model calls.

`C:/search_agent/.venv/Scripts/python.exe -m ainews.cli` is not guaranteed to
dispatch; use the installed `ainews.exe` entry point with PYTHONPATH set to the
target checkout's `src`, or launch `python -c 'from ainews.cli import main; main()'`
as the CLI process. This tiny subprocess launcher is permitted; test files must
not import product internals. `HOST=127.0.0.1` and `PORT=PORT` in the child
environment followed by `ainews serve` starts HTTP; serve takes no host/port
CLI options. `/health` is the health route.

Initialize an isolated DB through `ainews demo seed`, or `ainews sources` to
create schema/seed sources. CLI and server take DATABASE_URL, OPENAI_API_KEY,
TAVILY_API_KEY, SCHEDULER_ENABLED, PHOENIX_ENABLED, DEMO_MODE and TIMEZONE from
environment. Set DEMO_MODE=false except demo-specific setup. All seeded external
sources can be disabled with raw SQLite fixture writes before the server starts.

Fixtures may read/write documented persistent tables using sqlite3: sources,
articles, summaries, runs, bulletins, bulletin_items, verdicts, eval_results.
Fixture construction may inspect PRAGMA table_info to populate required neutral
columns, or duplicate demo rows with fresh unique identifiers. Summary has
article_id, language, title_local, summary, why_it_matters, tags_json, importance,
key_fact, relevant, kind, model, tokens_in, tokens_out, est_cost_usd, created_at.
Run has id, kind, language, status, timestamps, counts, tokens, est_cost_usd,
model_summarize/model_rank and error. Bulletin has day, language, version,
editor_note, agreement, est_cost_usd, run_id, created_at, checks_json. BulletinItem
has bulletin_id, summary_id, position, tier and reason. Verdict has summary_id,
verdict, reason, note and created_at. EvalResult retains old rank_stability rows.
Dates are UTC SQLite timestamps; bulletin day follows TIMEZONE.

Public HTTP: GET /, /?all=1, /?all=1&tag=TAG, /archive?b=ID,
/runs, /runs/ID, /runs/verdicts, /runs/confirm, /search?q=QUERY. Add `lang=en` or
`lang=tr` for UI language. POST /verdict is form encoded with summary_id,
verdict, optional reason/note/frag; enforce real same-origin/CSRF rules using
normal browser cookies and Origin header. A server rejection caused by missing
CSRF must not be mistaken for successful verdict validation. Parse HTML or use
stdlib HTMLParser; do not assert incidental whitespace or styling.

`ainews eval report --help` describes report selection/output options;
`--since 30d`, `--bulletin ID` and `--out PATH` are its stable report interfaces.
Output artifacts go into the test temp directory, never docs/evals.md.

Wave-2 schema and deterministic provider stand-in are specified in v2 before
R10 scenarios are authored. Wave-1 acceptance covers R2/R3/R4/R5/R7/R8/R9/R11;
R3 can be authored now but is only gated after wave 2.

## Wave-1 observable fixture and presentation details

Report output is UTF-8 Markdown regardless of filename extension. Its window
summary includes `Product spend $0.1080` for two digest runs costing 0.104 and
0.004, and separate `eval spend $...`. Per-bulletin headings start `### Bulletin`.
The keep-list rows include `Key fact named`, `kept in the writing`,
`A body figure survives into the summary`, `Ungrounded numerals`,
`Summaries over 55 words`, `Why-it-matters over 20 words`, `Titles over 10 words`
and tag vocabulary/filter diagnostics. Production agreement is written as
`agreement 0.82` or `agreement n/a` in report bulletin headings. Run detail
must expose an English label containing `agreement` and an honest missing marker
(`n/a` or an em dash); use `lang=en`. Feedback categories use their stable slugs
in reports. No precision or TNR row/claim remains in newly generated output.

On the digest, each visible story has `id="story-SUMMARY_ID"`; story membership
can be counted by those IDs. Topic pills are anchor links with `tag=TAG` in href
and visible tag/count text. Parse anchors and their text without relying on
whitespace, class order or exact typography. For a deterministic topic fixture,
clone a demo article and summary into a fresh ID/unique URL with relevant=false,
same language and created_at on the latest bulletin's day, unique phantom tag,
and no bulletin item. Make a second relevant non-published clone with another
unique tag. Counts must reflect only visible stories, never the irrelevant clone.
The demo contract promises at least one article, summary, run, bulletin and item;
it does not promise any particular existing irrelevant row. Clone all existing
columns via sqlite3 PRAGMA/SELECT and change unique values, rather than depending
on ORM defaults being SQL defaults. Run statuses are running, ok, partial, error.
Create one running collect or digest at a time, check both prune forms, remove
that fixture, then test the other kind independently. `--dry-run` must preserve
the same counts as a refused real prune; successful prune should also be exercised
with a truly old unsummarized fixture article and preserved history counts.

SQLite DATABASE_URL uses `sqlite+aiosqlite:///ABSOLUTE/PATH`. All external sources
must be disabled after fixture initialization; scheduled collection must never
contact publishers in acceptance tests. Model setting keys are OPENAI_MODEL,
OPENAI_MODEL_SUMMARIZE and OPENAI_MODEL_JUDGE. Override them deliberately in
subprocess environments rather than inventing aliases. To verify built-in defaults
independently of operator settings, run the CLI/server with working directory a
temporary checkout-shaped directory containing a copy of the target's `src`
(no secrets) and set PYTHONPATH to that isolated src; this prevents the settings
loader's project-relative operator file from participating. Alternatively use
explicit model environment values for UI override behavior and leave built-in
defaults to lane C's focused config tests; record that coverage limit honestly.

Run-confirmation query parameters are `ms` (summarize) and `mr` (rank); valid
explicit legacy override `gpt-5.6-terra` remains supported. Model buttons carry
the full model ID in `title` and mark selection with `aria-current="true"`.
Do not infer selection from mere model-name presence in the menu. The confirmation
GET must leave the run count unchanged. POST /verdict with same-origin Origin
header needs no CSRF token and returns HTTP 200 on valid form, 422 on invalid
verdict/reason. Do not guess token conventions. Stored historical ok rows remain
unchanged and their summary offers no positive action in active UI.

R5 transaction interleaving and R11 outbound API parameters are covered by focused
lane regression/transport tests and the bounded real spike, since they have no
stable public pause/inspection surface. Holdout scenarios must cover the visible
refusal/preservation/model-choice behavior without pretending to prove those
internal timing/transport properties. H03 must actually assert spend arithmetic
and bulletin-detail narrowing, rather than only command success.
