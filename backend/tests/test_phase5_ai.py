from datetime import datetime
import json
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.guardrails import (
    AIResearchSafetyError,
    ConfidenceCaps,
    agreement_status,
    apply_confidence_cap,
    estimate_cost_usd,
)
from app.ai.input_builder import build_ai_research_input
from app.ai.models import (
    AIResearchResult,
    AIResearchModelOutput,
    MarketView,
    ProviderResult,
    ProviderUsage,
)
from app.ai.prompts import SYSTEM_PROMPT
from app.ai.openai_provider import OpenAIResearchProvider
from app.ai.repository import AIResearchRepository
from app.ai.research_agent import AIResearchConfig, generate_research
from app.ai.service import build_and_store_ai_research
from app.api.ai_research import get_ai_research_repository
from app.collector.service import collection_bucket
from app.data.models import MarketSnapshot
from app.db.models import AIResearchSnapshotRecord
from app.db.repositories import SnapshotRepository
from app.features.engine import FeatureEngineConfig
from app.features.repository import FeatureRepository
from app.features.service import build_and_store_features
from app.main import app
from app.regime.engine import RegimeConfig
from app.regime.repository import RegimeRepository
from app.regime.service import build_and_store_regime
from scripts.build_ai_research import result_lines

IST = ZoneInfo("Asia/Kolkata")


def model_output(view: str = "UNCERTAIN", confidence: float = 50, observation: str = "Evidence is limited"):
    return AIResearchModelOutput(
        market_view=view,
        confidence=confidence,
        support_zone=None,
        resistance_zone=None,
        key_observations=[observation],
        bullish_evidence=[],
        bearish_evidence=[],
        range_evidence=[],
        risks=["Low evidence quality"],
        missing_evidence=["Intraday OI"],
        what_would_change_view=["Usable market-hours OI change"],
        research_summary="The supplied evidence supports an uncertain research view.",
    )


class MockProvider:
    provider_name = "mock"
    model_name = "mock-structured-model"

    def __init__(self, output=None, fail: bool = False) -> None:
        self.output = output or model_output()
        self.fail = fail
        self.calls = 0

    def generate_research(self, research_input):
        self.calls += 1
        if self.fail:
            raise RuntimeError("api_key=must-not-be-stored")
        now = datetime.now(IST)
        return ProviderResult(
            output=self.output,
            provider=self.provider_name,
            model=self.model_name,
            requested_at=now,
            responded_at=now,
            latency_ms=25,
            usage=ProviderUsage(input_tokens=800, output_tokens=200, total_tokens=1000),
        )


