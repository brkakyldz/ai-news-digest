"""Independent HTTP/CLI acceptance scenarios for the October issue contract."""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
import uuid
from html.parser import HTMLParser
from pathlib import Path

import pytest
from acceptance_helpers import (
    clone_article_summary,
    clone_row,
    insert_record,
    latest_bulletin,
    primary_key,
    row_dict,
    run_cli,
    set_all_zero,
    set_matching_dates,
    sqlite3_connect,
    table_columns,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("AINEWS_RUN_ACCEPTANCE") != "1",
    reason="integrated acceptance holdout runs only through run_acceptance.py",
)


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.story_ids = []
        self.tag_links = []
        self.model_buttons = []
        self._anchor = None
        self._story_attrs = None
        self._story_controls = {}
        self.story_controls = {}
        self._story_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ident = attrs.get("id", "")
        if ident.startswith("story-"):
            self.story_ids.append(ident.removeprefix("story-"))
            self._story_attrs = ident
            self._story_controls[ident] = []
            self._story_depth = 1
        elif self._story_depth:
            self._story_depth += 1
        if tag == "a" and "tag=" in attrs.get("href", ""):
            self._anchor = {"href": attrs["href"], "text": ""}
        if tag == "button" and attrs.get("title"):
            self.model_buttons.append(attrs)
        if self._story_depth and tag in {"button", "input", "select"}:
            self._story_controls.setdefault(self._story_attrs, []).append(attrs)

    def handle_endtag(self, tag):
        if self._anchor and tag == "a":
            self.tag_links.append(self._anchor)
            self._anchor = None
        if self._story_depth:
            self._story_depth -= 1
            if self._story_depth == 0:
                self._story_attrs = None

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["text"] += data

    def feed_page(self, page):
        self.feed(page)
        self.story_controls = self._story_controls
        return self

    @staticmethod
    def query_tags(link):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(link["href"]).query).get("tag", [])


def add_fixture_bulletin(db, base_bulletin_id, run_id, *, note, agreement):
    max_version = db.execute("SELECT MAX(version) FROM bulletins").fetchone()[0] or 0
    new_id = clone_row(
        db,
        "bulletins",
        "id",
        base_bulletin_id,
        {
            "run_id": run_id,
            "editor_note": note,
            "agreement": agreement,
            "est_cost_usd": 0.0,
            "version": max_version + 1,
        },
    )
    item_pk = primary_key(db, "bulletin_items")
    item_id, summary_id = db.execute(
        f'SELECT "{item_pk}", summary_id FROM bulletin_items WHERE bulletin_id = ? LIMIT 1',
        (base_bulletin_id,),
    ).fetchone()
    clone_row(
        db,
        "bulletin_items",
        item_pk,
        item_id,
        {"bulletin_id": new_id, "summary_id": summary_id},
    )
    return new_id, summary_id


def report(root: Path, db: Path, output: Path, *extra: str):
    return run_cli(root, db, "eval", "report", "--since", "30d", *extra, "--out", str(output))


def spend_is_108(markdown: str):
    assert re.search(r"Product spend\s+\$0\.1080", markdown, re.IGNORECASE)
    assert re.search(r"eval spend\s+\$0\.0000", markdown, re.IGNORECASE)


def ensure_legacy_eval_row(db, run_id):
    info = db.execute('PRAGMA table_info("eval_results")').fetchall()
    columns = {row[1]: row for row in info}
    for name in columns:
        if name.casefold() in {"kind", "type", "name", "metric", "eval_type"}:
            existing = db.execute(
                f'SELECT 1 FROM eval_results WHERE "{name}" LIKE ? LIMIT 1',
                ("%rank_stability%",),
            ).fetchone()
            if existing:
                return
    record = {}
    for name, declared, not_null, default, pk in (
        (row[1], row[2].upper(), row[3], row[4], row[5]) for row in info
    ):
        folded = name.casefold()
        if pk:
            record[name] = "legacy-" + uuid.uuid4().hex if "INT" not in declared else 900000001
        elif default is not None or not not_null:
            continue
        elif folded in {"kind", "type", "name", "metric", "eval_type"}:
            record[name] = "rank_stability"
        elif "json" in folded or "payload" in folded or "result" in folded:
            record[name] = "{}"
        elif folded.endswith(("_at", "_date")):
            record[name] = "2026-10-23 10:00:00"
        elif folded in {"run_id", "run"}:
            record[name] = run_id
        elif "INT" in declared or "REAL" in declared or "NUM" in declared:
            record[name] = 0
        else:
            record[name] = "legacy-rank-stability"
    assert record, "eval_results schema must allow a legacy row fixture"
    insert_record(db, "eval_results", record)


