"""`ainews eval report` (PLAN-EVALS E4.1): every number, dated, appended, never edited."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import publish

from ainews.config import Settings
from ainews.db import Article, Bulletin, EvalResult, Run, Source, Summary, Verdict
from ainews.evals.cli import run_eval
from ainews.evals.report import (
    HEADER,
    append_report,
    build_report,
    parse_since,
    render_markdown,
)
from ainews.pipeline.prompts import prompt_version


@pytest.fixture
async def measured(session: AsyncSession) -> Bulletin:
    """A bulletin: four summaries, two published, one verdict and judge rows."""
    src = Source(name="Ars", url="https://ars.dev/feed", weight=1.0)
    session.add(src)
    await session.flush()
    run = Run(
        kind="digest",
        language="tr",
        status="ok",
        n_summarized=4,
        est_cost_usd=0.05,
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    ids = []
    for i in range(4):
        art = Article(
            source_id=src.id,
            title=f"Story {i}",
            url=f"https://ars.dev/{i}",
            url_canonical=f"https://ars.dev/{i}",
            body_text="The round was $300 million, said the founder in 2026.",
        )
        session.add(art)
        await session.flush()
        summary = Summary(
            article_id=art.id,
            language="tr",
            title_local=f"Haber {i}",
            summary="Bir. Iki. 300 milyon dolar." if i else "Bir. Iki. 500 bin saat.",
            why_it_matters="Onemli.",
            tags_json=json.dumps(["funding", f"only-{i}"]),
            importance=[5, 3, 3, 2][i],
            model="gpt-5.6-luna",
            tokens_in=1400,
            tokens_out=500,
            est_cost_usd=0.0115,
        )
        session.add(summary)
        await session.flush()
        ids.append(summary.id)
    await session.commit()
    bulletin = await publish(session, ids[:2], run=run, editor_note="a b c\n\nd e f\n\ng h i")
    session.add(Verdict(summary_id=ids[0], verdict="wrong", reason="wrong_fact", note="uydurma"))
    session.add_all(
        [
            EvalResult(
                bulletin_id=bulletin.id,
                summary_id=ids[0],
                kind="grounding",
                passed=False,
                detail="500 bin saat",
                model="gpt-5.6-terra",
                prompt_version=prompt_version("judge_grounding", "en"),
                tokens_in=900,
                tokens_out=30,
                est_cost_usd=0.002,
            ),
            EvalResult(
                bulletin_id=bulletin.id,
                summary_id=ids[1],
                kind="grounding",
                passed=True,
                model="gpt-5.6-terra",
                prompt_version=prompt_version("judge_grounding", "en"),
                tokens_in=900,
                tokens_out=30,
                est_cost_usd=0.002,
            ),
        ]
    )
    await session.commit()
    return bulletin


def test_since_is_a_number_of_days() -> None:
    assert parse_since("30d") == 30
    assert parse_since("7") == 7
    with pytest.raises(ValueError):
        parse_since("2w")


async def test_the_report_carries_every_number_and_its_function(
    session: AsyncSession, measured: Bulletin, settings: Settings
) -> None:
    report = await build_report(session, since_days=30, settings=settings)
    assert len(report.bulletins) == 1
    run = report.bulletins[0]
    assert (run.n_stories, run.n_ranked) == (4, 2)
    assert run.ungrounded and run.ungrounded[0][1] == ["500000"], "500 bin, scaled"
    assert run.verdicts == {
        "wrong": 1,
        "wrong_fact": 1,
        "not_news": 0,
        "duplicate": 0,
        "wrong_place": 0,
    }
    assert report.product_cost == pytest.approx(0.05), "one recorded digest run is counted once"
    assert report.product_runs == 1
    assert report.eval_cost == pytest.approx(0.004)

    text = render_markdown(report)
    for name in (
        "checks.word_budget",
        "checks.ungrounded_numerals",
        "checks.tag_vocabulary",
        "Verdict.reason",
    ):
        assert f"`{name}`" in text, name
    assert "500000" in text
    assert "agreement 0.82" in text
    assert "precision" not in text.lower() and "tnr" not in text.lower()
    for retired in (
        "sentence histogram",
        "Importance 1..5",
        "Ranker vs fallback",
        "Unrepresented fives",
        "Editor against the free order",
    ):
        assert retired not in text


async def test_product_spend_counts_partial_and_failed_digest_runs_once(
    session: AsyncSession, measured: Bulletin, settings: Settings
) -> None:
    session.add_all(
        [
            Run(
                kind="digest",
                language="tr",
                status="partial",
                est_cost_usd=0.004,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            ),
            Run(
                kind="digest",
                language="tr",
                status="error",
                est_cost_usd=0.003,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            ),
            Run(
                kind="collect",
                language="tr",
                status="error",
                est_cost_usd=0.9,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            ),
        ]
    )
    await session.commit()
    report = await build_report(session, since_days=30, settings=settings, bulletin_id=measured.id)
    assert report.product_cost == pytest.approx(0.057)
    assert report.product_runs == 3


async def test_a_bulletin_outside_the_window_is_not_reported(
    session: AsyncSession, measured: Bulletin, settings: Settings
) -> None:
    measured.created_at = datetime.now(UTC) - timedelta(days=40)
    await session.commit()
    report = await build_report(session, since_days=30, settings=settings)
    assert report.bulletins == []
    assert "No bulletin in the window" in render_markdown(report)


def test_sections_are_appended_and_earlier_ones_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "evals.md"
    append_report("## first\n\nx\n", path)
    append_report("## second\n\ny\n", path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(HEADER)
    assert text.index("## first") < text.index("## second")
    assert text.count("# Evaluation record") == 1


async def test_the_cli_prints_and_writes(
    session: AsyncSession, measured: Bulletin, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "evals.md"
    args = argparse.Namespace(
        eval_command="report", since="30d", bulletin=None, out=out, no_write=False
    )
    assert await run_eval(args) == 0
    captured = capsys.readouterr()
    assert "checks.word_budget" in captured.out
    assert out.exists() and "appended to" in captured.err

    args = argparse.Namespace(
        eval_command="report", since="oops", bulletin=None, out=out, no_write=True
    )
    assert await run_eval(args) == 2


async def test_a_run_already_on_the_record_in_the_same_numbers_is_a_pointer_not_a_repeat(
    session: AsyncSession, measured: Bulletin, settings: Settings
) -> None:
    """The record grew by the report count: three sections carried one run
    three times with identical deterministic rows. A block that is already in
    the file byte for byte is written as one line pointing back."""
    report = await build_report(session, since_days=30, settings=settings)
    first = render_markdown(report)
    assert "checks.word_budget" in first

    second = render_markdown(report, existing=first)
    assert "Unchanged since an earlier section." in second
    assert "checks.word_budget" not in second, "the table is not repeated"
    assert f"### Bulletin {measured.day}" in second, "the bulletin is still named"
    assert "### Judge calibration" not in second

    # A changed number is a new section in full.
    report.bulletins[0].verdicts["wrong"] += 1
    third = render_markdown(report, existing=first)
    assert "checks.word_budget" in third


async def test_the_report_can_be_narrowed_to_one_bulletin(
    session: AsyncSession, measured: Bulletin, settings: Settings
) -> None:
    other = await publish(session, [], day="2026-09-01", language="en")

    everything = await build_report(session, since_days=30, settings=settings)
    assert {r.bulletin_id for r in everything.bulletins} == {measured.id, other.id}

    one = await build_report(session, since_days=30, settings=settings, bulletin_id=measured.id)
    assert [r.bulletin_id for r in one.bulletins] == [measured.id]
    assert one.eval_cost == pytest.approx(everything.eval_cost), "the window's spend does not"
    assert one.product_cost == pytest.approx(everything.product_cost), (
        "detail selection does not narrow run spend"
    )


async def test_the_report_keeps_agreement_and_tag_filter_diagnostics(
    session: AsyncSession, measured: Bulletin, settings: Settings
) -> None:
    measured.agreement = None
    await session.commit()
    text = render_markdown(await build_report(session, since_days=30, settings=settings))
    assert "agreement n/a" in text
    assert "Tag singleton share" in text
    assert "Tags from the preferred vocabulary" in text
    assert "Tier" not in text and "fallback" not in text.lower()
