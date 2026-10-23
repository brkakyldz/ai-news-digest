"""Read projections for bilingual bulletins and their legacy history."""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import day_bounds, local_day
from ainews.db import (
    Article,
    Bulletin,
    BulletinItem,
    Source,
    Summary,
    SummaryTranslation,
)
from ainews.db.models import utcnow
from ainews.web import queries
from ainews.web.views import project_editor_note


async def _seed_bilingual(session: AsyncSession) -> dict[str, object]:
    source = Source(name="Lab", url="https://lab.example/feed")
    session.add(source)
    await session.flush()
    now = utcnow()

    async def add_summary(number: int, *, language: str = "en", relevant: bool = True) -> Summary:
        article = Article(
            source_id=source.id,
            title=f"Canonical marker story {number}",
            url=f"https://lab.example/{number}",
            url_canonical=f"https://lab.example/{number}",
            published_at=now - timedelta(minutes=number),
        )
        session.add(article)
        await session.flush()
        summary = Summary(
            article_id=article.id,
            language=language,
            title_local=(
                f"Haber eski {number}" if language == "tr" else f"English marker {number}"
            ),
            summary=f"Canonical prose marker {number}",
            why_it_matters="Canonical why",
            tags_json=json.dumps(["agents"] if number != 2 else ["models"]),
            importance=4,
            relevant=relevant,
        )
        session.add(summary)
        await session.flush()
        return summary

    selected = [await add_summary(1), await add_summary(2)]
    extra = await add_summary(3)
    irrelevant = await add_summary(4, relevant=False)
    legacy = await add_summary(5, language="tr")
    legacy_extra_en = await add_summary(6)
    legacy_extra_tr = await add_summary(7, language="tr")
    old_day = local_day(now - timedelta(days=1))
    old_start, _ = day_bounds(old_day)
    legacy_extra_en.created_at = old_start + timedelta(hours=1)
    legacy_extra_tr.created_at = old_start + timedelta(hours=2)

    current = Bulletin(
        day=local_day(),
        language="en",
        version=1,
        editor_note="Canonical editorial note.",
        editor_note_tr="Türkçe editör notu.",
        bilingual=True,
        agreement=0.8,
    )
    old = Bulletin(
        day=old_day,
        language="tr",
        version=1,
        editor_note="Eski editör notu.",
        bilingual=False,
    )
    session.add_all([current, old])
    await session.flush()
    for index, summary in enumerate(selected, start=1):
        session.add(
            BulletinItem(
                bulletin_id=current.id,
                summary_id=summary.id,
                position=index,
                tier="lead" if index == 1 else "major",
                reason="Canonical reason.",
                reason_tr="Türkçe neden.",
            )
        )
    session.add(
        BulletinItem(
            bulletin_id=old.id,
            summary_id=legacy.id,
            position=1,
            tier="lead",
            reason="Eski neden.",
        )
    )
    valid = SummaryTranslation(
        summary_id=selected[0].id,
        language="tr",
        title_local="Türkçe çevrilmiş başlık",
        summary="çeviri araması için % işareti ve alt çizgi _ içerir",
        why_it_matters="Türkçe önemi.",
        canonical_hash=selected[0].content_hash,
        model="gpt-6-luna",
    )
    stale = SummaryTranslation(
        summary_id=selected[1].id,
        language="tr",
        title_local="Eski önbellek hayaletkelime.",
        summary="Boş olmayan eski metin.",
        why_it_matters="Eski önem.",
        canonical_hash="0" * 64,
        model="gpt-6-luna",
    )
    session.add_all([valid, stale])
    await session.commit()
    return {
        "current": current,
        "old": old,
        "selected": selected,
        "extra": extra,
        "irrelevant": irrelevant,
        "legacy": legacy,
        "legacy_extra_en": legacy_extra_en,
        "legacy_extra_tr": legacy_extra_tr,
    }


