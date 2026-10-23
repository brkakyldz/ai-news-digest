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
not import product internals. `ainews serve --host 127.0.0.1 --port PORT` starts
HTTP; `/health` is the health route (verify actual response contract before using).

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
