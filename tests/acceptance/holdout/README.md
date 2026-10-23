# October issue acceptance holdout

Author: GPT-6 Luna. The human explicitly approved this same-runtime authoring exception. Authored 2026-10-23 without inspecting product code, tests, operator configuration, or secrets.

Pack path: `C:/scratch/2026-10-23_search-agent-issues/acceptance-wave1`.

The default test-suite collection skips these integrated scenarios unless `AINEWS_RUN_ACCEPTANCE=1`. `run_acceptance.py` sets that flag, launches pytest against this pack, writes JUnit XML outside the repository, and fails if all ten scenarios do not execute with zero skips. Use `C:/search_agent/.venv/Scripts/python.exe C:/scratch/2026-10-23_search-agent-issues/acceptance-wave1/run_acceptance.py` for the explicit holdout run.

The tests use subprocess CLI calls, loopback HTTP, and temporary SQLite databases. The subprocess launcher imports the CLI only inside the child process. Each test creates a temporary checkout-shaped directory containing a copy of `src`, sets explicit dummy credentials, model settings, timezone, scheduler, tracing, and demo values, and disables seeded sources after initialization. This keeps project-relative operator configuration out of fixtures. No test invokes provider work.

The holdout covers visible refusal and preservation behavior for prune, but does not claim to prove SQLite transaction interleaving. It checks confirmation model selection, but does not inspect outbound provider request parameters. Those properties have no stable public pause or inspection surface under the supplied contract and belong to the focused transport/config checks.
