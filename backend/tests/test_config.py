import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_missing_configuration_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOTAK_CONSUMER_KEY", raising=False)
    with pytest.raises(ValidationError, match="kotak_consumer_key"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_secrets_are_masked_in_repr() -> None:
    settings = Settings(
        _env_file=None,
        kotak_consumer_key="never-print-this",
        database_url="postgresql+psycopg://user:database-secret@localhost/db",
        openai_api_key="never-print-openai-key",
    )
    rendered = repr(settings)
    assert "never-print-this" not in rendered
    assert "database-secret" not in rendered
    assert "never-print-openai-key" not in rendered
    assert "**********" in rendered


def test_blank_ai_configuration_is_treated_as_missing() -> None:
    settings = Settings(
        _env_file=None,
        kotak_consumer_key="configured",
        openai_api_key="   ",
        ai_research_model="   ",
    )
    assert settings.openai_api_key is None
    assert settings.ai_research_model is None


def test_phase14_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None, kotak_consumer_key="configured")
    assert settings.alpha_engine_enabled is False
    assert settings.regime_use_statistical_alpha is False
    assert settings.strategy_volatility_buffer_enabled is False
    assert settings.pipeline_run_ai_research is False


@pytest.mark.parametrize("source", ["SPOT", "future"])
def test_alpha_price_source_is_validated(source: str) -> None:
    assert Settings(
        _env_file=None, kotak_consumer_key="configured", alpha_price_source=source
    ).alpha_price_source in {"SPOT", "FUTURE"}


def test_invalid_alpha_price_source_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ALPHA_PRICE_SOURCE"):
        Settings(_env_file=None, kotak_consumer_key="configured", alpha_price_source="MIXED")

