"""Wave-two black-box scenarios for bilingual publication and reads."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import urllib.parse
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from acceptance_helpers import (
    clone_article_summary,
    clone_row,
    environment,
    latest_bulletin,
    run_cli,
    sqlite3_connect,
    table_columns,
)
from provider_standin import OpenAIStandin

pytestmark = pytest.mark.skipif(
    os.environ.get("AINEWS_RUN_ACCEPTANCE") != "1",
    reason="integrated acceptance holdout runs only through run_acceptance.py",
)


class StoryMarkup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.story_ids = []
        self.story_text = {}
        self.story_scales = {}
        self.tag_links = []
        self._anchor = None
        self._story = None
        self._story_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        story_id = attrs.get("id", "")
        if story_id.startswith("story-"):
            self._story = story_id.removeprefix("story-")
            self.story_ids.append(self._story)
            self.story_text[self._story] = []
            scale = re.search(r"(?<![\w-])p([1-5])(?!\w)", attrs.get("class", ""))
            self.story_scales[self._story] = scale.group(1) if scale else None
            self._story_depth = 1
        elif self._story and tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self._story_depth += 1
        if tag == "a" and "tag=" in attrs.get("href", ""):
            self._anchor = {"href": attrs["href"], "text": ""}

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            self.tag_links.append(self._anchor)
            self._anchor = None
        if self._story and self._story_depth:
            self._story_depth -= 1
            if self._story_depth == 0:
                self._story = None

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["text"] += data
        if self._story:
            self.story_text[self._story].append(data)

    def page(self, source):
        self.feed(source)
        return self

    @staticmethod
    def tag_value(link):
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(link["href"]).query).get("tag", [])
        return values[0] if values else None


@pytest.fixture
def standin():
    service = OpenAIStandin().start()
    yield service
    service.close()


def now_utc():
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def add_candidate_articles(db_path: Path, count=3):
    current_time = now_utc()
    with sqlite3_connect(db_path) as db:
        base_id = db.execute("SELECT id FROM articles LIMIT 1").fetchone()[0]
        columns = table_columns(db, "articles")
        body_columns = [
            name
            for name in columns
            if any(token in name.casefold() for token in ("body", "content", "text", "full_text"))
        ]
        assert body_columns, "article fixture contract requires a writable English body field"
        article_ids = []
        for index in range(count):
            stamp = uuid.uuid4().hex[:8]
            headline = f"Acceptance model release {index + 1} {stamp}"
            body = (
                f"{headline} introduces a new language model for software teams. "
                "The release changes how the model handles long documents and code. "
                "The company reported 42 percent lower inference latency in its tests. "
                "Researchers can access the model through a public development API."
            )
            changes = {"created_at": current_time}
            for name in columns:
                folded = name.casefold()
                if folded in {"title", "title_local", "headline"}:
                    changes[name] = headline
                elif any(token in folded for token in ("body", "content", "text", "full_text")):
                    changes[name] = body
                elif folded in {
                    "url",
                    "url_canonical",
                    "canonical_url",
                    "source_url",
                    "link",
                    "guid",
                }:
                    changes[name] = f"https://acceptance.invalid/{stamp}"
                elif folded in {"language", "lang"}:
                    changes[name] = "en"
                elif folded.endswith(("_at", "_date", "_time")) or folded in {
                    "published",
                    "fetched",
                    "created",
                    "updated",
                }:
                    changes[name] = current_time
                elif folded in {"processed_at", "summarized_at", "enriched_at"}:
                    changes[name] = None
            new_id = clone_row(db, "articles", "id", base_id, changes)
            article_ids.append(new_id)
        db.commit()
        return article_ids


def prepare_pipeline(demo):
    root, db_path = demo
    old = "2000-01-01 00:00:00"
    with sqlite3_connect(db_path) as db:
        for table in ("articles", "summaries"):
            for name in table_columns(db, table):
                folded = name.casefold()
                if folded.endswith(("_at", "_date", "_time")) or folded in {
                    "published",
                    "fetched",
                    "created",
                    "updated",
                }:
                    db.execute(f'UPDATE "{table}" SET "{name}" = ?', (old,))
        db.commit()
    article_ids = add_candidate_articles(db_path)
    return root, db_path, article_ids


def provider_server_overrides(standin):
    return {
        "OPENAI_BASE_URL": standin.base_url,
        "OPENAI_MAX_RETRIES": "0",
        "OPENAI_TIMEOUT_SECONDS": "5",
        "TAVILY_API_KEY": "",
        "SCHEDULER_ENABLED": "false",
        "PHOENIX_ENABLED": "false",
        "DEMO_MODE": "false",
    }


def provider_env(root, db_path, standin):
    env = environment(root, db_path)
    env.update(provider_server_overrides(standin))
    return env


def digest(root, db_path, standin, timeout=120):
    return run_cli(
        root, db_path, "digest", env=provider_env(root, db_path, standin), timeout=timeout
    )


def latest_run(db_path):
    with sqlite3_connect(db_path) as db:
        return db.execute(
            "SELECT id, status, est_cost_usd FROM runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()


def published_bulletin(db_path):
    with sqlite3_connect(db_path) as db:
        cols = set(table_columns(db, "bulletins"))
        assert {"bilingual", "editor_note_tr"}.issubset(cols)
        return db.execute(
            "SELECT id, language, bilingual, editor_note, editor_note_tr, run_id "
            "FROM bulletins WHERE bilingual = 1 ORDER BY day DESC, version DESC LIMIT 1"
        ).fetchone()


def bulletin_items(db_path, bulletin_id):
    with sqlite3_connect(db_path) as db:
        return db.execute(
            "SELECT summary_id, position, tier, reason, reason_tr "
            "FROM bulletin_items WHERE bulletin_id = ? ORDER BY position",
            (bulletin_id,),
        ).fetchall()


def archive_page(server, bulletin_id, lang):
    path = f"/archive?b={urllib.parse.quote(str(bulletin_id))}&lang={lang}"
    status, page = server.get(path)
    assert status == 200
    return page


def test_H11_R10_successful_bulletin_has_complete_english_and_turkish_prose(
    demo, standin, server_factory
):
    """H11: R10 — successful publication has matching item identity and complete bilingual prose."""
    root, db_path, _ = prepare_pipeline(demo)
    result = digest(root, db_path, standin)
    assert result.returncode == 0, f"digest failed: {result.stdout}\n{result.stderr}"
    bulletin = published_bulletin(db_path)
    bulletin_id, language, bilingual, editor_en, editor_tr, run_id = bulletin
    assert (language, bool(bilingual)) == ("en", True)
    assert editor_en.strip() and editor_tr.strip()
    items = bulletin_items(db_path, bulletin_id)
    assert items
    ids = [row[0] for row in items]
    with sqlite3_connect(db_path) as db:
        summaries = {
            row[0]: row[1:]
            for row in db.execute(
                "SELECT id, language, title_local, summary, why_it_matters FROM summaries "
                f"WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
        }
        translations = {
            row[0]: row[1:]
            for row in db.execute(
                "SELECT summary_id, title_local, summary, why_it_matters, canonical_hash, "
                "model, tokens_in, tokens_out, est_cost_usd FROM summary_translations "
                "WHERE language = 'tr'"
            ).fetchall()
        }
    for summary_id, _, _, reason_en, reason_tr in items:
        assert summary_id in translations
        language_en, title_en, summary_en, why_en = summaries[summary_id]
        assert language_en == "en"
        title_tr, summary_tr, why_tr, canonical_hash, model, tokens_in, tokens_out, cost = (
            translations[summary_id]
        )
        expected_hash = hashlib.sha256(
            json.dumps(
                [title_en, summary_en, why_en], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        assert canonical_hash == expected_hash
        assert all(value and value.strip() for value in (title_tr, summary_tr, why_tr))
        assert model and tokens_in > 0 and tokens_out > 0 and cost >= 0
        if reason_en and reason_en.strip():
            assert reason_tr and reason_tr.strip()
    server = server_factory(root, db_path, overrides=provider_server_overrides(standin))
    english = archive_page(server, bulletin_id, "en")
    turkish = archive_page(server, bulletin_id, "tr")
    english_markup = StoryMarkup().page(english)
    turkish_markup = StoryMarkup().page(turkish)
    expected_ids = [str(value) for value in ids]
    assert english_markup.story_ids == expected_ids
    assert turkish_markup.story_ids == expected_ids
    for story_id in expected_ids:
        assert english_markup.story_scales[story_id] in {"1", "2", "3", "4", "5"}
        assert turkish_markup.story_scales[story_id] in {"1", "2", "3", "4", "5"}
        assert english_markup.story_scales[story_id] == turkish_markup.story_scales[story_id]
        assert "Türkçe başlık wave2" in " ".join(turkish_markup.story_text[story_id])
    assert "Canonical English" in english
    assert "Türkçe özet wave2" in turkish
    assert "Türkçe editör notu wave2" in turkish
    assert "Türkçe gerekçe" in turkish
    assert run_id is not None


def test_H12_R10_editorial_translation_failure_keeps_previous_publication_and_records_cost(
    demo, standin, server_factory
):
    """H12: R10 — incomplete editorial translation errors the run without publishing a version."""
    root, db_path, _ = prepare_pipeline(demo)
    with sqlite3_connect(db_path) as db:
        before = db.execute(
            "SELECT id, day, version FROM bulletins ORDER BY day DESC, version DESC LIMIT 1"
        ).fetchone()
    standin.state.incomplete_editorial_once = True
    digest(root, db_path, standin)
    run_id, status, cost = latest_run(db_path)
    assert status == "error"
    assert cost is not None and cost > 0
    with sqlite3_connect(db_path) as db:
        after = db.execute(
            "SELECT id, day, version FROM bulletins ORDER BY day DESC, version DESC LIMIT 1"
        ).fetchone()
        translation_count = db.execute("SELECT COUNT(*) FROM summary_translations").fetchone()[0]
        new_items = db.execute(
            "SELECT COUNT(*) FROM bulletin_items bi JOIN bulletins b ON b.id=bi.bulletin_id "
            "WHERE b.run_id = ?",
            (run_id,),
        ).fetchone()[0]
    assert after == before
    assert translation_count > 0
    assert new_items == 0
    server = server_factory(root, db_path, overrides=provider_server_overrides(standin))
    status, detail = server.get(f"/runs/{urllib.parse.quote(str(run_id))}?lang=en")
    assert status == 200
    assert "error" in detail.lower()
    assert "cost" in detail.lower()


def test_H13_R10_retry_reuses_completed_translations_without_summary_rebuy(demo, standin):
    """H13: R10 — retry ranks the English pool and reuses committed translations and spend."""
    root, db_path, _ = prepare_pipeline(demo)
    standin.state.incomplete_editorial_once = True
    digest(root, db_path, standin)
    assert latest_run(db_path)[1] == "error"
    first_requests = standin.state.snapshot()
    assert any(_schema_name(request) == "StoryTranslation" for request in first_requests)
    with sqlite3_connect(db_path) as db:
        summaries_before = db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        translation_before = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(est_cost_usd), 0) FROM summary_translations"
        ).fetchone()
    standin.state.clear()
    retried = digest(root, db_path, standin)
    assert retried.returncode == 0, f"retry failed: {retried.stdout}\n{retried.stderr}"
    retry_requests = standin.state.snapshot()
    assert any(_schema_name(request) == "RankedDigest" for request in retry_requests)
    assert any(_schema_name(request) == "EditorialTranslation" for request in retry_requests)
    assert not any(_schema_name(request) == "ArticleSummary" for request in retry_requests)
    assert not any(_schema_name(request) == "StoryTranslation" for request in retry_requests)
    with sqlite3_connect(db_path) as db:
        summaries_after = db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        translation_after = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(est_cost_usd), 0) FROM summary_translations"
        ).fetchone()
    assert summaries_after == summaries_before
    assert translation_after == translation_before
    assert published_bulletin(db_path)


def _schema_name(request):
    return request.get("response_format", {}).get("json_schema", {}).get("name")


def test_H14_R10_legacy_language_and_uncached_english_fallback_remain_honest(demo, server_factory):
    """H14: R10 — legacy prose remains readable and uncached English stays labelled."""
    root, db_path = demo
    with sqlite3_connect(db_path) as db:
        bulletin_id, day, language = latest_bulletin(db)
        db.execute(
            "UPDATE bulletins SET bilingual = 0, editor_note_tr = NULL WHERE id = ?", (bulletin_id,)
        )
        for name in table_columns(db, "bulletin_items"):
            if name == "reason_tr":
                db.execute(
                    "UPDATE bulletin_items SET reason_tr = NULL WHERE bulletin_id = ?",
                    (bulletin_id,),
                )
        db.execute("DELETE FROM summary_translations")
        db.commit()
    with sqlite3_connect(db_path) as db:
        has_english = (
            db.execute("SELECT 1 FROM summaries WHERE language = 'en' LIMIT 1").fetchone()
            is not None
        )
    uncached_id = clone_article_summary(
        db_path,
        tag="legacy-fallback-story",
        relevant=True,
        created_at=f"{day}T12:00:00+00:00",
        title="Legacy English fallback story",
        language="en" if has_english else None,
    )
    with sqlite3_connect(db_path) as db:
        db.execute(
            "UPDATE summaries SET language='en', title_local=?, summary=?, why_it_matters=? "
            "WHERE id = ?",
            (
                "Legacy English fallback story",
                "An uncached English summary for the acceptance fixture.",
                "This English prose has no Turkish translation cache.",
                uncached_id,
            ),
        )
        db.commit()
    server = server_factory(root, db_path)
    en_status, en_page = server.get(f"/archive?b={bulletin_id}&lang=en")
    tr_status, tr_page = server.get(f"/archive?b={bulletin_id}&lang=tr")
    assert en_status == tr_status == 200
    assert StoryMarkup().page(en_page).story_ids == StoryMarkup().page(tr_page).story_ids
    true_label = "Turkish" if language == "tr" else "English"
    alternate = "Türkçe" if language == "tr" else "İngilizce"
    for page in (en_page, tr_page):
        markup = StoryMarkup().page(page)
        for story_id in markup.story_ids:
            card_text = " ".join(markup.story_text[story_id]).casefold()
            assert true_label.casefold() in card_text or alternate.casefold() in card_text
    status, page = server.get("/?all=1&lang=tr")
    assert status == 200
    fallback_markup = StoryMarkup().page(page)
    assert str(uncached_id) in fallback_markup.story_ids
    fallback_text = " ".join(fallback_markup.story_text[str(uncached_id)]).casefold()
    assert "english" in fallback_text or "ingilizce" in fallback_text


def test_H15_R10_turkish_search_returns_canonical_story_once(demo, standin, server_factory):
    """H15: R10 — Turkish prose search returns the canonical story identity once."""
    root, db_path, _ = prepare_pipeline(demo)
    result = digest(root, db_path, standin)
    assert result.returncode == 0, f"digest failed: {result.stdout}\n{result.stderr}"
    bulletin = published_bulletin(db_path)
    bulletin_id = bulletin[0]
    summary_id = bulletin_items(db_path, bulletin_id)[0][0]
    with sqlite3_connect(db_path) as db:
        term = db.execute(
            "SELECT title_local FROM summary_translations WHERE summary_id = ? AND language='tr'",
            (summary_id,),
        ).fetchone()[0]
    server = server_factory(root, db_path, overrides=provider_server_overrides(standin))
    status, page = server.get("/search?q=" + urllib.parse.quote(term) + "&lang=tr")
    assert status == 200
    ids = StoryMarkup().page(page).story_ids
    assert ids.count(str(summary_id)) == 1


def test_H16_R3_R10_topic_counts_and_membership_match_in_both_languages(
    demo, standin, server_factory
):
    """H16: R3/R10 — topic counts and visible identity are language-independent."""
    root, db_path, _ = prepare_pipeline(demo)
    result = digest(root, db_path, standin)
    assert result.returncode == 0, f"digest failed: {result.stdout}\n{result.stderr}"
    server = server_factory(root, db_path, overrides=provider_server_overrides(standin))
    views = {}
    counts = {}
    for language in ("en", "tr"):
        status, page = server.get(f"/?all=1&lang={language}")
        assert status == 200
        markup = StoryMarkup().page(page)
        views[language] = set(markup.story_ids)
        counted = {}
        for link in markup.tag_links:
            tag = markup.tag_value(link)
            digits = re.findall(r"\d+", html.unescape(link["text"]))
            if tag and digits:
                counted[tag] = int(digits[-1])
        counts[language] = counted
    assert views["en"] == views["tr"]
    assert counts["en"] == counts["tr"]
    assert "acceptance-wave-topic" in counts["en"]
    for language in ("en", "tr"):
        tag = "acceptance-wave-topic"
        status, filtered = server.get(f"/?all=1&lang={language}&tag={urllib.parse.quote(tag)}")
        assert status == 200
        filtered_ids = StoryMarkup().page(filtered).story_ids
        assert len(filtered_ids) == counts[language][tag]


def test_H17_R10_reads_do_not_call_the_provider(demo, standin, server_factory):
    """H17: R10 — dashboard, archive, search and run reads do not translate or spend."""
    root, db_path, _ = prepare_pipeline(demo)
    result = digest(root, db_path, standin)
    assert result.returncode == 0, f"digest failed: {result.stdout}\n{result.stderr}"
    bulletin_id = published_bulletin(db_path)[0]
    run_id = latest_run(db_path)[0]
    standin_calls = len(standin.state.snapshot())
    server = server_factory(root, db_path, overrides=provider_server_overrides(standin))
    for route in (
        "/?lang=en",
        "/?all=1&lang=tr",
        f"/archive?b={bulletin_id}&lang=tr",
        "/search?q=" + urllib.parse.quote("Türkçe özet") + "&lang=tr",
        f"/runs/{urllib.parse.quote(str(run_id))}?lang=en",
    ):
        status, _ = server.get(route)
        assert status == 200
    assert len(standin.state.snapshot()) == standin_calls


def test_H18_R10_additive_migration_preserves_seeded_legacy_demo_history(demo):
    """H18: R10 — schema-0008 fixture upgrades additively and keeps legacy demo rows."""
    root, db_path = demo
    with sqlite3_connect(db_path) as db:
        history = {
            table: _snapshot_table(db, table, exclude=excluded)
            for table, excluded in {
                "summaries": (),
                "runs": (),
                "bulletins": ("bilingual", "editor_note_tr"),
                "bulletin_items": ("reason_tr",),
                "verdicts": (),
            }.items()
        }
        db.execute("DROP TABLE summary_translations")
        db.execute("ALTER TABLE bulletins DROP COLUMN bilingual")
        db.execute("ALTER TABLE bulletins DROP COLUMN editor_note_tr")
        db.execute("ALTER TABLE bulletin_items DROP COLUMN reason_tr")
        db.execute("UPDATE alembic_version SET version_num = '0008_published_checks'")
        db.commit()
    upgraded = run_cli(root, db_path, "sources", env=environment(root, db_path))
    assert upgraded.returncode == 0, f"schema upgrade failed: {upgraded.stdout}\n{upgraded.stderr}"
    with sqlite3_connect(db_path) as db:
        assert {"bilingual", "editor_note_tr"}.issubset(set(table_columns(db, "bulletins")))
        assert "reason_tr" in table_columns(db, "bulletin_items")
        assert "summary_translations" in {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, expected in history.items():
            assert (
                _snapshot_table(
                    db,
                    table,
                    exclude={
                        "bulletins": ("bilingual", "editor_note_tr"),
                        "bulletin_items": ("reason_tr",),
                    }.get(table, ()),
                )
                == expected
            )
        assert db.execute("SELECT COUNT(*) FROM summary_translations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM bulletins WHERE bilingual != 0").fetchone()[0] == 0


def _snapshot_table(db, table, exclude=()):
    columns = [name for name in table_columns(db, table) if name not in set(exclude)]
    selected = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
    rows = db.execute(f'SELECT {selected} FROM "{table}" ORDER BY rowid').fetchall()
    return columns, rows


def test_H19_R10_demo_export_preserves_bilingual_fields(demo):
    """H19: R10 — public demo export retains translated prose and bilingual columns."""
    root, db_path = demo
    with sqlite3_connect(db_path) as db:
        bulletin_id, day, _ = latest_bulletin(db)
        item = db.execute(
            "SELECT summary_id FROM bulletin_items WHERE bulletin_id = ? ORDER BY position LIMIT 1",
            (bulletin_id,),
        ).fetchone()
        assert item is not None
        summary_id = item[0]
        summary = db.execute(
            "SELECT title_local, summary, why_it_matters FROM summaries WHERE id = ?",
            (summary_id,),
        ).fetchone()
        canonical_hash = hashlib.sha256(
            json.dumps(list(summary), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        db.execute(
            "INSERT INTO summary_translations "
            "(summary_id, language, title_local, summary, why_it_matters, canonical_hash, "
            "model, tokens_in, tokens_out, est_cost_usd, created_at) "
            "VALUES (?, 'tr', ?, ?, ?, ?, 'acceptance-standin', 3, 4, 0.001, ?)",
            (
                summary_id,
                "Dışa aktarılan Türkçe başlık",
                "Dışa aktarılan Türkçe özet",
                "Dışa aktarılan Türkçe önem",
                canonical_hash,
                f"{day}T12:00:00+00:00",
            ),
        )
        db.execute(
            "UPDATE bulletins SET bilingual = 1, editor_note_tr = ? WHERE id = ?",
            ("Dışa aktarılan editör notu", bulletin_id),
        )
        db.execute(
            "UPDATE bulletin_items SET reason_tr = ? WHERE bulletin_id = ? AND summary_id = ?",
            ("Dışa aktarılan gerekçe", bulletin_id, summary_id),
        )
        db.commit()
    exported = db_path.parent / "bilingual-demo-export.json"
    result = run_cli(
        root,
        db_path,
        "demo",
        "export",
        "--out",
        str(exported),
        env=environment(root, db_path, demo=True),
    )
    assert result.returncode == 0, f"demo export failed: {result.stdout}\n{result.stderr}"
    assert exported.is_file()
    artifact = json.loads(exported.read_text(encoding="utf-8"))
    serialized = json.dumps(artifact, ensure_ascii=False)
    for expected in (
        "Dışa aktarılan Türkçe başlık",
        "Dışa aktarılan Türkçe özet",
        "Dışa aktarılan Türkçe önem",
        "Dışa aktarılan editör notu",
        "Dışa aktarılan gerekçe",
        "canonical_hash",
        "bilingual",
        "reason_tr",
    ):
        assert expected in serialized
