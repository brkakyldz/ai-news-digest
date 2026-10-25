"""Deterministic checks over a run's stored output.

Every function takes plain dicts and returns plain numbers: no I/O, no settings,
no model. That is what lets the same code run over a recorded fixture inside
`pytest` and over the live database inside `ainews eval report`.

A story dict has the shape `record.py` writes: `article_id`, `source`, `weight`,
`title`, `title_local`, `summary`, `why_it_matters`, `tags`, `importance`,
`rank`, `body_numerals`, `body_capitalised`. Article bodies are never here -
only the numerals and capitalised tokens a check needs.

The 2026-09-04 probe that these grew out of is
`reports/research/2026-09-05_quality-evaluation.md`: a ten-line numeral check
over 91 summaries found one fabricated figure and no false positives, and both
prompt regressions in the project's history were format failures a word count
would have caught.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from ainews.pipeline.prompts import TAG_VOCABULARY

Story = dict[str, Any]

# The budgets the summarize prompt states, in both languages.
WORD_BUDGET = {"summary": 55, "why_it_matters": 20, "title_local": 10}

# -- numerals -----------------------------------------------------------------

# A number with optional grouping and decimal marks, optionally glued to a
# currency or percent sign on either side, optionally followed by a scale word
# or a one-letter suffix ("3B", "300M"). Turkish and English scale words both.
_SCALE = {
    "bin": 1_000,
    "thousand": 1_000,
    "k": 1_000,
    "milyon": 1_000_000,
    "million": 1_000_000,
    "m": 1_000_000,
    "mn": 1_000_000,
    "milyar": 1_000_000_000,
    "billion": 1_000_000_000,
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "trilyon": 1_000_000_000_000,
    "trillion": 1_000_000_000_000,
}
_NUMERAL = re.compile(
    r"(?<![\w.])"
    r"[$€£₺%]?\s?"
    r"(?P<number>\d+(?:[.,]\d+)*)"
    r"\s?%?"
    r"(?:\s?(?P<scale>bin|thousand|milyon|million|mn|milyar|billion|bn|trilyon|trillion|[kmb])"
    r"(?=[^\w]|$))?",
    re.IGNORECASE,
)
# Turkish suffixes glue to the number with an apostrophe: "500'den", "2026'da".
# They carry no numeric meaning and are simply not part of the match.


def _parse_number(raw: str) -> float | None:
    """`12,9` and `12.9` are both twelve point nine; `1,000` and `1.000` are both
    a thousand. A mark followed by exactly three digits, with nothing after or
    another such group, is a thousands separator; anything else is a decimal.
    """
    parts = re.split(r"([.,])", raw)
    digits = parts[0::2]
    marks = parts[1::2]
    if not marks:
        return float(digits[0])
    if all(len(d) == 3 for d in digits[1:]):
        # Every group after the first has three digits: grouping marks, unless
        # there is exactly one mark and the whole thing is short ("1,500" is a
        # thousand and a half; "1,5" never reaches here).
        return float("".join(digits))
    if len(marks) == 1:
        return float(digits[0] + "." + digits[1])
    # Mixed, e.g. "1.234,5": the last mark is the decimal one.
    return float("".join(digits[:-1]) + "." + digits[-1])


def _canonical(value: float) -> str:
    """One spelling per value, so `12.9e9` and `12900000000.0` compare equal."""
    if value == int(value):
        return str(int(value))
    return repr(round(value, 6))


def numeral_values(text: str) -> set[str]:
    """Every number in `text`, canonicalised.

    A number with a scale word yields *both* its scaled value and its bare
    value: "13 billion" is `{"13000000000", "13"}`. The bare form is kept so a
    summary that says "13" about a body that says "13 billion" is still
    grounded; the scaled form is what lets "12,9 milyar" meet "$12.9 billion".
    """
    values: set[str] = set()
    for match in _NUMERAL.finditer(text or ""):
        number = _parse_number(match.group("number"))
        if number is None:
            continue
        values.add(_canonical(number))
        scale = match.group("scale")
        if scale:
            values.add(_canonical(number * _SCALE[scale.lower()]))
    return values


def ungrounded_numerals(stories: list[Story]) -> list[tuple[int, list[str]]]:
    """`(article_id, numerals)` for every story whose summary or why-it-matters
    carries a number the body did not.

    A number with a scale word is checked in its scaled form only - "13 milyar"
    against a body that says "12.9 billion" is a rounded figure, and the prompt
    says a rounded figure is a figure the text did not give.
    """
    flagged: list[tuple[int, list[str]]] = []
    for story in stories:
        body = set(story.get("body_numerals") or [])
        if not body:
            # A body-less article was summarised from its title; nothing to
            # ground against, so nothing to flag.
            continue
        text = f"{story.get('summary', '')} {story.get('why_it_matters', '')}"
        missing: list[str] = []
        for match in _NUMERAL.finditer(text):
            number = _parse_number(match.group("number"))
            if number is None:
                continue
            scale = match.group("scale")
            value = _canonical(number * _SCALE[scale.lower()]) if scale else _canonical(number)
            if value not in body and value not in missing:
                missing.append(value)
        if missing:
            flagged.append((int(story["article_id"]), missing))
    return flagged


# -- the content floor --------------------------------------------------------

# Words too common to count as a match between a key fact and a summary. Both
# languages, because a story is summarised in one and the fact is copied from an
# English body: "the model" matching "modelin" would make the check pass on
# nothing.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "new",
        "model",
        "models",
        "ai",
        "research",
        "company",
        "release",
        "version",
        "ve",
        "veya",
        "ile",
        "bir",
        "bu",
        "da",
        "de",
        "icin",
        "için",
        "yeni",
        "model",
        "modeli",
        "yapay",
        "zeka",
        "zekâ",
    ]
)


def _content_tokens(text: str) -> set[str]:
    """Lowercased words worth matching on, punctuation and stopwords dropped."""
    words = re.findall(r"[\w']+", (text or "").lower(), re.UNICODE)
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def content_floor(stories: list[Story]) -> dict[str, Any]:
    """Does the writing carry anything concrete, or only the right shape.

    The word-budget and tag checks measure shape, while the content floor tests
    whether the summary kept specific facts from its source. A regression to three
    generic numberless sentences at importance 3 clears all of them, and clears
    the numeral check (no figure to flag) and the grounding judge (no claim to
    disagree with) as well. The 2026-09-06 run was already 70% threes.

    Two numbers, both shares of the stories they can be asked of:

    `key_fact_share` - how many stories the summariser could name a concrete
    thing for at all. `key_fact_kept` - of those, how many carry it in the
    summary or the headline. A fact recorded and then written around is the
    exact regression this exists to catch: the model still *knows* what the
    story was and no longer tells the reader.

    `numeral_recall` - of the stories whose body carries a figure, how many
    summaries carry one of the body's own figures. It is the mirror of
    `ungrounded_numerals`: that one catches a figure that was invented, this one
    catches a summary that dropped every figure the article had. A run of
    numberless prose scores 0.0 here and is flagged by nothing else.

    Reported and not asserted, like the other distributions: the bar depends on
    the day's mix of releases and opinion, and a threshold nobody can defend is
    a threshold that gets raised until it passes.
    """
    with_fact = [s for s in stories if (s.get("key_fact") or "").strip()]
    kept = 0
    for story in with_fact:
        fact = _content_tokens(story["key_fact"])
        written = _content_tokens(f"{story.get('title_local', '')} {story.get('summary', '')}")
        # Any content word in common. Not the whole phrase: a fact copied from
        # an English body and written into a Turkish summary comes back
        # inflected, and demanding the string return unchanged would measure the
        # language rather than the writing.
        if fact & written:
            kept += 1

    grounded = [s for s in stories if s.get("body_numerals")]
    recalled = 0
    for story in grounded:
        body = set(story["body_numerals"])
        text = f"{story.get('summary', '')} {story.get('why_it_matters', '')}"
        if numeral_values(text) & body:
            recalled += 1

    n = len(stories)
    return {
        "n": n,
        "key_fact_share": len(with_fact) / n if n else 0.0,
        "key_fact_kept": kept / len(with_fact) if with_fact else None,
        "n_with_numerals": len(grounded),
        "numeral_recall": recalled / len(grounded) if grounded else None,
    }


# -- format budgets -----------------------------------------------------------


def word_count(text: str) -> int:
    return len((text or "").split())


def word_budget(stories: list[Story]) -> dict[str, Any]:
    """Share of stories over each field's prompt word budget."""
    n = len(stories)
    over = dict.fromkeys(WORD_BUDGET, 0)
    for story in stories:
        for field, budget in WORD_BUDGET.items():
            if word_count(story.get(field, "")) > budget:
                over[field] += 1
    return {
        "n": n,
        "over_share": {field: (count / n if n else 0.0) for field, count in over.items()},
        "over_count": over,
    }