def test_H01_R2_invalid_timezone_fallback_keeps_dashboard_and_health(demo, server_factory):
    """H01: R2 — scheduler with invalid timezone still serves health and dashboard."""
    root, db_path = demo
    server = server_factory(root, db_path, timezone="Europe/Istambul", scheduler=True)
    assert server.get("/health")[0] == 200
    status, page = server.get("/")
    assert status == 200
    assert page.strip()


def test_H02_R3_topic_pills_count_visible_relevant_stories(demo, server_factory):
    """H02: R3 — topic counts match relevant filtered story IDs; irrelevant tags vanish."""
    root, db_path = demo
    with sqlite3_connect(db_path) as db:
        _, day, language = latest_bulletin(db)
        same_day_rows = db.execute(
            "SELECT id FROM summaries WHERE relevant = 1 AND language = ? "
            "AND substr(created_at, 1, 10) = ?",
            (language, day),
        ).fetchall()
        for (summary_id,) in same_day_rows:
            db.execute(
                "UPDATE summaries SET tags_json = ? WHERE id = ?",
                (json.dumps(["acceptance-visible-tag"]), summary_id),
            )
        db.commit()
    stamp = f"{day}T12:00:00+00:00"
    relevant_ids = [
        clone_article_summary(
            db_path,
            tag="acceptance-visible-tag",
            relevant=True,
            created_at=stamp,
            title=f"Acceptance relevant topic story {index}",
            language=language,
        )
        for index in range(8)
    ]
    irrelevant_id = clone_article_summary(
        db_path,
        tag="acceptance-phantom-tag",
        relevant=False,
        created_at=stamp,
        title="Acceptance irrelevant phantom story",
        language=language,
    )
    server = server_factory(root, db_path)
    status, all_page = server.get("/?all=1")
    assert status == 200
    all_markup = Markup().feed_page(all_page)
    assert str(irrelevant_id) not in all_markup.story_ids
    assert "acceptance-phantom-tag" not in {
        tag for link in all_markup.tag_links for tag in all_markup.query_tags(link)
    }
    filtered_status, filtered = server.get("/?all=1&tag=acceptance-visible-tag")
    assert filtered_status == 200
    filtered_markup = Markup().feed_page(filtered)
    assert {str(summary_id) for summary_id in relevant_ids}.issubset(set(filtered_markup.story_ids))
    counted_links = [
        link
        for link in all_markup.tag_links
        if "acceptance-visible-tag" in all_markup.query_tags(link)
        and re.search(r"\d+", html.unescape(link["text"]))
    ]
    assert counted_links, "the visible topic pill must expose its count"
    tag_link = counted_links[0]
    counts = re.findall(r"\d+", html.unescape(tag_link["text"]))
    assert counts and int(counts[-1]) == len(filtered_markup.story_ids)