def stored_context(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> tuple[int, AIResearchRepository]:
    run_id = repository.create_collector_run(market_snapshot.timestamp_ist)
    raw = repository.save_market_snapshot(
        market_snapshot, collection_bucket(market_snapshot.timestamp_ist, 3), run_id
    )
    feature_repository = FeatureRepository(session_factory)
    build_and_store_features(feature_repository, raw.snapshot_id, FeatureEngineConfig())
    regime_repository = RegimeRepository(session_factory)
    build_and_store_regime(regime_repository, raw.snapshot_id, RegimeConfig())
    return raw.snapshot_id, AIResearchRepository(session_factory)


def research_input(repository: SnapshotRepository, session_factory, market_snapshot):
    snapshot_id, ai_repository = stored_context(repository, session_factory, market_snapshot)
    _, feature, _, regime = ai_repository.load_context(snapshot_id)
    return build_ai_research_input(feature, regime)


def test_compact_input_omits_raw_chain_and_sensitive_fields(
    repository, session_factory, market_snapshot
) -> None:
    value = research_input(repository, session_factory, market_snapshot)
    payload = value.model_dump(mode="json")
    rendered = json.dumps(payload)
    assert value.phase3_summary.top_call_oi
    assert "option_contract_snapshots" not in rendered
    assert '"options"' not in rendered
    field_names: set[str] = set()

    def collect_field_names(item) -> None:
        if isinstance(item, dict):
            field_names.update(str(key).lower() for key in item)
            for nested in item.values():
                collect_field_names(nested)
        elif isinstance(item, list):
            for nested in item:
                collect_field_names(nested)

    collect_field_names(payload)
    assert field_names.isdisjoint(
        {"consumer_key", "api_key", "authorization", "mpin", "totp", "sid", "rid"}
    )
    assert value.data_quality.intraday_oi_usable is True
    assert value.snapshot.india_vix is None


def test_structured_output_validation_rejects_malformed_and_invalid_view() -> None:
    with pytest.raises(ValidationError):
        AIResearchModelOutput.model_validate({"market_view": "SIDEWAYS"})
    with pytest.raises(ValidationError):
        model_output(confidence=101)
    with pytest.raises(ValidationError):
        model_output(confidence=-0.1)
    with pytest.raises(ValidationError):
        AIResearchModelOutput.model_validate(
            model_output().model_dump() | {"trade": "BUY"}
        )


@pytest.mark.parametrize("confidence", [0, 0.4, 40, 100])
def test_confidence_values_use_percent_scale_without_conversion(confidence: float) -> None:
    output = model_output(confidence=confidence)
    assert output.confidence == confidence


def test_fractional_confidence_is_not_rescaled_by_guardrail() -> None:
    assert apply_confidence_cap(
        model_output(confidence=0.4), "LOW", ConfidenceCaps()
    ) == (0.4, False, None)


def test_structured_output_schema_and_prompt_require_zero_to_one_hundred() -> None:
    confidence_schema = AIResearchModelOutput.model_json_schema()["properties"]["confidence"]
    result_confidence_schema = AIResearchResult.model_json_schema()["properties"]["confidence"]
    assert confidence_schema["minimum"] == 0
    assert confidence_schema["maximum"] == 100
    assert result_confidence_schema["minimum"] == 0
    assert result_confidence_schema["maximum"] == 100
    assert "0 to 100" in confidence_schema["description"]
    assert "confidence must be a number from 0 to 100" in SYSTEM_PROMPT
    assert "confidence: 35" in SYSTEM_PROMPT
    assert "confidence: 78" in SYSTEM_PROMPT


@pytest.mark.parametrize(
    ("quality", "cap"),
    [("INSUFFICIENT", 40), ("LOW", 55), ("MEDIUM", 75), ("HIGH", 90)],
)
def test_evidence_quality_confidence_caps(
    repository, session_factory, market_snapshot, quality: str, cap: float
) -> None:
    value = research_input(repository, session_factory, market_snapshot)
    value = value.model_copy(
        update={
            "data_quality": value.data_quality.model_copy(update={"evidence_quality": quality}),
            "phase4": value.phase4.model_copy(update={"evidence_quality": quality}),
        }
    )
    result = generate_research(value, MockProvider(model_output(confidence=99)))
    assert result.original_ai_confidence == 99
    assert result.confidence == cap
    assert result.confidence_capped is True
    assert result.cap_reason == f"{quality}_EVIDENCE_CONFIDENCE_CAP"


@pytest.mark.parametrize(
    ("deterministic", "view", "expected"),
    [
        ("BULLISH", MarketView.BULLISH, "AGREE"),
        ("BEARISH", MarketView.BEARISH, "AGREE"),
        ("BULLISH", MarketView.UNCERTAIN, "PARTIAL"),
        ("BULLISH", MarketView.BEARISH, "DISAGREE"),
        ("NO_TRADE", MarketView.UNCERTAIN, "AGREE"),
        ("NO_TRADE", MarketView.BULLISH, "NOT_COMPARABLE"),
    ],
)
def test_deterministic_agreement_mapping(deterministic, view, expected) -> None:
    assert agreement_status(deterministic, view).value == expected


def test_unavailable_dynamic_oi_claim_is_rejected(
    repository, session_factory, market_snapshot
) -> None:
    value = research_input(repository, session_factory, market_snapshot)
    value = value.model_copy(
        update={"data_quality": value.data_quality.model_copy(update={"intraday_oi_usable": False})}
    )
    provider = MockProvider(model_output(observation="Strong fresh put writing is confirmed"))
    with pytest.raises(AIResearchSafetyError):
        generate_research(value, provider)


def test_token_cost_and_missing_usage() -> None:
    assert estimate_cost_usd(1_000_000, 500_000, 2, 8) == 6
    assert estimate_cost_usd(None, 10, 2, 8) is None
    assert estimate_cost_usd(10, 10, None, 8) is None


def test_missing_api_key_fails_before_any_request() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIResearchProvider("", "configured-model")


def test_persistence_idempotency_force_usage_and_api(
    repository, session_factory, market_snapshot
) -> None:
    snapshot_id, ai_repository = stored_context(repository, session_factory, market_snapshot)
    provider = MockProvider(model_output(confidence=40))
    config = AIResearchConfig(input_cost_per_million=2, output_cost_per_million=8)
    first = build_and_store_ai_research(ai_repository, snapshot_id, provider, config)
    second = build_and_store_ai_research(ai_repository, snapshot_id, provider, config)
    forced = build_and_store_ai_research(
        ai_repository, snapshot_id, provider, config, force=True
    )
    with session_factory() as session:
        count = session.scalar(select(func.count(AIResearchSnapshotRecord.id)))
        record = session.scalar(select(AIResearchSnapshotRecord))
    assert first.reused_existing is False
    assert second.reused_existing is True
    assert forced.reused_existing is False
    assert provider.calls == 2
    assert count == 1
    assert record.input_tokens == 800 and record.total_tokens == 1000
    assert float(record.estimated_cost_usd) == 0.0032
    assert float(record.confidence) == 40
    assert float(record.original_ai_confidence) == 40
    app.dependency_overrides[get_ai_research_repository] = lambda: ai_repository
    try:
        client = TestClient(app)
        assert client.get("/api/ai-research/latest").status_code == 200
        detail = client.get(f"/api/ai-research/{snapshot_id}").json()
        assert detail["snapshot_id"] == snapshot_id
        assert detail["confidence"] == 40
        assert len(client.get("/api/ai-research?limit=50").json()) == 1
    finally:
        app.dependency_overrides.clear()

    assert "confidence=40.0" in result_lines(first)


def test_failed_provider_run_is_stored_without_secret(
    repository, session_factory, market_snapshot
) -> None:
    snapshot_id, ai_repository = stored_context(repository, session_factory, market_snapshot)
    with pytest.raises(RuntimeError):
        build_and_store_ai_research(
            ai_repository, snapshot_id, MockProvider(fail=True), AIResearchConfig()
        )
    stored = ai_repository.get(snapshot_id)
    assert stored["status"] == "FAILED"
    assert stored["error_type"] == "RuntimeError"
    assert "must-not-be-stored" not in json.dumps(stored)
