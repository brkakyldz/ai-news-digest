"""The sampled judge and the rank-stability probe (PLAN-EVALS E3.6), offline.

A fake model stands in for the judge and the ranker; nothing here has a key or
a network, and the one thing that would spend money - the call - is exactly
what the cost guard is tested to stop.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import local_day
from ainews.config import Settings
from ainews.db import Article, Bulletin, BulletinItem, EvalResult, Run, Source, Summary, Verdict
from ainews.evals import judge as judge_module
from ainews.evals.cli import run_eval
from ainews.evals.judge import (
    Calibration,
    CostGuard,
    GroundingVerdict,
    calibrate,
    choose_sample,
    format_calibration,
    judge_bulletin,
    judge_labelled,
)


class FakeMessage:
    def __init__(self, tokens_in: int = 900, tokens_out: int = 30) -> None:
        self.usage_metadata = {"input_tokens": tokens_in, "output_tokens": tokens_out}
        self.response_metadata: dict[str, Any] = {}


class FakeStructured:
    def __init__(self, answer: Any, *, calls: list[str], raise_with: Exception | None = None):
        self._answer = answer
        self._calls = calls
        self._raise = raise_with

    async def ainvoke(self, prompt: str, *_: object, **__: object) -> dict[str, Any]:
        self._calls.append(prompt)
        if self._raise:
            raise self._raise
        answer = self._answer(prompt) if callable(self._answer) else self._answer
        return {"parsed": answer, "raw": FakeMessage()}


class FakeLLM:
    def __init__(self, answer: Any, *, raise_with: Exception | None = None) -> None:
        self.answer = answer
        self.calls: list[str] = []
        self.raise_with = raise_with

    def with_structured_output(self, _schema: Any, **__: object) -> FakeStructured:
        return FakeStructured(self.answer, calls=self.calls, raise_with=self.raise_with)


BODY = "Nvidia will pay $12.9 billion for Hugging Face, which hosts three million models. " * 8


@pytest.fixture
async def bulletin(session: AsyncSession) -> Bulletin:
    """A published bulletin: three stories on the page, three the day left out.

    Both halves, because `choose_sample` prefers the published ones and fills
    the rest from below the fold - a judge that only ever saw the bulletin would
    measure the ranker's taste as much as the summariser's grounding.
    """
    src = Source(name="Ars", url="https://ars.dev/feed", weight=1.2)
    session.add(src)
    await session.flush()
    run = Run(kind="digest", language="tr", status="ok", n_summarized=6)
    session.add(run)
    await session.flush()
    bulletin = Bulletin(
        day=local_day(), language="tr", version=1, est_cost_usd=0.004, run_id=run.id
    )
    session.add(bulletin)
    await session.flush()
    for i in range(6):
        art = Article(
            source_id=src.id,
            title=f"Story {i}",
            url=f"https://ars.dev/{i}",
            url_canonical=f"https://ars.dev/{i}",
            body_text=BODY,
        )
        session.add(art)
        await session.flush()
        summary = Summary(
            article_id=art.id,
            language="tr",
            title_local=f"Haber {i}",
            summary=f"Ozet {i}. Iki. Uc.",
            why_it_matters="Onemli.",
            tags_json=json.dumps(["nvidia"]),
            importance=3 + (i % 3),
        )
        session.add(summary)
        await session.flush()
        if i < 3:
            session.add(
                BulletinItem(
                    bulletin_id=bulletin.id,
                    summary_id=summary.id,
                    position=i + 1,
                    tier="lead" if i == 0 else "major",
                )
            )
    await session.commit()
    return bulletin


@pytest.fixture
def fake_judge(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    llm = FakeLLM(GroundingVerdict(passed=True))
    monkeypatch.setattr(judge_module, "judge_model", lambda *_: llm)
    return llm


# -- the judge ----------------------------------------------------------------


async def test_the_judge_refuses_to_run_without_a_key(
    env_free_of_keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(
        eval_command="judge", run="latest", sample=12, seed=0, max_cost=0.1, labelled=False
    )
    assert await run_eval(args) == 2
    assert "OPENAI_API_KEY is not set" in capsys.readouterr().err


def test_the_sample_is_stable_under_a_seed() -> None:
    candidates = [judge_module.Candidate(i, "r", i, "s", "t", "b", "sum", "why") for i in range(50)]
    first = [c.summary_id for c in choose_sample(candidates, 12, seed=7)]
    second = [c.summary_id for c in choose_sample(list(reversed(candidates)), 12, seed=7)]
    other = [c.summary_id for c in choose_sample(candidates, 12, seed=8)]
    assert first == second, "input order must not change the sample"
    assert len(first) == 12 and first == sorted(first)
    assert first != other
    assert len(choose_sample(candidates[:5], 12, seed=0)) == 5, "a small run is judged whole"


async def test_the_cost_guard_stops_before_the_first_call(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM(GroundingVerdict(passed=True), raise_with=AssertionError("must not be called"))
    monkeypatch.setattr(judge_module, "judge_model", lambda *_: llm)

    with pytest.raises(CostGuard) as exc:
        await judge_bulletin(session, bulletin.id, sample=6, max_cost=0.0, settings=settings)
    assert "over --max-cost" in str(exc.value)
    assert llm.calls == []
    assert (await session.execute(select(EvalResult))).scalars().all() == []


async def test_a_judged_run_writes_one_row_per_summary_with_its_cost(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, fake_judge: FakeLLM
) -> None:
    report = await judge_bulletin(
        session, bulletin.id, sample=4, seed=1, max_cost=1.0, settings=settings
    )
    assert (report.n_passed, report.n_failed, report.n_unparsed) == (4, 0, 0)
    assert len(fake_judge.calls) == 4
    assert BODY[:30] in fake_judge.calls[0], "the judge reads the body the summariser read"

    rows = (await session.execute(select(EvalResult))).scalars().all()
    assert len(rows) == 4
    assert {r.kind for r in rows} == {"grounding"}
    assert all(r.passed is True and r.model == settings.openai_model_judge for r in rows)
    assert all(r.est_cost_usd > 0 and r.tokens_in == 900 for r in rows)
    assert report.est_cost_usd == pytest.approx(sum(r.est_cost_usd for r in rows))


async def test_a_failed_claim_is_recorded_verbatim(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(prompt: str) -> GroundingVerdict:
        if "Ozet 2." in prompt:
            return GroundingVerdict(passed=False, unsupported_claim="500 bin veri kümesi")
        return GroundingVerdict(passed=True)

    monkeypatch.setattr(judge_module, "judge_model", lambda *_: FakeLLM(answer))
    report = await judge_bulletin(session, bulletin.id, sample=6, max_cost=1.0, settings=settings)
    assert report.n_failed == 1
    failed = (
        await session.execute(select(EvalResult).where(EvalResult.passed.is_(False)))
    ).scalar_one()
    assert failed.detail == "500 bin veri kümesi"


async def test_a_judge_that_fails_to_parse_is_recorded_not_raised(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(judge_module, "judge_model", lambda *_: FakeLLM(None))
    report = await judge_bulletin(session, bulletin.id, sample=2, max_cost=1.0, settings=settings)
    assert report.n_unparsed == 2
    rows = (await session.execute(select(EvalResult))).scalars().all()
    assert [r.passed for r in rows] == [None, None]
    assert all(r.detail == "unparsable output" for r in rows)


async def test_an_upstream_error_is_a_row_not_a_traceback(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM(None, raise_with=RuntimeError("simulated 500"))
    monkeypatch.setattr(judge_module, "judge_model", lambda *_: llm)
    report = await judge_bulletin(session, bulletin.id, sample=1, max_cost=1.0, settings=settings)
    assert report.outcomes[0].error is not None and "simulated 500" in report.outcomes[0].error
    row = (await session.execute(select(EvalResult))).scalar_one()
    assert row.passed is None and "simulated 500" in (row.detail or "")


# -- calibration --------------------------------------------------------------


def test_wrong_fact_catch_counts_are_diagnostic_and_ignore_ok_rows() -> None:
    table = calibrate(
        [
            ("wrong", "wrong_fact", False),
            ("wrong", "wrong_fact", True),
            ("wrong", "not_news", False),
            ("ok", None, False),
        ]
    )
    assert table == Calibration(wrong_caught=1, wrong_missed=1, other_reason=1)
    text = format_calibration(table)
    assert "diagnostic" in text
    assert "caught 1; missed 1" in text
    assert "precision" not in text.lower() and "tnr" not in text.lower()


def test_unparsed_judgements_are_counted_apart() -> None:
    table = calibrate([("ok", None, None), ("wrong", "wrong_fact", False)])
    assert table == Calibration(wrong_caught=1)


def test_only_a_wrong_fact_is_scored_against_the_grounding_judge() -> None:
    """Four claims sit in one story block and this judge reads one of them.

    A reader marking a faithful summary of an irrelevant story used to count as
    a grounding miss, so the judge's TPR fell for being right. `not_news`,
    `duplicate` and `wrong_place` leave the table entirely - they are not "ok"
    either, because a story the reader rejected is no evidence the summary is
    faithful (PLAN-V2 5.1).
    """
    table = calibrate(
        [
            ("wrong", "wrong_fact", False),
            ("wrong", "not_news", True),
            ("wrong", "duplicate", True),
            ("wrong", "wrong_place", True),
            # A label from before the reader was asked which of the four.
            ("wrong", None, True),
            ("ok", None, True),
        ]
    )
    assert (table.wrong_caught, table.wrong_missed) == (1, 0)
    assert table.n_wrong == 1
    assert table.other_reason == 4
    assert "another category" in format_calibration(table)


async def test_labelled_mode_judges_only_what_the_reader_labelled(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = list((await session.execute(select(Summary.id).order_by(Summary.id))).scalars())
    session.add(Verdict(summary_id=ids[0], verdict="wrong", reason="wrong_fact", note="uydurma"))
    session.add(Verdict(summary_id=ids[1], verdict="ok"))
    await session.commit()

    def answer(prompt: str) -> GroundingVerdict:
        return GroundingVerdict(passed="Ozet 0." not in prompt, unsupported_claim=None)

    llm = FakeLLM(answer)
    monkeypatch.setattr(judge_module, "judge_model", lambda *_: llm)
    table, outcomes = await judge_labelled(session, max_cost=1.0, settings=settings)
    assert len(outcomes) == 1 and len(llm.calls) == 1
    assert (table.wrong_caught, table.wrong_missed) == (1, 0)


# -- the sample follows the reader (2026-09-08) --------------------------------


def test_the_sample_is_drawn_from_the_published_stories_first() -> None:
    """The reader labels what the page shows, which is the bulletin. A sample
    drawn uniformly over ninety summaries held one or two of them, so a
    judgement and a label almost never landed on the same story."""
    candidates = [
        judge_module.Candidate(
            i, 1, i, "s", "t", "b", "sum", "why", position=i + 1 if i < 3 else None
        )
        for i in range(20)
    ]
    chosen = choose_sample(candidates, 5, seed=0)
    assert [c.summary_id for c in chosen if c.position is not None] == [0, 1, 2], "every one"
    assert len(chosen) == 5, "the rest filled from below the fold"

    two = choose_sample(candidates, 2, seed=0)
    assert all(c.position is not None for c in two), "a small sample stays on the page"
    assert two == choose_sample(list(reversed(candidates)), 2, seed=0), "still seeded"