def test_H03_R4_product_spend_is_per_run_and_bulletin_only_limits_detail(demo, tmp_path):
    """H03: R4 — run costs sum to 0.1080 once; bulletin selection leaves window spend intact."""
    root, db_path = demo
    with sqlite3_connect(db_path) as db:
        set_all_zero(db, "runs", ("est_cost_usd",))
        set_all_zero(db, "bulletins", ("est_cost_usd",))
        set_all_zero(db, "eval_results", ("est_cost_usd", "cost_usd"))
        base_run_id = db.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
        now = "2026-10-23 10:00:00"
        run_a = clone_row(
            db,
            "runs",
            "id",
            base_run_id,
            {
                "kind": "digest",
                "status": "ok",
                "est_cost_usd": 0.104,
                "created_at": now,
            },
        )
        run_b = clone_row(
            db,
            "runs",
            "id",
            base_run_id,
            {
                "kind": "digest",
                "status": "ok",
                "est_cost_usd": 0.004,
                "created_at": now,
            },
        )
        base_bulletin_id, _, _ = latest_bulletin(db)
        bulletin_a, _ = add_fixture_bulletin(
            db,
            base_bulletin_id,
            run_a,
            note="acceptance repeated version A",
            agreement=0.82,
        )
        bulletin_b, _ = add_fixture_bulletin(
            db,
            base_bulletin_id,
            run_b,
            note="acceptance repeated version B",
            agreement=0.91,
        )
        assert bulletin_a != bulletin_b
        db.commit()
    all_path = tmp_path / "all-report.md"
    selected_path = tmp_path / "one-bulletin-report.md"
    result = report(root, db_path, all_path)
    assert result.returncode == 0, result.stderr
    selected = report(root, db_path, selected_path, "--bulletin", str(bulletin_a))
    assert selected.returncode == 0, selected.stderr
    all_text = all_path.read_text(encoding="utf-8")
    selected_text = selected_path.read_text(encoding="utf-8")
    spend_is_108(all_text)
    spend_is_108(selected_text)
    assert "agreement 0.82" in all_text.lower()
    assert "agreement 0.91" in all_text.lower()
    assert "agreement 0.82" in selected_text.lower()
    assert "agreement 0.91" not in selected_text.lower()
    assert len(re.findall(r"^### Bulletin\b", all_text, re.MULTILINE)) > len(
        re.findall(r"^### Bulletin\b", selected_text, re.MULTILINE)
    )


def test_H04_R5_each_persisted_running_kind_refuses_prune_and_dry_run(demo):
    """H04: R5 — persisted running collect and digest independently block both prune forms."""
    root, db_path = demo
    for kind in ("collect", "digest"):
        with sqlite3_connect(db_path) as db:
            base_id = db.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
            run_id = clone_row(
                db,
                "runs",
                "id",
                base_id,
                {
                    "kind": kind,
                    "status": "running",
                    "created_at": "2026-10-23 10:00:00",
                },
            )
            db.commit()
            before = {
                table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in ("articles", "summaries", "bulletins", "bulletin_items")
            }
        for args in (("prune",), ("prune", "--dry-run")):
            result = run_cli(root, db_path, *args)
            assert result.returncode != 0, f"{kind} should block {args}"
        with sqlite3_connect(db_path) as db:
            after = {
                table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in before
            }
            assert after == before
            db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            db.commit()


def test_H05_R5_successful_prune_deletes_only_old_unsummarized_article(demo):
    """H05: R5 — real prune deletes old unrepresented article and preserves durable history."""
    root, db_path = demo
    with sqlite3_connect(db_path) as db:
        article_id = db.execute("SELECT id FROM articles LIMIT 1").fetchone()[0]
        old_article = row_dict(db, "articles", "id", article_id)
        new_id = clone_row(db, "articles", "id", article_id, {})
        old_article = row_dict(db, "articles", "id", new_id)
        for name in tuple(old_article):
            if name.casefold() in {"url", "link", "guid", "external_id", "slug"}:
                old_article[name] = "https://acceptance.invalid/old-unread-article"
        set_matching_dates(old_article, "1999-01-01 00:00:00")
        pk = primary_key(db, "articles")
        update_values = tuple(old_article[k] for k in old_article if k != pk)
        db.execute(
            "UPDATE articles SET "
            + ", ".join(f'"{k}" = ?' for k in old_article if k != pk)
            + f' WHERE "{pk}" = ?',
            (*update_values, new_id),
        )
        baseline = {
            table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "summaries",
                "bulletins",
                "bulletin_items",
                "verdicts",
                "eval_results",
            )
        }
        db.commit()
    with sqlite3_connect(db_path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM summaries WHERE article_id = ?", (new_id,)).fetchone()[
                0
            ]
            == 0
        )
    result = run_cli(root, db_path, "prune")
    assert result.returncode == 0, result.stderr
    with sqlite3_connect(db_path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM articles WHERE id = ?", (new_id,)).fetchone()[0] == 0
        )
        after = {
            table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in baseline
        }
    assert after == baseline


