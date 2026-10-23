# October issue acceptance holdout, wave 2

Author: GPT-6 Luna. The human explicitly approved this same-runtime authoring exception. Authored 2026-10-23 from the frozen contracts only, without inspecting product code, source, tests, operator configuration, or secrets.

Pack path: `C:/scratch/2026-10-23_search-agent-issues/acceptance-wave2`.

This pack adds H11–H19 to the ten wave-one scenarios. The final holdout is flat under `tests/acceptance/holdout`; its shared `run_acceptance.py` and `verify.json` require all nineteen scenarios to execute with zero skips.

The local OpenAI-compatible provider is an external dependency stand-in implemented with Python's standard HTTP server. It dispatches by `response_format.json_schema.name`, records requests and token usage, returns deterministic structured JSON for ArticleSummary, RankedDigest, StoryTranslation and EditorialTranslation, and can return incomplete editorial output once. It does not import or replace product functions. The tests send only loopback OpenAI requests, use dummy credentials, disable seeded sources after the documented sources sync, leave Tavily disabled, and put databases, provider traffic, logs and JUnit output in temporary paths.