async def test_bilingual_views_keep_membership_order_and_show_honest_language(
    client: TestClient, session: AsyncSession
) -> None:
    objects = await _seed_bilingual(session)
    current = objects["current"]
    selected = objects["selected"]
    assert isinstance(current, Bulletin)
    assert isinstance(selected, list)
    ids = [summary.id for summary in selected]

    english = client.get("/?lang=en").text
    turkish = client.get("/?lang=tr").text
    for body in (english, turkish):
        assert [body.index(f'id="story-{summary_id}"') for summary_id in ids] == sorted(
            body.index(f'id="story-{summary_id}"') for summary_id in ids
        )
        assert 'class="item p5" id="story-' in body
        assert 'class="item p4" id="story-' in body

    assert "English marker 1" in english
    assert "Türkçe çevrilmiş başlık" in turkish
    assert "English marker 2" in turkish
    assert 'lang="en">English</span>' in turkish
    assert "bar__lang" not in turkish, "a bilingual bulletin is not English-only"
    assert "Türkçe neden." in turkish
    assert '<h2 class="item__t" lang="tr"><a' in turkish
    assert 'lang="tr">çeviri araması için % işareti' in turkish
    expanded = client.get("/?lang=tr&all=1").text
    assert "English marker 3" in expanded
    assert 'lang="en">English</span>' in expanded

    projected = await queries.stories_for_bulletin(session, current, language="tr")
    assert [story.id for story in projected] == ids
    assert [story.reason for story in projected] == ["Türkçe neden.", "Türkçe neden."]
    assert [story.reason_language for story in projected] == ["tr", "tr"]


async def test_legacy_archive_labels_its_language_in_both_shells(
    client: TestClient, session: AsyncSession
) -> None:
    objects = await _seed_bilingual(session)
    old = objects["old"]
    assert isinstance(old, Bulletin)

    for language in ("en", "tr"):
        body = client.get(f"/archive?lang={language}&b={old.id}").text
        assert "Haber eski 5" in body
        assert 'lang="tr">Türkçe</span>' in body
        assert "Eski editör notu." in body


async def test_legacy_turkish_expansion_keeps_english_and_turkish_extras(
    session: AsyncSession,
) -> None:
    objects = await _seed_bilingual(session)
    old = objects["old"]
    english = objects["legacy_extra_en"]
    turkish = objects["legacy_extra_tr"]
    assert isinstance(old, Bulletin)
    assert isinstance(english, Summary)
    assert isinstance(turkish, Summary)

    for language in ("en", "tr"):
        stories = await queries.other_stories(session, old, language=language)
        by_id = {story.id: story for story in stories}
        assert set(by_id) == {english.id, turkish.id}
        assert by_id[english.id].prose_language == "en"
        assert by_id[english.id].language_label
        assert by_id[turkish.id].prose_language == "tr"
        assert by_id[turkish.id].language_label


async def test_blank_turkish_note_and_reason_fall_back_to_english(
    session: AsyncSession,
) -> None:
    objects = await _seed_bilingual(session)
    current = objects["current"]
    selected = objects["selected"]
    assert isinstance(current, Bulletin)
    assert isinstance(selected, list)
    current.editor_note_tr = "  \n"
    item = (
        await session.execute(
            select(BulletinItem).where(
                BulletinItem.bulletin_id == current.id,
                BulletinItem.summary_id == selected[0].id,
            )
        )
    ).scalar_one()
    item.reason_tr = "  \n"
    await session.commit()

    assert project_editor_note(current, "tr") == ("Canonical editorial note.", "en")
    stories = await queries.stories_for_bulletin(session, current, language="tr")
    first = next(story for story in stories if story.id == selected[0].id)
    assert first.reason == "Canonical reason."
    assert first.reason_language == "en"


async def test_turkish_search_uses_only_valid_escaped_translation_hits(
    session: AsyncSession,
) -> None:
    objects = await _seed_bilingual(session)
    selected = objects["selected"]
    assert isinstance(selected, list)
    first, _ = selected

    hits = await queries.search_stories(session, "% işareti", "tr", limit=20)
    assert [story.id for story in hits.rows] == [first.id]
    assert hits.rows[0].title_local == "Türkçe çevrilmiş başlık"
    assert hits.rows[0].prose_language == "tr"
    assert not hits.rows[0].language_label

    stale = await queries.search_stories(session, "hayaletkelime", "tr", limit=20)
    assert stale.rows == []
    assert stale.total == 0

    duplicate = await queries.search_stories(session, "marker", "tr", limit=20)
    assert len({story.id for story in duplicate.rows}) == duplicate.total


async def test_topic_counts_follow_the_visible_relevant_scope(session: AsyncSession) -> None:
    objects = await _seed_bilingual(session)
    current = objects["current"]
    extra = objects["extra"]
    irrelevant = objects["irrelevant"]
    assert isinstance(current, Bulletin)
    assert isinstance(extra, Summary)
    assert isinstance(irrelevant, Summary)

    published = await queries.tag_counts(session, current)
    expanded = await queries.tag_counts(session, current, include_all=True)
    assert dict(published.published) == {"agents": 1, "models": 1}
    assert dict(expanded.every) == {"agents": 2, "models": 1}
