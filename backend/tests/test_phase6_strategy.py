from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.api.strategy_candidates import get_strategy_repository
from app.collector.service import collection_bucket
from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.db.models import StrategyCandidateSetRecord
from app.features.engine import build_market_features
from app.features.repository import FeatureRepository
from app.main import app
from app.regime.models import EvidenceQuality, Regime, RegimeResult
from app.regime.repository import RegimeRepository
from app.strategy.candidate_engine import StrategyConfig, _score, generate_candidates
from app.strategy.liquidity import bid_ask_spread_pct
from app.strategy.models import PricingBasis, StrategyType
from app.strategy.payoff import calculate_payoff
from app.strategy.repository import StrategyRepository
from app.strategy.service import backfill_candidates, build_and_store_candidates

IST = ZoneInfo("Asia/Kolkata")


def priced_snapshot(base: MarketSnapshot) -> MarketSnapshot:
    options = []
    for item in base.options:
        premium = max(5.0, 100 - abs(item.strike - 25_000) * 0.1)
        options.append(item.model_copy(update={
            "ltp": premium,
            "bid": premium - 1,
            "ask": premium + 1,
            "open_interest": 5_000,
            "volume": 500,
            "delta": None,
        }))
    return base.model_copy(update={"nifty_spot": 25_000, "atm_strike": 25_000, "options": options})


def phase6_context(base: MarketSnapshot, direction: str = "BULLISH"):
    raw = priced_snapshot(base)
    feature = build_market_features(1, raw, [])
    seed = feature.support_resistance.potential_support_clusters[0]
    support = seed.model_copy(update={"low_strike": 24_750, "high_strike": 24_800,
                                     "center_strike": 24_775})
    resistance = seed.model_copy(update={
        "side": "POTENTIAL_RESISTANCE_CLUSTER", "low_strike": 25_200,
        "high_strike": 25_250, "center_strike": 25_225,
    })
    feature = feature.model_copy(update={
        "support_resistance": feature.support_resistance.model_copy(update={
            "potential_support_clusters": [support],
            "potential_resistance_clusters": [resistance],
        })
    })
    regime = RegimeResult(
        snapshot_id=1,
        feature_snapshot_id=1,
        timestamp=raw.timestamp_ist,
        regime=direction,
        bull_score=8 if direction == "BULLISH" else 1,
        bear_score=8 if direction == "BEARISH" else 1,
        range_score=0,
        confidence=80,
        evidence_quality="HIGH",
        signal_groups=[],
        bull_evidence=[],
        bear_evidence=[],
        range_evidence=[],
        warnings=[],
        missing_inputs=[],
        risk_flags=[],
    )
    return raw, feature, regime


@pytest.mark.parametrize(
    ("regime", "expected", "reason"),
    [
        ("BULLISH", StrategyType.BULL_PUT_SPREAD, "CANDIDATE_ELIGIBLE"),
        ("BEARISH", StrategyType.BEAR_CALL_SPREAD, "CANDIDATE_ELIGIBLE"),
        ("RANGE", StrategyType.NONE, "REGIME_NOT_DIRECTIONAL"),
        ("NO_TRADE", StrategyType.NONE, "REGIME_NOT_DIRECTIONAL"),
    ],
)
def test_regime_gating_and_strategy_mapping(market_snapshot, regime, expected, reason):
    raw, feature, result = phase6_context(market_snapshot, regime)
    generated = generate_candidates(raw, feature, result, 10)
    assert generated.strategy_type == expected
    assert reason in generated.reason_codes
    assert generated.eligible is (expected != StrategyType.NONE)


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"confidence": 59}, "REGIME_CONFIDENCE_TOO_LOW"),
        ({"evidence_quality": EvidenceQuality.LOW}, "EVIDENCE_QUALITY_TOO_LOW"),
    ],
)
def test_quality_gates(market_snapshot, update, reason):
    raw, feature, regime = phase6_context(market_snapshot)
    generated = generate_candidates(raw, feature, regime.model_copy(update=update), 10)
    assert generated.eligible is False
    assert reason in generated.reason_codes


