"""The persist step: publish the bulletin and close out the run row.

It no longer writes the summaries. Those were committed one at a time as they
came back (ADR 0030), so this node's job is the *other* object: one day's
reading, in one language, as a ranking over the summaries that were in view.

Publishing is a new `version` of the day rather than a second bulletin. That is
the whole of what F1 changes for the reader: two presses before lunch leave one
page, and the earlier version stays in the archive as what was on the front page
at the time rather than being overwritten.

Cost is computed here rather than reported by the API, from the token counts
each LLM node carried back. It is an estimate and the run row says so - prompt
caching in particular makes the real bill lower than this number.

It also runs the free checks over the day it just published and stores them on
the bulletin (PLAN-V2 5.5). They read rows and call nothing, so this costs a
few milliseconds and no money - and it is what puts a quality number in front of
someone who never opens a terminal. `ainews/quality/` is product code for
exactly this reason; the paid evaluation stays in `evals/`, which nothing here
imports (ADR 0019 §2).
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Bulletin, BulletinItem, Run, Summary, SummaryTranslation
from ainews.db.models import utcnow
from ainews.pipeline.pricing import estimate_cost
from ainews.pipeline.state import PipelineState
from ainews.quality import day_stories, published_checks

log = logging.getLogger(__name__)

# Errors are shown in /runs verbatim, so the column has to stay readable.
MAX_STORED_ERRORS = 20


async def publish_bulletin(
    session: AsyncSession, state: PipelineState, rank_cost: float
) -> Bulletin | None:
    """Write this run's ranking as the next version of its day.

    `None` when the run ranked nothing: a press that found no candidates has
    not edited the day, and writing an empty version 2 over a real version 1
    would take the reader's bulletin away and call it an update.
    """
    items = state.get("ranked") or []
    if not items:
        return None

    day, language = state["day"], "en"
    ids = [item["summary_id"] for item in items]
    summaries = {
        row.id: row
        for row in (await session.execute(select(Summary).where(Summary.id.in_(ids)))).scalars()
    }
    translations = {
        row.summary_id: row
        for row in (
            await session.execute(
                select(SummaryTranslation).where(
                    SummaryTranslation.summary_id.in_(ids), SummaryTranslation.language == "tr"
                )
            )
        ).scalars()
    }
    reason_tr = state.get("reason_translations") or {}
    expected_reason_ids = {
        item["summary_id"] for item in items if (item.get("reason") or "").strip()
    }
    if set(reason_tr) != expected_reason_ids:
        raise ValueError("Turkish item reasons do not exactly match the nonempty English reasons")
    for item in items:
        summary = summaries.get(item["summary_id"])
        translation = translations.get(item["summary_id"])
        if summary is None or summary.language != "en":
            raise ValueError("cannot publish a non-English or missing canonical summary")
        if not all(
            (value or "").strip()
            for value in (summary.title_local, summary.summary, summary.why_it_matters)
        ):
            raise ValueError(f"summary {summary.id} has blank canonical English prose")
        if (
            translation is None
            or translation.canonical_hash != summary.content_hash
            or not all(
                (value or "").strip()
                for value in (
                    translation.title_local,
                    translation.summary,
                    translation.why_it_matters,
                )
            )
        ):
            raise ValueError(f"summary {summary.id} has no valid Turkish translation")
        if (item.get("reason") or "").strip() and not (reason_tr.get(summary.id) or "").strip():
            raise ValueError(f"summary {summary.id} has no Turkish reason translation")
    editor_note = (state.get("editor_note") or "").strip()
    if not editor_note:
        raise ValueError("publication has no English editor note")
    if not (state.get("editor_note_tr") or "").strip():
        raise ValueError("publication has no Turkish editor note")
    version = (
        await session.execute(
            select(func.coalesce(func.max(Bulletin.version), 0) + 1)
            .where(Bulletin.day == day)
            .where(Bulletin.language == language)
        )
    ).scalar_one()

    bulletin = Bulletin(
        day=day,
        language=language,
        version=version,
        editor_note=editor_note,
        editor_note_tr=state["editor_note_tr"].strip(),
        bilingual=True,
        model_rank=state.get("model_rank"),
        agreement=state.get("agreement"),
        est_cost_usd=rank_cost,
        run_id=state["run_id"],
    )
    session.add(bulletin)
    await session.flush()
    for item in items:
        session.add(
            BulletinItem(
                bulletin_id=bulletin.id,
                summary_id=item["summary_id"],
                position=item["position"],
                tier=item["tier"],
                reason=(item.get("reason") or "").strip() or None,
                reason_tr=(reason_tr.get(item["summary_id"]) or "").strip() or None,
            )
        )
    await session.flush()

    try:
        stories = await day_stories(session, bulletin)
        bulletin.checks_json = json.dumps(
            published_checks(stories, bulletin.editor_note), ensure_ascii=False
        )
    except Exception:
        # A measurement that fails must not lose a bulletin the run paid for.
        # The report recomputes where this column is null, so the cost of the
        # miss is a slower report and not a missing page.
        log.exception("could not compute the published checks for bulletin %d", bulletin.id)
    return bulletin


async def persist_run(
    session: AsyncSession, state: PipelineState, settings: Settings | None = None
) -> Run:
    settings = settings or get_settings()
    run = await session.get(Run, state["run_id"])
    if run is None:
        raise RuntimeError(f"run {state['run_id']} disappeared mid-flight")

    payloads = state.get("summaries") or []
    summary_failures = state.get("summary_failures") or []
    # A retried `Send` appends its payload a second time, because `summaries` is
    # a concatenating reducer. The rows themselves are safe - the summarise node
    # writes them under a unique key - but counting the tokens twice would put a
    # bill on the run that nobody was charged.
    by_summary = {payload["summary_id"]: payload for payload in payloads}

    # The rank calls' tokens come on their own channel and are priced at *their*
    # model. Costing every token at `openai_model_summarize` is only right while
    # the two knobs point at the same model - the day the summariser moves up to
    # terra (ADR 0001) the run row would lie about the bill.
    usage = state.get("rank_usage") or {"tokens_in": 0, "tokens_out": 0}
    rank_in, rank_out = usage["tokens_in"], usage["tokens_out"]
    # Priced at the model this run actually used, which since ADR 0020 is a
    # choice made at the press and may be nothing like the environment's
    # default. Reading `settings` here would let the two agree with each other
    # and both disagree with the run.
    rank_cost = estimate_cost(state.get("model_rank") or settings.openai_model, rank_in, rank_out)
    summarize_in = sum(payload["tokens_in"] for payload in by_summary.values()) + sum(
        failure["tokens_in"] for failure in summary_failures
    )
    summarize_out = sum(payload["tokens_out"] for payload in by_summary.values()) + sum(
        failure["tokens_out"] for failure in summary_failures
    )
    summarize_cost = sum(payload["est_cost_usd"] for payload in by_summary.values()) + sum(
        failure["est_cost_usd"] for failure in summary_failures
    )
    errors = state.get("errors") or []

    bulletin = await publish_bulletin(session, state, rank_cost)

    run.finished_at = utcnow()
    run.n_collected = state.get("n_collected", 0)
    run.n_new = state.get("n_new", 0)
    run.n_summarized = len(by_summary)
    translation_in = state.get("translation_tokens_in", 0)
    translation_out = state.get("translation_tokens_out", 0)
    run.tokens_in = summarize_in + rank_in + translation_in
    run.tokens_out = summarize_out + rank_out + translation_out
    # The run's own spend, which is what it paid for: the summaries it bought
    # plus the ranking it bought. A bulletin's cost is the ranking alone, and a
    # summary carries its own - the same dollar is not on two rows.
    translation_cost = estimate_cost(
        state.get("model_summarize") or settings.openai_model_summarize,
        translation_in,
        translation_out,
    )
    run.est_cost_usd = summarize_cost + rank_cost + translation_cost
    run.error = "\n".join(errors[:MAX_STORED_ERRORS]) or None
    # "partial" is a real outcome, not a failure: a feed 404ed or three articles
    # would not summarise, and the other ninety-seven are still a bulletin.
    run.status = "partial" if errors else "ok"

    await session.commit()
    log.info(
        "run %s finished: %d summaries, bulletin %s, %d/%d tokens, ~$%.4f, status=%s",
        run.id,
        run.n_summarized,
        f"{bulletin.day} v{bulletin.version}" if bulletin else "none",
        run.tokens_in,
        run.tokens_out,
        run.est_cost_usd,
        run.status,
    )
    return run