def test_H06_R7_removed_rank_stability_keeps_legacy_rows_and_n_a_agreement_visible(
    demo,
    tmp_path,
    server_factory,
):
    """H06: R7 — removed command fails without changing history; missing agreement is visible."""
    root, db_path = demo
    with sqlite3_connect(db_path) as db:
        base_run_id = db.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
        run_id = clone_row(
            db,
            "runs",
            "id",
            base_run_id,
            {
                "kind": "digest",
                "status": "ok",
                "est_cost_usd": 0.0,
                "created_at": "2026-10-23 10:00:00",
            },
        )
        ensure_legacy_eval_row(db, run_id)
        base_bulletin_id, _, _ = latest_bulletin(db)
        bulletin_id, _ = add_fixture_bulletin(
            db,
            base_bulletin_id,
            run_id,
            note="acceptance agreement absent",
            agreement=None,
        )
        eval_before = db.execute("SELECT COUNT(*) FROM eval_results").fetchone()[0]
        db.commit()
    help_result = run_cli(root, db_path, "eval", "--help")
    assert help_result.returncode == 0
    assert "rank-stability" not in (help_result.stdout + help_result.stderr).lower()
    removed = run_cli(root, db_path, "eval", "rank-stability")
    assert removed.returncode != 0
    with sqlite3_connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM eval_results").fetchone()[0] == eval_before
    report_path = tmp_path / "agreement.md"
    result = report(root, db_path, report_path, "--bulletin", str(bulletin_id))
    assert result.returncode == 0, result.stderr
    report_text = report_path.read_text(encoding="utf-8")
    assert "agreement n/a" in report_text.lower()
    server = server_factory(root, db_path)
    status, detail = server.get(f"/runs/{urllib.parse.quote(str(run_id))}?lang=en")
    assert status == 200
    assert "agreement" in detail.lower()
    assert "n/a" in detail.lower() or "—" in detail


def test_H07_R8_only_wrong_feedback_is_accepted_and_old_ok_stays_inactive(demo, server_factory):
    """H07: R8 — feedback validation, repeated annotations, and legacy ok storage/UI semantics."""
    root, db_path = demo
    with sqlite3_connect(db_path) as db:
        item = db.execute(
            "SELECT bi.summary_id FROM bulletin_items bi "
            "WHERE NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.summary_id = bi.summary_id) LIMIT 1"
        ).fetchone()
        assert item
        summary_id = item[0]
    old_summary_id = clone_article_summary(
        db_path,
        tag="legacy-ok-story",
        relevant=True,
        created_at="2026-10-23 12:00:00",
        title="Legacy ok acceptance story",
    )
    with sqlite3_connect(db_path) as db:
        columns = table_columns(db, "verdicts")
        record = {
            "summary_id": old_summary_id,
            "verdict": "ok",
            "reason": None,
            "note": None,
            "created_at": "2026-10-23 12:00:00",
        }
        record = {key: value for key, value in record.items() if key in columns}
        insert_record(db, "verdicts", record)
        db.commit()
    server = server_factory(root, db_path)
    assert server.post("/verdict", {"summary_id": str(summary_id), "verdict": "ok"})[0] == 422
    assert (
        server.post(
            "/verdict",
            {
                "summary_id": str(summary_id),
                "verdict": "wrong",
                "reason": "invented_reason",
            },
        )[0]
        == 422
    )
    for reason in ("wrong_fact", "not_news", "duplicate", "wrong_place"):
        status, _ = server.post(
            "/verdict",
            {
                "summary_id": str(summary_id),
                "verdict": "wrong",
                "reason": reason,
                "note": "acceptance note",
            },
        )
        assert status == 200
    assert server.post("/verdict", {"summary_id": str(summary_id), "verdict": "wrong"})[0] == 200
    with sqlite3_connect(db_path) as db:
        verdict, reason, note = db.execute(
            "SELECT verdict, reason, note FROM verdicts WHERE summary_id = ?",
            (summary_id,),
        ).fetchone()
        legacy = db.execute(
            "SELECT verdict FROM verdicts WHERE summary_id = ?", (old_summary_id,)
        ).fetchone()
    assert (verdict, reason, note) == ("wrong", "wrong_place", "acceptance note")
    assert legacy == ("ok",)
    status, page = server.get("/?all=1")
    assert status == 200
    markup = Markup().feed_page(page)
    controls = markup.story_controls.get(f"story-{old_summary_id}", [])
    assert not any(
        control.get("name") == "verdict" and control.get("value") == "ok" for control in controls
    )


