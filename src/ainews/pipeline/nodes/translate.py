"""Translate the selected canonical stories before they can be published."""

from __future__ import annotations

import json
from collections import Counter

from pydantic import BaseModel, Field
from sqlalchemy import select

from ainews.config import get_settings
from ainews.db import Summary, SummaryTranslation
from ainews.db.session import session_scope
from ainews.pipeline.llm import summarizer, usage_from_message
from ainews.pipeline.pricing import estimate_cost
from ainews.pipeline.state import PipelineState


class StoryTranslation(BaseModel):
    title_local: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)


class EditorialReason(BaseModel):
    summary_id: int
    reason: str = Field(min_length=1)


class EditorialTranslation(BaseModel):
    editor_note: str = Field(min_length=1)
    reasons: list[EditorialReason]


class TranslationFailure(RuntimeError):
    def __init__(self, message: str, tokens_in: int = 0, tokens_out: int = 0):
        super().__init__(message)
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


def _nonblank(*values: str | None) -> bool:
    return all(value is not None and value.strip() for value in values)


async def _translate(model: str, schema: type[BaseModel], name: str, prompt: str):
    try:
        client = summarizer(model=model).with_structured_output(
            schema, method="json_schema", strict=True, include_raw=True
        )
        response = await client.ainvoke(prompt)
    except Exception as exc:
        raise TranslationFailure(f"{name} failed: {type(exc).__name__}: {exc}") from exc
    usage = usage_from_message(response.get("raw") if isinstance(response, dict) else None)
    parsed = response.get("parsed") if isinstance(response, dict) else None
    if parsed is None:
        raise TranslationFailure(
            f"{name} returned invalid structured output", usage.tokens_in, usage.tokens_out
        )
    return parsed, usage.tokens_in, usage.tokens_out


async def translate_publication(state: PipelineState) -> PipelineState:
    totals = Counter()
    try:
        return await _translate_publication(state, totals)
    except TranslationFailure:
        raise
    except Exception as exc:
        raise TranslationFailure(
            f"translation pipeline failed: {type(exc).__name__}: {exc}",
            totals["in"],
            totals["out"],
        ) from exc


async def _translate_publication(state: PipelineState, totals: Counter) -> PipelineState:
    ranked = state.get("ranked") or []
    model = state.get("model_summarize") or get_settings().openai_model_summarize
    ids = [item["summary_id"] for item in ranked]
    translated: dict[int, dict[str, str]] = {}
    canonical: dict[int, Summary] = {}

    if not ids:
        return {
            "translated": {},
            "editor_note_tr": None,
            "translation_tokens_in": 0,
            "translation_tokens_out": 0,
        }
    if not (state.get("editor_note") or "").strip():
        raise TranslationFailure("selected stories have no English editor note")

    async with session_scope() as session:
        summaries = list(
            (await session.execute(select(Summary).where(Summary.id.in_(ids)))).scalars()
        )
        canonical = {summary.id: summary for summary in summaries}
        if set(canonical) != set(ids) or any(
            summary.language != "en"
            or not _nonblank(summary.title_local, summary.summary, summary.why_it_matters)
            for summary in summaries
        ):
            raise TranslationFailure(
                "selected canonical summaries are missing, blank, or not English"
            )

        for summary_id in ids:
            summary = canonical[summary_id]
            cached = (
                await session.execute(
                    select(SummaryTranslation).where(
                        SummaryTranslation.summary_id == summary_id,
                        SummaryTranslation.language == "tr",
                    )
                )
            ).scalar_one_or_none()
            if (
                cached is not None
                and cached.canonical_hash == summary.content_hash
                and _nonblank(cached.title_local, cached.summary, cached.why_it_matters)
            ):
                translated[summary_id] = {
                    "title_local": cached.title_local,
                    "summary": cached.summary,
                    "why_it_matters": cached.why_it_matters,
                }
                continue

            prompt = (
                "Translate the following canonical English news prose into clear Turkish. "
                "Preserve facts, names, numbers, versions and dates. Return only the schema.\n"
                f"title_local: {summary.title_local}\nsummary: {summary.summary}\n"
                f"why_it_matters: {summary.why_it_matters}"
            )
            try:
                result, tokens_in, tokens_out = await _translate(
                    model, StoryTranslation, "StoryTranslation", prompt
                )
            except TranslationFailure as exc:
                raise TranslationFailure(
                    str(exc), totals["in"] + exc.tokens_in, totals["out"] + exc.tokens_out
                ) from exc
            totals["in"] += tokens_in
            totals["out"] += tokens_out
            values = {
                "title_local": result.title_local.strip(),
                "summary": result.summary.strip(),
                "why_it_matters": result.why_it_matters.strip(),
            }
            if not _nonblank(*values.values()):
                raise TranslationFailure(
                    "story translation contains blank prose", totals["in"], totals["out"]
                )
            if cached is None:
                cached = SummaryTranslation(summary_id=summary_id, language="tr")
                session.add(cached)
            cached.title_local = values["title_local"]
            cached.summary = values["summary"]
            cached.why_it_matters = values["why_it_matters"]
            cached.canonical_hash = summary.content_hash
            cached.model = model
            cached.tokens_in = tokens_in
            cached.tokens_out = tokens_out
            cached.est_cost_usd = estimate_cost(model, tokens_in, tokens_out)
            try:
                await session.commit()
            except Exception as exc:
                raise TranslationFailure(
                    f"could not cache translation: {type(exc).__name__}: {exc}",
                    totals["in"],
                    totals["out"],
                ) from exc
            translated[summary_id] = values

        reasons = {item["summary_id"]: item.get("reason", "").strip() for item in ranked}
        expected = {summary_id for summary_id, reason in reasons.items() if reason}
        source_note = (state.get("editor_note") or "").strip()
        if not source_note and not expected:
            editor_note_tr = None
            reason_translations: dict[int, str] = {}
        else:
            editorial_source = {
                "editor_note": source_note,
                "reasons": [
                    {"summary_id": sid, "reason": reasons[sid]} for sid in sorted(expected)
                ],
            }
            prompt = (
                "Translate this editorial note and each nonempty placement reason into Turkish. "
                "Return every supplied summary_id exactly once, no others. Preserve facts.\n"
                "Translate only the values; preserve each summary_id exactly:\n"
                f"{json.dumps(editorial_source, ensure_ascii=False)}"
            )
            try:
                editorial, tokens_in, tokens_out = await _translate(
                    model, EditorialTranslation, "EditorialTranslation", prompt
                )
            except TranslationFailure as exc:
                raise TranslationFailure(
                    str(exc), totals["in"] + exc.tokens_in, totals["out"] + exc.tokens_out
                ) from exc
            totals["in"] += tokens_in
            totals["out"] += tokens_out
            received = [row.summary_id for row in editorial.reasons]
            if Counter(received) != Counter(expected) or not editorial.editor_note.strip():
                raise TranslationFailure(
                    "editorial translation did not exactly cover the requested IDs",
                    totals["in"],
                    totals["out"],
                )
            reason_translations = {row.summary_id: row.reason.strip() for row in editorial.reasons}
            if any(not value for value in reason_translations.values()):
                raise TranslationFailure(
                    "editorial translation contains a blank reason", totals["in"], totals["out"]
                )
            editor_note_tr = editorial.editor_note.strip()

    return {
        "translated": translated,
        "reason_translations": reason_translations,
        "editor_note_tr": editor_note_tr,
        "translation_tokens_in": totals["in"],
        "translation_tokens_out": totals["out"],
    }
