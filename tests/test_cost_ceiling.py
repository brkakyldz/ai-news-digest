"""The guard on the one press that spends money.

ADR 0020 declined a ceiling: the price line under the button was the whole
guard, which holds while the only person who can press it wrote that line. It
does not hold for someone running this with their own key, and the first press
on a fresh installation is the worst case - nothing is collected yet, so a
pre-flight count says zero, `collect` then brings in a week of backlog, and the
press that most needs a ceiling is the one a check at the button waves through.

So the guard sits after `dedupe`, where the real number is known and before
anything has been spent.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import local_day
from ainews.config import Settings
from ainews.db import Article, Run, Source, Summary
from ainews.pipeline import graph as graph_module
from ainews.pipeline.agreement import RANK_PASSES
from ainews.pipeline.pricing import (
    EDITORIAL_TRANSLATE_TOKENS,
    RANK_TOKENS_PER_CANDIDATE,
    SUMMARIZE_TOKENS_PER_ARTICLE,
    TRANSLATE_TOKENS_PER_STORY,
    CostCeiling,
    estimate_cost,
    estimate_digest_cost,
)


def test_the_estimate_prices_each_node_at_the_model_that_will_run_it() -> None:
    """Since ADR 0020 the two may differ by an order of magnitude, and one
    blended number would misprice both halves of the press."""
    n = 20
    summarize_in, summarize_out = SUMMARIZE_TOKENS_PER_ARTICLE
    rank_in, rank_out = RANK_TOKENS_PER_CANDIDATE

    estimate = estimate_digest_cost(n, "gpt-5.6-luna", "gpt-6-astra")
    expected = estimate_cost("gpt-5.6-luna", n * summarize_in, n * summarize_out) + estimate_cost(
        "gpt-6-astra", RANK_PASSES * n * rank_in, RANK_PASSES * n * rank_out
    )

    assert estimate == pytest.approx(expected)
    # Summarising cheaply and ranking expensively is not the same press as
    # doing both cheaply, and the estimate has to be able to tell them apart.
    assert estimate > estimate_digest_cost(n, "gpt-5.6-luna", "gpt-5.6-luna")


def test_gpt6_luna_rates_are_used_for_the_default_press() -> None:
    n = 20
    summarize_in, summarize_out = SUMMARIZE_TOKENS_PER_ARTICLE
    rank_in, rank_out = RANK_TOKENS_PER_CANDIDATE
    expected = (n * summarize_in * 0.10 + n * summarize_out * 0.50) / 1_000_000
    expected += (RANK_PASSES * n * rank_in * 0.10 + RANK_PASSES * n * rank_out * 0.50) / 1_000_000

    assert estimate_cost("gpt-6-luna", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert estimate_digest_cost(n, "gpt-6-luna", "gpt-6-luna") == pytest.approx(expected)
    assert estimate_digest_cost(n, "gpt-6-luna", "gpt-6-luna") < estimate_digest_cost(
        n, "gpt-5.6-luna", "gpt-5.6-luna"
    )


def test_an_ordinary_press_is_cents_and_the_backlog_case_is_dollars() -> None:
    """The two numbers the ceiling sits between, from the audit that asked for
    it: twenty candidates on the cheap tier against a week's backlog on the
    expensive one."""
    ordinary = estimate_digest_cost(20, "gpt-6-luna", "gpt-6-luna")
    backlog = estimate_digest_cost(130, "gpt-6-astra", "gpt-6-astra")

    assert ordinary < 0.10
    assert backlog > 1.00


async def test_a_press_over_the_ceiling_is_refused_before_it_spends(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dedupe` raises, so `enrich` never runs and no branch is ever sent.

    Enrichment is the first node that spends anything - Tavily credits before
    the fan-out spends tokens - so a guard that fires here has cost nothing.
    """
    monkeypatch.setattr(settings, "digest_max_cost_usd", 0.01)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)

    with pytest.raises(CostCeiling) as caught:
        await graph_module._guard_cost({"model_summarize": "gpt-6-astra"}, 130)

    assert caught.value.n_candidates == 130
    assert caught.value.ceiling == 0.01
    # The message carries the numbers: the run row shows this text verbatim, and
    # "too expensive" alone leaves the reader with nothing to decide on.
    assert "130 candidates" in str(caught.value)
    assert "$0.01" in str(caught.value)


async def test_an_ordinary_day_passes_the_guard(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "digest_max_cost_usd", 1.00)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)

    await graph_module._guard_cost({}, 20)