@pytest.mark.parametrize("direction", ["BULLISH", "BEARISH"])
def test_defined_risk_leg_rules_and_payoff(market_snapshot, direction):
    raw, feature, regime = phase6_context(market_snapshot, direction)
    candidate = generate_candidates(raw, feature, regime, 10).candidates[0]
    assert candidate.short_leg.action == "SELL"
    assert candidate.long_leg.action == "BUY"
    assert candidate.short_leg.expiry == candidate.long_leg.expiry == raw.expiry
    assert candidate.spread_width in (50, 100, 150, 200)
    assert candidate.net_credit == pytest.approx(
        candidate.short_leg.bid - candidate.long_leg.ask
    )
    assert candidate.max_profit == candidate.net_credit
    assert candidate.max_loss == pytest.approx(candidate.spread_width - candidate.net_credit)
    if direction == "BULLISH":
        assert candidate.short_leg.option_type == "PE"
        assert candidate.short_leg.strike < raw.nifty_spot
        assert candidate.long_leg.strike < candidate.short_leg.strike
        assert candidate.short_leg.strike <= candidate.support_or_resistance_reference
        assert candidate.breakeven == pytest.approx(candidate.short_leg.strike - candidate.net_credit)
    else:
        assert candidate.short_leg.option_type == "CE"
        assert candidate.short_leg.strike > raw.nifty_spot
        assert candidate.long_leg.strike > candidate.short_leg.strike
        assert candidate.short_leg.strike >= candidate.support_or_resistance_reference
        assert candidate.breakeven == pytest.approx(candidate.short_leg.strike + candidate.net_credit)


def test_missing_structure_is_conservative(market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot)
    feature = feature.model_copy(update={
        "support_resistance": feature.support_resistance.model_copy(
            update={"potential_support_clusters": []}
        )
    })
    result = generate_candidates(raw, feature, regime, 10)
    assert result.strategy_type == StrategyType.NONE
    assert "NO_SUPPORT_REFERENCE" in result.reason_codes


def test_bid_ask_preferred_and_ltp_fallback_marked(market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot)
    bid_ask = generate_candidates(raw, feature, regime, 10).candidates[0]
    assert bid_ask.pricing_basis == PricingBasis.BID_ASK
    no_quotes = raw.model_copy(update={
        "options": [item.model_copy(update={"bid": None, "ask": None}) for item in raw.options]
    })
    estimated = generate_candidates(no_quotes, feature, regime, 10).candidates[0]
    assert estimated.pricing_basis == PricingBasis.LTP_ESTIMATE
    assert "PRICING_USING_LTP_ESTIMATE" in estimated.warnings


@pytest.mark.parametrize("long_ltp", [100, 110])
def test_zero_or_negative_credit_rejected(market_snapshot, long_ltp):
    raw, feature, regime = phase6_context(market_snapshot)
    options = [item.model_copy(update={"bid": None, "ask": None, "ltp": long_ltp})
               if item.option_type.value == "PE" and item.strike < 24_750 else item
               for item in raw.options]
    result = generate_candidates(raw.model_copy(update={"options": options}), feature, regime, 10)
    assert result.eligible is False
    assert "INSUFFICIENT_CREDIT" in result.reason_codes


def test_liquidity_oi_volume_and_bid_ask_filters(market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot)
    low_oi = raw.model_copy(update={"options": [item.model_copy(update={"open_interest": 0}) for item in raw.options]})
    assert "LIQUIDITY_TOO_LOW" in generate_candidates(low_oi, feature, regime, 10).reason_codes
    low_volume = raw.model_copy(update={"options": [item.model_copy(update={"volume": 0}) for item in raw.options]})
    assert "LIQUIDITY_TOO_LOW" in generate_candidates(low_volume, feature, regime, 10).reason_codes
    wide = raw.model_copy(update={"options": [item.model_copy(update={"bid": 10, "ask": 20}) for item in raw.options]})
    assert "BID_ASK_TOO_WIDE" in generate_candidates(wide, feature, regime, 10).reason_codes
    crossed = raw.model_copy(update={"options": [item.model_copy(update={"bid": 20, "ask": 10}) for item in raw.options]})
    assert "MALFORMED_OPTION_RECORD" in generate_candidates(crossed, feature, regime, 10).reason_codes


def test_unusable_volume_warns_without_silent_rejection(market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot)
    feature = feature.model_copy(update={
        "data_quality": feature.data_quality.model_copy(update={"intraday_volume_usable": False})
    })
    raw = raw.model_copy(update={"options": [item.model_copy(update={"volume": 0}) for item in raw.options]})
    result = generate_candidates(raw, feature, regime, 10)
    assert result.eligible is True
    assert "VOLUME_UNAVAILABLE" in result.warnings


