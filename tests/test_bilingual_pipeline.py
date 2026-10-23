from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ainews.clock import local_day
from ainews.db import (
    Article,
    Bulletin,
    BulletinItem,
    Run,
    RunStep,
    Source,
    Summary,
    SummaryTranslation,
    create_engine,
    init_db,
)
from ainews.db.models import utcnow
from ainews.demo import export_demo, load_demo, seed_demo, write_demo
from ainews.pipeline.graph import translate_node
from ainews.pipeline.nodes import summarize as summarize_module
from ainews.pipeline.nodes import translate as translate_module
from ainews.pipeline.nodes.persist import persist_run, publish_bulletin
from ainews.pipeline.nodes.summarize import summarize_article
from ainews.pipeline.nodes.translate import EditorialReason, EditorialTranslation, StoryTranslation
from ainews.pipeline.pricing import estimate_cost
from ainews.pipeline.runner import _fail_run, reconcile_orphaned_runs
from ainews.pipeline.steps import record_fan_out


class _Raw:
    def __init__(self) -> None:
        self.usage_metadata = {"input_tokens": 80, "output_tokens": 20}


class _Structured:
    def __init__(self, schema: type[Any], answer: Any) -> None:
        self.schema = schema
        self.answer = answer

    async def ainvoke(self, _prompt: str) -> dict[str, Any]:
        answer = self.answer(self.schema) if callable(self.answer) else self.answer
        return {"parsed": answer, "raw": _Raw()}


class _LLM:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def with_structured_output(self, schema: type[Any], **_: Any) -> _Structured:
        return _Structured(schema, self.answer)


async def _canonical(session: AsyncSession) -> Summary:
    source = Source(name="Lab", url="https://lab.dev/feed")
    session.add(source)
    await session.flush()
    article = Article(
        source_id=source.id,
        title="A new AI release ships version 8",
        url="https://lab.dev/8",
        url_canonical="https://lab.dev/8",
        body_text="The vendor released version 8 after a year of development.",
    )
    session.add(article)
    await session.flush()
    summary = Summary(
        article_id=article.id,
        language="en",
        title_local="AI release ships version 8",
        summary=(
            "The vendor shipped version 8. It changes the model. The release is available today."
        ),
        why_it_matters="Builders can now test version 8.",
        importance=4,
        model="gpt-6-luna",
    )
    session.add(summary)
    await session.commit()
    return summary


def _state(summary: Summary) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "language": "en",
        "day": local_day(),
        "model_summarize": "gpt-6-luna",
        "ranked": [
            {"summary_id": summary.id, "position": 1, "tier": "lead", "reason": "Version 8 is new."}
        ],
        "editor_note": "Version 8 led the day.",
    }