def test_H08_R9_report_and_run_detail_keep_actionable_quality_only(demo, tmp_path, server_factory):
    """H08: R9 — report and run detail retain the keep-list and drop retired diagnostics."""
    root, db_path = demo
    feedback_ids = [
        clone_article_summary(
            db_path,
            tag=f"quality-{category}",
            relevant=True,
            created_at="2026-10-23 12:00:00",
            title=f"Quality {category} fixture",
        )
        for category in ("wrong_fact", "not_news", "duplicate", "wrong_place")
    ]
    with sqlite3_connect(db_path) as db:
        run_id = db.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
        bulletin_id, _, _ = latest_bulletin(db)
        db.execute("UPDATE bulletins SET agreement = NULL WHERE id = ?", (bulletin_id,))
        bulletin_run_id = db.execute(
            "SELECT run_id FROM bulletins WHERE id = ?", (bulletin_id,)
        ).fetchone()[0]
        if bulletin_run_id is not None:
            run_id = bulletin_run_id
        # Give the report deterministic recent wrong-feedback categories.
        for summary_id, category in zip(
            feedback_ids,
            ("wrong_fact", "not_news", "duplicate", "wrong_place"),
            strict=True,
        ):
            verdict_columns = table_columns(db, "verdicts")
            verdict = {
                "summary_id": summary_id,
                "verdict": "wrong",
                "reason": category,
                "note": None,
                "created_at": "2026-10-23 12:00:00",
            }
            insert_record(
                db,
                "verdicts",
                {key: value for key, value in verdict.items() if key in verdict_columns},
            )
        db.commit()
    report_path = tmp_path / "quality.md"
    result = report(root, db_path, report_path)
    assert result.returncode == 0, result.stderr
    text = report_path.read_text(encoding="utf-8")
    keep = (
        "Key fact named",
        "kept in the writing",
        "A body figure survives into the summary",
        "Ungrounded numerals",
        "Summaries over 55 words",
        "Why-it-matters over 20 words",
        "Titles over 10 words",
        "tag",
        "agreement",
        "Product spend",
        "eval spend",
        "wrong_fact",
        "not_news",
        "duplicate",
        "wrong_place",
    )
    forbidden = (
        "sentence histogram",
        "importance distribution",
        "fallback overlap",
        "unrepresented-five",
        "tier/note shape",
        "rank experiment",
        "precision",
        "TNR",
    )
    for label in keep:
        assert label.casefold() in text.casefold(), f"missing report output: {label}"
    for label in forbidden:
        assert label.casefold() not in text.casefold(), f"retired output remains: {label}"
    server = server_factory(root, db_path)
    status, detail = server.get(f"/runs/{urllib.parse.quote(str(run_id))}?lang=en")
    assert status == 200
    detail_text = html.unescape(re.sub(r"<[^>]+>", " ", detail))
    for label in ("Key fact", "numeral", "word", "tag", "agreement", "cost", "wrong"):
        assert label.casefold() in detail_text.casefold()
    for label in forbidden:
        assert label.casefold() not in detail_text.casefold()


def test_H09_R11_explicit_model_choices_select_summarize_and_rank(demo, server_factory):
    """H09: R11 — confirmation marks the supported explicit summarize and rank override."""
    root, db_path = demo
    server = server_factory(root, db_path)
    status, page = server.get("/runs/confirm?ms=gpt-5.6-terra&mr=gpt-5.6-terra")
    assert status == 200
    buttons = Markup().feed_page(page).model_buttons
    selected = [button for button in buttons if button.get("aria-current") == "true"]
    assert [button.get("title") for button in selected].count("gpt-5.6-terra") >= 2


def test_H10_R11_confirmation_override_is_selected_without_creating_run(demo, server_factory):
    """H10: R11 — explicit model choice is visible and confirmation GET has no paid side effect."""
    root, db_path = demo
    with sqlite3_connect(db_path) as db:
        before = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    server = server_factory(root, db_path)
    status, page = server.get("/runs/confirm?ms=gpt-5.6-terra&mr=gpt-5.6-terra")
    assert status == 200
    buttons = Markup().feed_page(page).model_buttons
    selected = [button for button in buttons if button.get("aria-current") == "true"]
    assert [button.get("title") for button in selected].count("gpt-5.6-terra") >= 2
    with sqlite3_connect(db_path) as db:
        after = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert after == before