async def test_a_ceiling_of_zero_turns_the_guard_off(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who wants no ceiling should be able to say so, rather than
    setting a number large enough to mean the same thing."""
    monkeypatch.setattr(settings, "digest_max_cost_usd", 0)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)

    await graph_module._guard_cost({"model_summarize": "gpt-6-astra"}, 100_000)


async def test_retry_with_existing_english_pool_is_not_estimated_as_free(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Source(name="Lab", url="https://lab.dev/feed")
    session.add(source)
    await session.flush()
    article = Article(
        source_id=source.id,
        title="New model release number 83",
        url="https://lab.dev/83",
        url_canonical="https://lab.dev/83",
        body_text="A real release story.",
    )
    session.add(article)
    await session.flush()
    session.add(
        Summary(
            article_id=article.id,
            language="en",
            title_local="New model release number 83",
            summary="The model shipped. This is new. Builders can test it.",
            why_it_matters="Builders can test the release.",
            importance=3,
        )
    )
    await session.commit()

    monkeypatch.setattr(settings, "digest_max_cost_usd", 0.000001)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)
    with pytest.raises(CostCeiling) as caught:
        await graph_module._guard_cost(
            {"day": local_day(), "model_summarize": "gpt-6-luna", "model_rank": "gpt-6-luna"}, 0
        )
    assert caught.value.n_candidates == 0


async def test_mixed_model_retry_prices_all_rank_passes_before_spending(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Source(name="Rank pool", url="https://rank.dev/feed")
    session.add(source)
    await session.flush()
    for i in range(4):
        article = Article(
            source_id=source.id,
            title=f"A distinct model release number {i}",
            url=f"https://rank.dev/{i}",
            url_canonical=f"https://rank.dev/{i}",
            body_text="A release story.",
        )
        session.add(article)
        await session.flush()
        session.add(
            Summary(
                article_id=article.id,
                language="en",
                title_local=article.title,
                summary="The vendor shipped the model. It is available now.",
                why_it_matters="Builders can test the model.",
                importance=3,
            )
        )
    await session.commit()

    rank_in, rank_out = RANK_TOKENS_PER_CANDIDATE
    story_in, story_out = TRANSLATE_TOKENS_PER_STORY
    editorial_in, editorial_out = EDITORIAL_TRANSLATE_TOKENS
    one_rank_pass = estimate_cost("gpt-6-astra", 4 * rank_in, 4 * rank_out)
    translation = estimate_cost(
        "gpt-6-luna", 4 * story_in + editorial_in, 4 * story_out + editorial_out
    )
    one_pass_estimate = one_rank_pass + translation
    all_pass_estimate = estimate_digest_cost(
        0, "gpt-6-luna", "gpt-6-astra", rank_pool=4, selected=4
    )
    ceiling = (one_pass_estimate + all_pass_estimate) / 2
    assert one_pass_estimate < ceiling < all_pass_estimate

    monkeypatch.setattr(settings, "digest_max_cost_usd", ceiling)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)
    with pytest.raises(CostCeiling) as caught:
        await graph_module._guard_cost(
            {
                "day": local_day(),
                "model_summarize": "gpt-6-luna",
                "model_rank": "gpt-6-astra",
            },
            0,
        )
    assert caught.value.estimate == pytest.approx(all_pass_estimate)


def test_zero_delta_estimate_still_prices_rank_and_translation() -> None:
    rank_only = estimate_digest_cost(0, "gpt-6-luna", "gpt-6-luna", rank_pool=4, selected=0)
    with_translation = estimate_digest_cost(0, "gpt-6-luna", "gpt-6-luna", rank_pool=4, selected=4)
    assert with_translation > rank_only > 0


async def test_the_guard_stops_the_run_at_dedupe_and_closes_it(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a fresh installation, a week of backlog, the expensive tier.

    The run opens, collects for free, and stops with the estimate on the row.
    Nothing was summarised, so nothing was paid for.
    """
    source = Source(name="TechCrunch", url="https://example.com/feed", weight=1.0)
    session.add(source)
    await session.flush()
    for i in range(40):
        session.add(
            Article(
                source_id=source.id,
                title=f"A distinct headline number {i}",
                url=f"https://example.com/{i}",
                url_canonical=f"https://example.com/{i}",
                body_text="Some body text that is long enough to be usable." * 20,
            )
        )
    run = Run(kind="digest", language="en")
    session.add(run)
    await session.commit()

    monkeypatch.setattr(settings, "digest_max_cost_usd", 0.01)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)

    async def _no_collect(*_: object, **__: object) -> object:
        from ainews.pipeline.nodes.collect import CollectStats

        return CollectStats()

    monkeypatch.setattr(graph_module, "collect_articles", _no_collect)

    app = graph_module.build_graph().compile()
    with pytest.raises(CostCeiling):
        await app.ainvoke(
            {
                "run_id": run.id,
                "language": "en",
                "model_summarize": "gpt-6-astra",
                "candidate_ids": [],
                "summaries": [],
                "ranked": [],
                "errors": [],
            },
            config={"recursion_limit": 50},
        )

    assert (await session.execute(Summary.__table__.select())).first() is None
