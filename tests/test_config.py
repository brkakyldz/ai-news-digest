from __future__ import annotations

import pytest

from ainews.config import Settings, get_settings


def test_environment_overrides_reach_settings(settings: Settings) -> None:
    assert settings.openai_api_key == "sk-test-not-a-real-key"
    assert settings.llm_configured is True
    assert settings.tavily_configured is False


def test_gpt6_luna_is_the_pipeline_default_and_judge_default_stays_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OPENAI_MODEL", "OPENAI_MODEL_SUMMARIZE", "OPENAI_MODEL_JUDGE"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)

    assert defaults.openai_model == "gpt-6-luna"
    assert defaults.openai_model_summarize == "gpt-6-luna"
    assert defaults.openai_model_judge == "gpt-5.6-terra"


def test_model_environment_overrides_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("OPENAI_MODEL_SUMMARIZE", "gpt-5.6-terra")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.openai_model == "gpt-5.6-sol"
    assert settings.openai_model_summarize == "gpt-5.6-terra"


def test_template_placeholders_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key still holding the template's `xxxx` is not a configured key."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    get_settings.cache_clear()
    assert get_settings().llm_configured is False


def test_numeric_bounds_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEDUPE_SCORE_THRESHOLD", "150")
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings()


def test_sqlite_path_resolves_relative_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./data/app.db")
    get_settings.cache_clear()
    assert get_settings().sqlite_path.name == "app.db"