# -- tags and scores ----------------------------------------------------------


def tag_vocabulary(stories: list[Story], top: int = 10) -> dict[str, Any]:
    """How much of a filter the tags make.

    `singleton_share` is the share of distinct tags used exactly once - a tag
    that filters to one story filters nothing. `in_vocabulary_share` is the
    share of tag *uses* drawn from the list the prompt asks the model to prefer
    (`prompts.TAG_VOCABULARY`); on a run recorded before that list existed it
    reports how the model's own words happened to overlap it.
    """
    counts: Counter[str] = Counter()
    for story in stories:
        counts.update(story.get("tags") or [])
    distinct = len(counts)
    singletons = sum(1 for c in counts.values() if c == 1)
    uses = sum(counts.values())
    in_vocabulary = sum(n for tag, n in counts.items() if tag in TAG_VOCABULARY)
    return {
        "distinct": distinct,
        "singleton_share": (singletons / distinct) if distinct else 0.0,
        "in_vocabulary_share": (in_vocabulary / uses) if uses else 0.0,
        "top": counts.most_common(top),
    }


def importance_distribution(stories: list[Story]) -> dict[int, float]:
    n = len(stories)
    counts = Counter(int(s.get("importance", 0)) for s in stories)
    return {score: (counts.get(score, 0) / n if n else 0.0) for score in range(1, 6)}


# -- ranking ------------------------------------------------------------------


def ranked_order(stories: list[Story]) -> list[int]:
    """The published order, by article id. `position` is the editor's placement
    (ADR 0030); a story with none was not in the bulletin."""
    ranked = [s for s in stories if s.get("position") is not None]
    return [int(s["article_id"]) for s in sorted(ranked, key=lambda s: int(s["position"]))]