async def test_translation_cache_reuses_matching_hash_and_replaces_stale_rows(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = await _canonical(session)
    session.add(
        SummaryTranslation(
            summary_id=summary.id,
            language="tr",
            title_local="Yapay zeka surumu 8",
            summary="Saglayici 8 surumunu cikardi. Model degisti. Bugun kullanima acildi.",
            why_it_matters="Gelistriciler 8 surumunu deneyebilir.",
            canonical_hash=summary.content_hash,
            model="gpt-6-luna",
        )
    )
    await session.commit()
    calls: list[str] = []

    def answer(schema: type[Any]) -> Any:
        calls.append(schema.__name__)
        if schema is StoryTranslation:
            return StoryTranslation(
                title_local="Yapay zeka surumu 8",
                summary="Saglayici 8 surumunu cikardi. Model degisti. Bugun kullanima acildi.",
                why_it_matters="Gelistriciler 8 surumunu deneyebilir.",
            )
        return EditorialTranslation(
            editor_note="Surum 8 gunun ana konusuydu.",
            reasons=[EditorialReason(summary_id=summary.id, reason="Surum 8 yeni cikti.")],
        )

    monkeypatch.setattr(translate_module, "summarizer", lambda **_: _LLM(answer))
    state = _state(summary)
    result = await translate_module.translate_publication(state)
    assert calls == ["EditorialTranslation"]
    assert result["translation_tokens_in"] == 80

    summary.summary = summary.summary + " Version 8 adds a local mode."
    await session.commit()
    calls.clear()
    result = await translate_module.translate_publication(state)
    assert calls == ["StoryTranslation", "EditorialTranslation"]
    await session.refresh(summary)
    cached = (
        await session.execute(
            select(SummaryTranslation).where(SummaryTranslation.summary_id == summary.id)
        )
    ).scalar_one()
    assert cached.canonical_hash == summary.content_hash
    assert result["translation_tokens_in"] == 160


async def test_editorial_coverage_failure_keeps_paid_usage(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = await _canonical(session)
    session.add(
        SummaryTranslation(
            summary_id=summary.id,
            language="tr",
            title_local="Yapay zeka surumu 8",
            summary="Saglayici 8 surumunu cikardi.",
            why_it_matters="Gelistriciler deneyebilir.",
            canonical_hash=summary.content_hash,
            model="gpt-6-luna",
        )
    )
    await session.commit()

    def answer(schema: type[Any]) -> Any:
        assert schema is EditorialTranslation
        return EditorialTranslation(editor_note="Surum 8 gunun ana konusuydu.", reasons=[])

    monkeypatch.setattr(translate_module, "summarizer", lambda **_: _LLM(answer))
    run = Run(kind="digest", language="en")
    session.add(run)
    await session.flush()
    session.add(
        RunStep(
            run_id=run.id,
            node="summarize",
            model="gpt-6-luna",
            tokens_in=100,
            tokens_out=20,
            est_cost_usd=estimate_cost("gpt-6-luna", 100, 20),
            n_out=1,
        )
    )
    await session.commit()
    state = _state(summary)
    state["run_id"] = run.id
    with pytest.raises(translate_module.TranslationFailure) as caught:
        await translate_node(state)
    assert caught.value.tokens_in == 80
    assert caught.value.tokens_out == 20
    await _fail_run(run.id, "editorial translation omitted a selected reason")
    await session.refresh(run)
    assert run.status == "error"
    assert (run.tokens_in, run.tokens_out, run.n_summarized) == (180, 40, 1)
    assert run.est_cost_usd == pytest.approx(estimate_cost("gpt-6-luna", 180, 40))


async def test_persist_rejects_missing_translation_before_creating_publication(
    session: AsyncSession,
) -> None:
    summary = await _canonical(session)
    run = Run(kind="digest", language="en")
    session.add(run)
    await session.commit()
    state = _state(summary)
    state["run_id"] = run.id
    state["ranked"][0]["reason"] = ""
    with pytest.raises(ValueError, match="valid Turkish translation"):
        await publish_bulletin(session, state, 0.0)
    assert (await session.execute(select(Bulletin))).scalars().all() == []


async def test_translation_rejects_blank_canonical_prose_before_provider_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = await _canonical(session)
    summary.why_it_matters = "  "
    await session.commit()
    monkeypatch.setattr(
        translate_module,
        "summarizer",
        lambda **_: pytest.fail("blank English prose must be rejected before a paid call"),
    )

    with pytest.raises(translate_module.TranslationFailure, match="blank"):
        await translate_module.translate_publication(_state(summary))


async def test_persist_rejects_blank_english_prose_and_note_without_replacing_bulletin(
    session: AsyncSession,
) -> None:
    summary = await _canonical(session)
    session.add(
        SummaryTranslation(
            summary_id=summary.id,
            language="tr",
            title_local="Yapay zeka surumu 8",
            summary="Saglayici 8 surumunu cikardi.",
            why_it_matters="Gelistriciler deneyebilir.",
            canonical_hash=summary.content_hash,
            model="gpt-6-luna",
        )
    )
    existing_run = Run(kind="digest", language="en")
    session.add(existing_run)
    await session.flush()
    prior = Bulletin(
        day=local_day(), language="en", version=1, editor_note="Prior", run_id=existing_run.id
    )
    session.add(prior)
    await session.commit()

    state = _state(summary)
    state["run_id"] = existing_run.id
    state["ranked"][0]["reason"] = ""
    summary.title_local = " "
    await session.commit()
    with pytest.raises(ValueError, match="blank canonical English prose"):
        await publish_bulletin(session, state, 0.0)
    assert [row.version for row in (await session.execute(select(Bulletin))).scalars()] == [1]

    summary.title_local = "AI release ships version 8"
    await session.commit()
    state["editor_note"] = "  "
    with pytest.raises(ValueError, match="English editor note"):
        await publish_bulletin(session, state, 0.0)
    assert [row.version for row in (await session.execute(select(Bulletin))).scalars()] == [1]


async def test_later_cache_failure_keeps_prior_paid_translation_usage(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await _canonical(session)
    source = await session.get(Article, first.article_id)
    assert source is not None
    second = Summary(
        article_id=first.article_id + 1,
        language="en",
        title_local="A second release ships version 9",
        summary="The vendor shipped version 9. It changes the model. It is available today.",
        why_it_matters="Builders can now test version 9.",
        importance=3,
        model="gpt-6-luna",
    )
    second_article = Article(
        source_id=source.source_id,
        title="A second release ships version 9",
        url="https://lab.dev/9",
        url_canonical="https://lab.dev/9",
        body_text="The vendor released version 9 after a year of development.",
    )
    session.add(second_article)
    await session.flush()
    second.article_id = second_article.id
    session.add(second)
    await session.commit()

    def answer(schema: type[Any]) -> Any:
        assert schema is StoryTranslation
        return StoryTranslation(
            title_local="Yapay zeka surumu 8",
            summary="Saglayici 8 surumunu cikardi.",
            why_it_matters="Gelistriciler deneyebilir.",
        )

    monkeypatch.setattr(translate_module, "summarizer", lambda **_: _LLM(answer))
    original_execute = session.execute
    cache_selects = 0

    async def controlled_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal cache_selects
        if "summary_translations" in str(statement).lower():
            cache_selects += 1
            if cache_selects == 2:
                raise RuntimeError("controlled cache lookup failure")
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", controlled_execute)

    @asynccontextmanager
    async def use_test_session():
        yield session

    monkeypatch.setattr(translate_module, "session_scope", use_test_session)
    state = _state(first)
    state["ranked"].append(
        {"summary_id": second.id, "position": 2, "tier": "support", "reason": "Version 9."}
    )
    with pytest.raises(translate_module.TranslationFailure, match="controlled cache") as caught:
        await translate_module.translate_publication(state)
    assert (caught.value.tokens_in, caught.value.tokens_out) == (80, 20)
    cached = (
        await original_execute(
            select(SummaryTranslation).where(SummaryTranslation.summary_id == first.id)
        )
    ).scalar_one()
    assert cached.tokens_in == 80


async def test_unparsable_summary_usage_flows_to_step_and_partial_run(
    session: AsyncSession, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Source(name="Malformed", url="https://malformed.dev/feed")
    session.add(source)
    await session.flush()
    article = Article(
        source_id=source.id,
        title="A real model release ships version 83",
        url="https://malformed.dev/83",
        url_canonical="https://malformed.dev/83",
        body_text="The release ships today.",
    )
    run = Run(kind="digest", language="en")
    session.add_all([article, run])
    await session.commit()

    class Raw:
        def __init__(self) -> None:
            self.usage_metadata = {"input_tokens": 70, "output_tokens": 30}

    class Structured:
        async def ainvoke(self, _prompt: str) -> dict[str, Any]:
            return {"parsed": None, "raw": Raw(), "parsing_error": ValueError("schema mismatch")}

    class FakeSummarizer:
        def with_structured_output(self, *_: Any, **__: Any) -> Structured:
            return Structured()

    monkeypatch.setattr(summarize_module, "summarizer", lambda *_: FakeSummarizer())
    failed = await summarize_article(
        {
            "run_id": run.id,
            "language": "en",
            "article_id": article.id,
            "model": "gpt-6-luna",
        }
    )
    assert failed.get("summaries", []) == []
    failure = failed["summary_failures"][0]
    assert failure["article_id"] == article.id
    assert (failure["tokens_in"], failure["tokens_out"]) == (70, 30)

    start = utcnow()
    session.add(
        RunStep(
            run_id=run.id,
            node="enrich",
            started_at=start - timedelta(seconds=1),
            finished_at=start,
        )
    )
    await session.commit()
    state = {
        "run_id": run.id,
        "language": "en",
        "day": local_day(),
        "model_summarize": "gpt-6-luna",
        "candidate_ids": [article.id],
        "summaries": [],
        "summary_failures": [failure],
        "ranked": [],
        "rank_usage": {"tokens_in": 0, "tokens_out": 0},
        "errors": failed["errors"],
    }
    await record_fan_out(state, start + timedelta(milliseconds=1))
    await persist_run(session, state, settings)
    await session.refresh(run)
    assert (run.tokens_in, run.tokens_out, run.n_summarized) == (70, 30, 0)
    assert run.est_cost_usd == pytest.approx(estimate_cost("gpt-6-luna", 70, 30))
    assert run.status == "partial"
    summary_step = (
        await session.execute(
            select(RunStep).where(RunStep.run_id == run.id, RunStep.node == "summarize")
        )
    ).scalar_one()
    assert (summary_step.tokens_in, summary_step.tokens_out, summary_step.n_out) == (70, 30, 0)


async def test_failed_and_reconciled_runs_sum_completed_paid_steps(
    session: AsyncSession,
) -> None:
    run = Run(kind="digest", language="en")
    session.add(run)
    await session.flush()
    session.add_all(
        [
            RunStep(
                run_id=run.id,
                node="summarize",
                model="gpt-6-luna",
                tokens_in=100,
                tokens_out=20,
                est_cost_usd=0.00002,
                n_out=1,
            ),
            RunStep(
                run_id=run.id,
                node="translate",
                model="gpt-6-luna",
                tokens_in=80,
                tokens_out=20,
                est_cost_usd=0.000018,
                status="error",
            ),
        ]
    )
    await session.commit()
    await _fail_run(run.id, "translation coverage failed")
    await session.refresh(run)
    assert (run.tokens_in, run.tokens_out, run.n_summarized) == (180, 40, 1)
    assert run.est_cost_usd == pytest.approx(0.000038)

    orphan = Run(kind="digest", language="en")
    session.add(orphan)
    await session.flush()
    session.add(
        RunStep(
            run_id=orphan.id,
            node="rank",
            model="gpt-6-luna",
            tokens_in=50,
            tokens_out=10,
            est_cost_usd=0.00001,
        )
    )
    await session.commit()
    assert await reconcile_orphaned_runs() == 1
    await session.refresh(orphan)
    assert (orphan.tokens_in, orphan.tokens_out, orphan.est_cost_usd) == (50, 10, 0.00001)


async def test_demo_schema_two_roundtrips_translations_and_schema_one_still_loads(
    session: AsyncSession, settings: Any, tmp_path: Any
) -> None:
    summary = await _canonical(session)
    run = Run(kind="digest", language="en", status="ok")
    session.add(run)
    await session.flush()
    translation = SummaryTranslation(
        summary_id=summary.id,
        language="tr",
        title_local="Yapay zeka surumu 8",
        summary="Saglayici 8 surumunu cikardi.",
        why_it_matters="Gelistriciler deneyebilir.",
        canonical_hash=summary.content_hash,
        model="gpt-6-luna",
    )
    bulletin = Bulletin(
        day=local_day(),
        language="en",
        version=1,
        editor_note="Version 8 led.",
        editor_note_tr="Surum 8 one cikti.",
        bilingual=True,
        run_id=run.id,
    )
    session.add_all([translation, bulletin])
    await session.flush()
    session.add(
        BulletinItem(
            bulletin_id=bulletin.id,
            summary_id=summary.id,
            position=1,
            tier="lead",
            reason="New release.",
            reason_tr="Yeni surum.",
        )
    )
    await session.commit()

    data = await export_demo(session)
    path = write_demo(data, tmp_path / "demo.json")
    loaded = load_demo(path)
    assert loaded.schema == 2
    assert loaded.summary_translations[0]["canonical_hash"] == summary.content_hash
    assert loaded.bulletins[0]["bilingual"] is True
    assert loaded.bulletins[0]["editor_note_tr"] == "Surum 8 one cikti."
    assert loaded.bulletin_items[0]["reason_tr"] == "Yeni surum."

    target = create_engine(settings, f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    await init_db(target)
    try:
        async with async_sessionmaker(target, expire_on_commit=False)() as seeded:
            await seed_demo(seeded, loaded)
            copied = (await seeded.execute(select(SummaryTranslation))).scalar_one()
            copied_bulletin = (await seeded.execute(select(Bulletin))).scalar_one()
            copied_item = (await seeded.execute(select(BulletinItem))).scalar_one()
            assert copied.canonical_hash == summary.content_hash
            assert copied_bulletin.bilingual is True
            assert copied_bulletin.editor_note_tr == "Surum 8 one cikti."
            assert copied_item.reason_tr == "Yeni surum."
    finally:
        await target.dispose()

    legacy_path = tmp_path / "schema-1.json"
    legacy_path.write_text(json.dumps({"schema": 1}), encoding="utf-8")
    legacy = load_demo(legacy_path)
    assert legacy.schema == 2
    assert legacy.summary_translations == []