def test_delta_filter_and_unavailable_delta(market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot)
    config = StrategyConfig(short_delta_min_abs=0.1, short_delta_max_abs=0.3)
    missing = generate_candidates(raw, feature, regime, 10, config)
    assert missing.eligible and "DELTA_UNAVAILABLE" in missing.warnings
    bad = raw.model_copy(update={"options": [item.model_copy(update={"delta": -0.8}) for item in raw.options]})
    rejected = generate_candidates(bad, feature, regime, 10, config)
    assert not rejected.eligible and "DELTA_OUT_OF_RANGE" in rejected.reason_codes


def test_widths_missing_hedges_and_payoff_validation(market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot)
    result = generate_candidates(raw, feature, regime, 10, StrategyConfig(allowed_spread_widths=(75,)))
    assert not result.eligible and "NO_VALID_LONG_HEDGE" in result.reason_codes
    assert calculate_payoff(StrategyType.BULL_PUT_SPREAD, 100, 100, 1) is None
    assert calculate_payoff(StrategyType.BEAR_CALL_SPREAD, 100, 150, 50) is None


def test_ranking_is_stable_and_component_sensitive():
    args = dict(reference_gap=50, distance=200, width=50, credit_ratio=.10,
                short_oi=1000, long_oi=1000, pricing_basis=PricingBasis.BID_ASK,
                regime_confidence=80, config=StrategyConfig())
    base = _score(**args)
    assert base == _score(**args)
    assert _score(**(args | {"reference_gap": 0})) < base
    assert _score(**(args | {"credit_ratio": .03})) < base
    assert _score(**(args | {"short_oi": 100, "long_oi": 100})) < base


def test_stale_only_applies_to_live_mode(market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot)
    now = raw.timestamp_ist + timedelta(hours=1)
    historical = generate_candidates(raw, feature, regime, 10, enforce_freshness=False, now=now)
    live = generate_candidates(raw, feature, regime, 10, enforce_freshness=True, now=now)
    assert historical.eligible
    assert not live.eligible and "SNAPSHOT_STALE" in live.reason_codes


def test_persistence_idempotency_and_api(repository, session_factory, market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot)
    run_id = repository.create_collector_run(raw.timestamp_ist)
    stored = repository.save_market_snapshot(raw, collection_bucket(raw.timestamp_ist, 3), run_id)
    feature = feature.model_copy(update={"snapshot_id": stored.snapshot_id})
    feature_id = FeatureRepository(session_factory).upsert(feature)
    regime = regime.model_copy(update={"snapshot_id": stored.snapshot_id, "feature_snapshot_id": feature_id})
    RegimeRepository(session_factory).upsert(regime)
    strategy_repository = StrategyRepository(session_factory)
    first = build_and_store_candidates(strategy_repository, stored.snapshot_id, StrategyConfig())
    second = build_and_store_candidates(strategy_repository, stored.snapshot_id, StrategyConfig())
    with session_factory() as session:
        count = session.scalar(select(func.count(StrategyCandidateSetRecord.id)))
    assert first.candidates[0].candidate_id == second.candidates[0].candidate_id
    assert count == 1
    app.dependency_overrides[get_strategy_repository] = lambda: strategy_repository
    try:
        client = TestClient(app)
        assert client.get("/api/strategy-candidates/latest").status_code == 200
        assert client.get(f"/api/strategy-candidates/{stored.snapshot_id}").json()["candidate_count"] > 0
        assert len(client.get("/api/strategy-candidates?limit=50").json()) == 1
    finally:
        app.dependency_overrides.clear()


def test_no_candidate_is_valid_structured_result(market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot, "NO_TRADE")
    result = generate_candidates(raw, feature, regime, 10)
    assert result.model_dump(mode="json") | {}
    assert result.candidates == [] and result.candidate_count == 0
    assert result.strategy_type == StrategyType.NONE


def test_backfill_is_chronological_and_uses_only_repository_context(market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot, "NO_TRADE")

    class PersistedOnlyRepository:
        def __init__(self):
            self.saved = []

        def context_snapshot_ids_chronological(self):
            return [1, 2]

        def load_context(self, snapshot_id):
            current_feature = feature.model_copy(update={"snapshot_id": snapshot_id})
            current_regime = regime.model_copy(update={
                "snapshot_id": snapshot_id, "feature_snapshot_id": snapshot_id + 10
            })
            return raw, snapshot_id + 10, current_feature, snapshot_id + 20, current_regime

        def upsert(self, result):
            self.saved.append(result.snapshot_id)
            return len(self.saved)

    repository = PersistedOnlyRepository()
    results = backfill_candidates(repository, StrategyConfig())
    assert [result.snapshot_id for result in results] == [1, 2]
    assert repository.saved == [1, 2]
