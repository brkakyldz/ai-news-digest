from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import day_bounds
from ainews.config import PROJECT_ROOT, Settings
from ainews.db import Bulletin, BulletinItem, EvalResult, Run, Summary, Verdict
from ainews.db.models import utcnow
from ainews.quality.stories import day_stories, published_checks

DEFAULT_PATH = PROJECT_ROOT / "docs" / "evals.md"
HEADER = """# Evaluation record

One dated section per `ainews eval report` invocation, appended and never
edited. New sections contain the product checks and costs that can guide the
next press; older sections remain the record of what was measured at the time.
"""


@dataclass(slots=True)
class BulletinReport:
    bulletin_id: int
    day: str
    version: int
    language: str
    agreement: float | None
    at_publish: bool
    n_stories: int
    n_ranked: int
    budget: dict[str, Any]
    content: dict[str, Any]
    ungrounded: list[tuple[int, list[str]]]
    tags: dict[str, Any]
    verdicts: dict[str, int]


@dataclass(slots=True)
class Report:
    since_days: int
    generated_at: str
    bulletins: list[BulletinReport] = field(default_factory=list)
    product_cost: float = 0.0
    product_runs: int = 0
    eval_cost: float = 0.0


def parse_since(text: str) -> int:
    import re

    match = re.fullmatch(r"\s*(\d+)\s*d?\s*", text)
    if not match:
        raise ValueError(f"--since wants a number of days like '30d', not {text!r}")
    return int(match.group(1))


def _stored_checks(bulletin: Bulletin) -> tuple[dict[str, Any] | None, bool]:
    if not bulletin.checks_json:
        return None, False
    try:
        return json.loads(bulletin.checks_json), True
    except json.JSONDecodeError:
        return None, False


async def _verdict_counts(session: AsyncSession, bulletin: Bulletin) -> dict[str, int]:
    """Count wrong feedback for this published day; legacy `ok` rows are inert."""
    start, end = day_bounds(bulletin.day)
    in_bulletin = (
        select(BulletinItem.id)
        .where(BulletinItem.summary_id == Summary.id)
        .where(BulletinItem.bulletin_id == bulletin.id)
        .exists()
    )
    rows = (
        await session.execute(
            select(Verdict.reason, func.count())
            .join(Summary, Summary.id == Verdict.summary_id)
            .where(Verdict.verdict == "wrong")
            .where(Summary.language == bulletin.language)
            .where(in_bulletin | ((Summary.created_at >= start) & (Summary.created_at < end)))
            .group_by(Verdict.reason)
        )
    ).all()
    counts = {"wrong": 0, "wrong_fact": 0, "not_news": 0, "duplicate": 0, "wrong_place": 0}
    for reason, n in rows:
        counts["wrong"] += int(n)
        if reason in counts:
            counts[reason] += int(n)
    return counts


async def build_report(
    session: AsyncSession,
    since_days: int = 30,
    settings: Settings | None = None,
    bulletin_id: int | None = None,
) -> Report:
    """Report bulletin checks while measuring spend once per recorded run."""
    horizon = utcnow() - timedelta(days=since_days)
    report = Report(since_days=since_days, generated_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"))

    runs = (
        await session.execute(
            select(func.count(Run.id), func.coalesce(func.sum(Run.est_cost_usd), 0.0))
            .where(Run.kind == "digest")
            .where(Run.started_at >= horizon)
        )
    ).one()
    report.product_runs = int(runs[0])
    report.product_cost = float(runs[1])
    report.eval_cost = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(EvalResult.est_cost_usd), 0.0)).where(
                    EvalResult.created_at >= horizon
                )
            )
        ).scalar_one()
    )

    query = (
        select(Bulletin)
        .where(Bulletin.created_at >= horizon)
        .order_by(Bulletin.day.desc(), Bulletin.version.desc())
    )
    if bulletin_id is not None:
        query = query.where(Bulletin.id == bulletin_id)
    bulletins = list((await session.execute(query)).scalars())
    for bulletin in bulletins:
        stored, at_publish = _stored_checks(bulletin)
        if stored is None:
            stored = published_checks(await day_stories(session, bulletin), bulletin.editor_note)
        report.bulletins.append(
            BulletinReport(
                bulletin_id=bulletin.id,
                day=bulletin.day,
                version=bulletin.version,
                language=bulletin.language,
                agreement=bulletin.agreement,
                at_publish=at_publish,
                n_stories=stored["n"],
                n_ranked=stored["n_published"],
                budget=stored["budget"],
                content=stored["content"],
                ungrounded=[(row["article_id"], row["numerals"]) for row in stored["ungrounded"]],
                tags=stored["tags"],
                verdicts=await _verdict_counts(session, bulletin),
            )
        )
    return report


