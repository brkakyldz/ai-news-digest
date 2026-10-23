"""The sampled grounding judge: `ainews eval judge` (PLAN-EVALS E3.2-E3.4).

The judge reads the article text and checks whether its factual claims are
supported. It samples ranked stories first, estimates cost before calling the
model, and records each result in `eval_results`. The optional labelled mode is
a diagnostic count for stories the reader marked `wrong_fact`; it is not a
calibrated quality rate.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import day_bounds
from ainews.config import Settings, get_settings
from ainews.db import Article, Bulletin, BulletinItem, EvalResult, Source, Summary, Verdict
from ainews.pipeline.llm import judge as judge_model
from ainews.pipeline.llm import usage_from_message
from ainews.pipeline.nodes.summarize import MAX_BODY_CHARS
from ainews.pipeline.pricing import estimate_cost, resolve_model
from ainews.pipeline.prompts import load_prompt, prompt_version

log = logging.getLogger(__name__)

DEFAULT_SAMPLE = 12
DEFAULT_MAX_COST = 0.10
# Rough token estimate for the cost guard: four characters a token, and a
# short structured answer back.
CHARS_PER_TOKEN = 4
OUTPUT_TOKENS_GUESS = 80


class GroundingVerdict(BaseModel):
    """What the judge must return for one summary."""

    passed: bool = Field(description="True only if every claim is supported by the article text.")
    unsupported_claim: str | None = Field(
        default=None,
        description="The first unsupported claim, quoted from the summary. Empty when passed.",
    )


class CostGuard(RuntimeError):
    """Raised before the first call when the estimate is over the cap."""

    def __init__(self, estimate: float, cap: float, n: int) -> None:
        super().__init__(
            f"judging {n} summaries is estimated at ${estimate:.4f}, over --max-cost ${cap:.2f}; "
            "raise the cap or lower --sample"
        )
        self.estimate = estimate
        self.cap = cap


@dataclass(slots=True)
class Candidate:
    summary_id: int
    bulletin_id: int | None
    article_id: int
    source: str
    title: str
    body: str
    summary: str
    why_it_matters: str
    human_verdict: str | None = None
    # Which of the four a "wrong" was about. Only `wrong_fact` is a claim about
    # this judge's subject, and `calibrate` sets the rest aside.
    human_reason: str | None = None
    # The story's place in the bulletin, or None for one the editor left out.
    # What `choose_sample` prefers.
    position: int | None = None
    # Where `body` came from: `feed`, `fetch`, or `unknown` for a row written
    # before the article and the web context were separated (ADR 0029). A pass
    # over an `unknown` body is not evidence of grounding - it may be a pass
    # over search results appended to the article with no marker - so it is
    # recorded on the result rather than counted as if it were.
    body_source: str | None = None


@dataclass(slots=True)
class Outcome:
    candidate: Candidate
    passed: bool | None
    claim: str | None
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


@dataclass(slots=True)
class JudgeReport:
    bulletin_id: int
    model: str
    outcomes: list[Outcome] = field(default_factory=list)
    est_cost_usd: float = 0.0

    @property
    def n_passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed is True)

    @property
    def n_failed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed is False)

    @property
    def n_unparsed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed is None)


# -- loading ------------------------------------------------------------------


def _candidate(
    summary: Summary,
    article: Article,
    source: Source,
    verdict: str | None,
    item: BulletinItem | None = None,
    reason: str | None = None,
) -> Candidate:
    return Candidate(
        summary_id=summary.id,
        bulletin_id=item.bulletin_id if item else None,
        article_id=article.id,
        source=source.name,
        title=article.title,
        # The judge reads what the summariser read - the same cut-off - so a
        # claim from past the cut-off is unsupported here too.
        body=(article.body_text or "")[:MAX_BODY_CHARS],
        summary=summary.summary,
        why_it_matters=summary.why_it_matters,
        human_verdict=verdict,
        human_reason=reason,
        position=item.position if item else None,
        body_source=article.body_source,
    )


async def load_candidates(session: AsyncSession, bulletin_id: int) -> list[Candidate]:
    """Every story in the bulletin, and every summary its day left out.

    Both, because `choose_sample` prefers the published ones and fills the rest
    of the sample from below the fold - a judge that only ever saw the bulletin
    would measure the ranker's taste as much as the summariser's grounding. A
    summary with no body cannot be judged against one and is not offered.
    """
    bulletin = await session.get(Bulletin, bulletin_id)
    if bulletin is None:
        raise LookupError(f"no bulletin {bulletin_id}")
    start, end = day_bounds(bulletin.day)
    rows = (
        await session.execute(
            select(Summary, Article, Source, BulletinItem)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .outerjoin(
                BulletinItem,
                (BulletinItem.summary_id == Summary.id) & (BulletinItem.bulletin_id == bulletin_id),
            )
            .where(Summary.language == bulletin.language)
            .where(
                BulletinItem.id.isnot(None)
                | ((Summary.created_at >= start) & (Summary.created_at < end))
            )
            .where(Article.body_text.isnot(None))
            .where(Article.body_text != "")
            .order_by(Summary.id)
        )
    ).all()
    return [_candidate(s, a, src, None, item) for s, a, src, item in rows]


async def load_labelled(session: AsyncSession) -> list[Candidate]:
    """Summaries marked wrong by the reader and carrying a body.

    Keep every category here so the report can count non-factual feedback
    without asking the model to judge it.
    """
    rows = (
        await session.execute(
            select(Summary, Article, Source, Verdict)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .join(Verdict, Verdict.summary_id == Summary.id)
            .where(Article.body_text.isnot(None))
            .where(Verdict.verdict == "wrong")
            .order_by(Summary.id)
        )
    ).all()
    return [_candidate(s, a, src, v.verdict, reason=v.reason) for s, a, src, v in rows]


def choose_sample(candidates: list[Candidate], sample: int, seed: int) -> list[Candidate]:
    """`sample` candidates, the same ones for the same seed and the same bulletin.

    Published stories first: every ranked one when they fit, a seeded draw among
    them when they do not, and the remaining places filled by a seeded draw
    from below the fold. Judging what the reader sees is what makes a judgement
    and a label land on the same summary, which is the only pair calibration
    can use.
    """
    ordered = sorted(candidates, key=lambda c: c.summary_id)
    if sample >= len(ordered):
        return ordered
    ranked = [c for c in ordered if c.position is not None]
    unranked = [c for c in ordered if c.position is None]
    rng = random.Random(seed)
    if len(ranked) >= sample:
        chosen = rng.sample(ranked, sample)
    else:
        chosen = ranked + rng.sample(unranked, sample - len(ranked))
    return sorted(chosen, key=lambda c: c.summary_id)


# -- the call -----------------------------------------------------------------


def build_prompt(candidate: Candidate) -> str:
    return load_prompt("judge_grounding", "en").format(
        source=candidate.source,
        title=candidate.title,
        body=candidate.body or "(no body text)",
        summary=candidate.summary,
        why_it_matters=candidate.why_it_matters,
    )


def estimate_judge_cost(candidates: list[Candidate], model: str) -> float:
    tokens_in = sum(len(build_prompt(c)) // CHARS_PER_TOKEN for c in candidates)
    return estimate_cost(model, tokens_in, OUTPUT_TOKENS_GUESS * len(candidates))


async def judge_one(candidate: Candidate, settings: Settings, model: str | None = None) -> Outcome:
    """Judge one summary. Never raises: a failure is an outcome with `error`."""
    try:
        client = judge_model(settings, model).with_structured_output(
            GroundingVerdict, include_raw=True
        )
        response: Any = await client.ainvoke(build_prompt(candidate))
    except Exception as exc:
        log.warning("judge failed for summary %d: %s", candidate.summary_id, exc)
        return Outcome(candidate, None, None, error=f"{type(exc).__name__}: {exc}"[:300])

    parsed = response.get("parsed") if isinstance(response, dict) else None
    usage = usage_from_message(response.get("raw") if isinstance(response, dict) else None)
    if parsed is None:
        return Outcome(
            candidate, None, None, usage.tokens_in, usage.tokens_out, error="unparsable output"
        )
    claim = (parsed.unsupported_claim or "").strip() or None
    return Outcome(candidate, bool(parsed.passed), claim, usage.tokens_in, usage.tokens_out)


UNATTRIBUTED = "body provenance unknown; this pass is not evidence of grounding"


def _detail(outcome: Outcome) -> str | None:
    """What goes on the `EvalResult` row.

    A failure names the claim; an error names itself. A *pass* over a body
    nothing can attribute says so, because that is the one case where the
    number and its meaning come apart: before the split, `body_text` could hold
    the article or the article with a week's search results appended, and a
    judge reading the second and passing it has confirmed nothing.
    """
    if outcome.claim or outcome.error:
        return outcome.claim or outcome.error
    if outcome.passed and outcome.candidate.body_source not in ("feed", "fetch"):
        return UNATTRIBUTED
    return None


async def judge_candidates(
    session: AsyncSession,
    candidates: list[Candidate],
    *,
    max_cost: float,
    settings: Settings | None = None,
    model: str | None = None,
) -> list[Outcome]:
    """Judge each candidate, write one `EvalResult` per outcome, commit once.

    The model is resolved here rather than read from settings, so the name the
    cost guard prices, the name the calls go to and the name written on every
    `EvalResult` row are one value (ADR 0020). Judging the same bulletin twice
    on two tiers is a legitimate thing to want; a record that could not tell the
    two apart afterwards would not be one.
    """
    settings = settings or get_settings()
    model = resolve_model(model, settings.openai_model_judge)
    estimate = estimate_judge_cost(candidates, model)
    if estimate > max_cost:
        raise CostGuard(estimate, max_cost, len(candidates))

    outcomes: list[Outcome] = []
    for candidate in candidates:
        outcome = await judge_one(candidate, settings, model)
        outcomes.append(outcome)
        session.add(
            EvalResult(
                bulletin_id=candidate.bulletin_id,
                summary_id=candidate.summary_id,
                kind="grounding",
                passed=outcome.passed,
                detail=_detail(outcome),
                model=model,
                prompt_version=prompt_version("judge_grounding", "en"),
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                est_cost_usd=estimate_cost(model, outcome.tokens_in, outcome.tokens_out),
            )
        )
    await session.commit()
    return outcomes


async def judge_bulletin(
    session: AsyncSession,
    bulletin_id: int,
    *,
    sample: int = DEFAULT_SAMPLE,
    seed: int = 0,
    max_cost: float = DEFAULT_MAX_COST,
    settings: Settings | None = None,
    model: str | None = None,
) -> JudgeReport:
    settings = settings or get_settings()
    model = resolve_model(model, settings.openai_model_judge)
    chosen = choose_sample(await load_candidates(session, bulletin_id), sample, seed)
    outcomes = await judge_candidates(
        session, chosen, max_cost=max_cost, settings=settings, model=model
    )
    cost = sum(estimate_cost(model, o.tokens_in, o.tokens_out) for o in outcomes)
    return JudgeReport(bulletin_id, model, outcomes, cost)


# -- diagnostic feedback counts ----------------------------------------------


@dataclass(slots=True)
class Calibration:
    """Diagnostic count of wrong-fact labels the judge caught or missed."""

    wrong_caught: int = 0
    wrong_missed: int = 0
    unparsed: int = 0
    other_reason: int = 0

    @property
    def n_wrong(self) -> int:
        return self.wrong_caught + self.wrong_missed


def calibrate(labels: list[tuple[str, str | None, bool | None]]) -> Calibration:
    """Count judge outcomes for reader feedback specifically tagged wrong_fact."""
    table = Calibration()
    for human, reason, judged in labels:
        if human != "wrong":
            continue
        if reason != "wrong_fact":
            table.other_reason += 1
        elif judged is None:
            table.unparsed += 1
        elif judged:
            table.wrong_missed += 1
        else:
            table.wrong_caught += 1
    return table


def format_calibration(table: Calibration) -> str:
    lines = [
        f"wrong_fact labels: {table.n_wrong} (diagnostic, not a quality rate)",
        f"judge caught {table.wrong_caught}; missed {table.wrong_missed}",
    ]
    if table.unparsed:
        lines.append(f"{table.unparsed} judged but unparsable, not counted")
    if table.other_reason:
        lines.append(f"{table.other_reason} wrong labels use another category and were not counted")
    return "\n".join(lines)


async def judge_labelled(
    session: AsyncSession,
    *,
    max_cost: float = DEFAULT_MAX_COST,
    settings: Settings | None = None,
    model: str | None = None,
) -> tuple[Calibration, list[Outcome]]:
    settings = settings or get_settings()
    labelled = await load_labelled(session)
    factual = [candidate for candidate in labelled if candidate.human_reason == "wrong_fact"]
    outcomes = await judge_candidates(
        session, factual, max_cost=max_cost, settings=settings, model=model
    )
    judged = {outcome.candidate.summary_id: outcome.passed for outcome in outcomes}
    labels = [
        (candidate.human_verdict, candidate.human_reason, judged.get(candidate.summary_id))
        for candidate in labelled
    ]
    return calibrate(labels), outcomes