# -- rendering ----------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_bulletin(run: BulletinReport) -> list[str]:
    budget = run.budget
    version = "" if run.version == 1 else f" v{run.version}"
    agreement = "n/a" if run.agreement is None else f"{run.agreement:.2f}"
    lines = [
        f"### Bulletin {run.day}{version} · {run.language} · "
        f"{run.n_ranked} published of {run.n_stories} summarised · agreement {agreement}",
        "",
        (
            "Checks as the press ran them."
            if run.at_publish
            else "Checks recomputed now over an older bulletin."
        ),
        "",
        "| Measure | Value | Function |",
        "|---|---|---|",
        f"| Key fact named | {_pct(run.content['key_fact_share'])} of {run.content['n']} "
        f"| `checks.content_floor` |",
        f"| \u21b3 kept in the writing | {_pct(run.content['key_fact_kept'])} "
        f"| `checks.content_floor` |",
        f"| A body figure survives into the summary | {_pct(run.content['numeral_recall'])} "
        f"of {run.content['n_with_numerals']} | `checks.content_floor` |",
        f"| Ungrounded numerals | {len(run.ungrounded)} story(ies): "
        f"{', '.join(f'{aid} {nums}' for aid, nums in run.ungrounded) or 'none'} "
        f"| `checks.ungrounded_numerals` |",
        f"| Summaries over 55 words | {budget['over_count']['summary']}/{run.n_stories} "
        f"({_pct(budget['over_share']['summary'])}) | `checks.word_budget` |",
        f"| Why-it-matters over 20 words | {budget['over_count']['why_it_matters']} "
        f"| `checks.word_budget` |",
        f"| Titles over 10 words | {budget['over_count']['title_local']} | `checks.word_budget` |",
        f"| Tag singleton share | {_pct(run.tags['singleton_share'])} of {run.tags['distinct']} "
        f"| `checks.tag_vocabulary` |",
        f"| Tags from the preferred vocabulary | {_pct(run.tags['in_vocabulary_share'])} of uses "
        f"| `checks.tag_vocabulary` |",
        f"| Categorized wrong feedback | {run.verdicts['wrong']} total · "
        f"wrong_fact {run.verdicts['wrong_fact']} · not_news {run.verdicts['not_news']} · "
        f"duplicate {run.verdicts['duplicate']} · wrong_place {run.verdicts['wrong_place']} "
        f"| `Verdict.reason` |",
    ]
    return lines


def render_markdown(report: Report, existing: str = "") -> str:
    lines = [
        f"## {report.generated_at} \u2014 last {report.since_days} days",
        "",
        f"Product spend **${report.product_cost:.4f}** across {report.product_runs} digest run(s); "
        f"eval spend **${report.eval_cost:.4f}**.",
        "",
    ]
    for bulletin in report.bulletins:
        block = render_bulletin(bulletin)
        if existing and "\n".join(block[2:]) in existing:
            lines.extend([block[0], "", "Unchanged since an earlier section.", ""])
        else:
            lines.extend(block)
            lines.append("")
    if not report.bulletins:
        lines.extend(["No bulletin in the window.", ""])
    return "\n".join(lines).rstrip() + "\n"


def read_record(path: Path | None = None) -> str:
    path = path or DEFAULT_PATH
    return path.read_text(encoding="utf-8") if path.exists() else HEADER


def append_report(text: str, path: Path | None = None) -> Path:
    path = path or DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_record(path)
    if not existing.endswith("\n"):
        existing += "\n"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(existing + "\n" + text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path


# The report output used to render against the existing record. Keep this
# compatibility helper for callers that treat the record as a pure string.
