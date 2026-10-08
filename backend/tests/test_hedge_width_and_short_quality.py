"""Width settings reach hard risk; balanced short scoring never changes gates."""
from dataclasses import replace

import pytest

from app.core.config import Settings
from app.risk.engine import evaluate_risk
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.risk.exposure import ResearchRiskStateProvider
from app.risk.models import EvaluationContext
from app.risk.service import config_from_settings as risk_config
from app.strategy.candidate_engine import generate_candidates
from app.strategy.construction import construction_key, short_leg_quality
from app.strategy.service import config_from_settings as strategy_config
from tests.test_short_leg_first import example, construct


def settings(monkeypatch, *, enabled=True, **overrides):
    for name in ("RISK_MAX_SPREAD_WIDTH", "STRATEGY_ALLOWED_SPREAD_WIDTHS", "STRATEGY_ALLOWED_WIDTHS_POINTS"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, kotak_consumer_key="OFFLINE", telegram_enabled=False,
        phase14_2_strategy_logic_enabled=enabled, risk_max_loss_per_trade=20000,
        risk_max_capital_per_trade=20000, risk_required_consecutive_directional_snapshots=1,
        **overrides)


def evaluate(context, candidates, config):
    raw, feature, regime = context
    return evaluate_risk(candidates, 99, raw, feature, regime, config,
        EvaluationContext.HISTORICAL, ResearchRiskStateProvider(), ConfiguredMarketEventProvider())


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("width", [300, 400])
@pytest.mark.parametrize("direction", ["BULLISH", "BEARISH"])
def test_phase14_defaults_allow_wide_hedge_through_hard_risk(monkeypatch, market_snapshot, enabled, width, direction):
    configured = settings(monkeypatch, enabled=enabled)
    assert configured.risk_max_spread_width is None
    context = example(market_snapshot, direction)
    raw, feature, regime = context
    # Only observed hedges up to the requested width exist in this fixture.
    short_strike = 24700 if direction == "BULLISH" else 25300
    raw = raw.model_copy(update={"options": [c for c in raw.options if abs(c.strike-short_strike) <= width]})
    context = raw, feature, regime
    config = strategy_config(configured)
    assert width in config.allowed_spread_widths and config.max_defined_width is None
    candidates = generate_candidates(raw, feature, regime, 10, config)
    assert candidates.candidates[0].spread_width == width
    result = evaluate(context, candidates, risk_config(configured))
    decision = next(d for d in result.decisions if d.candidate_reference == candidates.candidates[0].candidate_id)
    assert decision.decision == "APPROVED", decision.reason_codes
    assert next(c for c in decision.checks if c.check_code == "MAX_SPREAD_WIDTH").threshold is None


@pytest.mark.parametrize("limit,reason", [("max_loss_per_trade", "HEDGE_REJECTED_MAX_LOSS"),
    ("max_capital_per_trade", "HEDGE_REJECTED_MAX_CAPITAL"), ("max_spread_width", "HEDGE_REJECTED_MAX_WIDTH")])
def test_phase14_400_rejected_by_each_explicit_limit(monkeypatch, market_snapshot, limit, reason):
    configured = settings(monkeypatch)
    context = example(market_snapshot, "BEARISH")
    raw, feature, regime = context
    base = risk_config(configured)
    stricter = replace(base, **{limit: 200 if limit == "max_spread_width" else 10000})
    config = strategy_config(configured, risk=stricter)
    candidates = generate_candidates(raw, feature, regime, 10, config)
    assert candidates.candidates and all(c.spread_width < 400 for c in candidates.candidates)
    assert reason in candidates.reason_codes
    # Defense in depth: the real risk engine rejects even an unfiltered proposal.
    broad = generate_candidates(raw, feature, regime, 10, strategy_config(configured))
    wide = next(c for c in broad.candidates if c.spread_width == 400)
    result = evaluate(context, broad, stricter)
    decision = next(d for d in result.decisions if d.candidate_reference == wide.candidate_id)
    assert decision.decision == "REJECTED"
    expected = {"max_loss_per_trade": "MAX_LOSS_EXCEEDED", "max_capital_per_trade": "MAX_CAPITAL_EXCEEDED",
                "max_spread_width": "MAX_SPREAD_WIDTH_EXCEEDED"}[limit]
    assert expected in decision.reason_codes


def test_explicit_200_setting_is_preserved(monkeypatch):
    configured = settings(monkeypatch, risk_max_spread_width=200)
    assert strategy_config(configured).max_defined_width == 200
    assert risk_config(configured).max_spread_width == 200
    monkeypatch.setenv("RISK_MAX_SPREAD_WIDTH", "200")
    from_environment = Settings(_env_file=None, kotak_consumer_key="OFFLINE", telegram_enabled=False)
    assert risk_config(from_environment).max_spread_width == 200


@pytest.mark.parametrize("direction", ["BULLISH", "BEARISH"])
def test_phase14_lower_premium_materially_safer_short_wins(market_snapshot, direction):
    candidates, _ = construct("phase14", example(market_snapshot, direction, structure_buffered=False), direction)
    winner = candidates[0]
    premium_rich = next(c for c in candidates if c.construction_evidence["short_sell_price"] == 80)
    assert winner.construction_evidence["short_sell_price"] == 59
    for key in ("structure", "survival", "distance"):
        assert winner.construction_evidence["short_quality_components"][key] > premium_rich.construction_evidence["short_quality_components"][key]
    assert winner.construction_evidence["short_quality_score"] > premium_rich.construction_evidence["short_quality_score"]


@pytest.mark.parametrize("engine", ["phase14", "phase15"])
@pytest.mark.parametrize("direction", ["BULLISH", "BEARISH"])
def test_lower_premium_better_short_book_and_liquidity_can_win(engine, market_snapshot, direction):
    raw, feature, regime = example(market_snapshot, direction)
    first = raw.options[0]
    # Still eligible, but materially inferior to the neighboring short.
    raw = raw.model_copy(update={"options": [first.model_copy(update={
        "ask": 95, "open_interest": 100, "volume": 1}), *raw.options[1:]]})
    candidates, _ = construct(engine, (raw, feature, regime), direction)
    winner = candidates[0]
    assert winner.construction_evidence["short_sell_price"] < 80
    assert any(c.construction_evidence["short_sell_price"] == 80 for c in candidates)


@pytest.mark.parametrize("engine", ["phase14", "phase15"])
def test_short_score_is_hedge_independent_and_auditable(engine, market_snapshot):
    candidates, _ = construct(engine, example(market_snapshot, "BEARISH"), "BEARISH")
    grouped = {}
    for c in candidates:
        evidence = c.construction_evidence
        parts = [v for v in evidence["short_quality_components"].values() if v is not None]
        assert evidence["short_quality_score"] == pytest.approx(100 * sum(parts) / len(parts))
        grouped.setdefault(c.short_leg.strike, set()).add(evidence["short_quality_score"])
    assert all(len(scores) == 1 for scores in grouped.values())


def test_missing_context_not_fabricated_and_retention_breaks_equal_short_quality():
    evidence = short_leg_quality(price=80, premium_reference=80, distance=300, distance_scale=100,
        oi=5000, min_oi=100, volume=None, min_volume=1, spread_pct=1, max_spread_pct=30)
    for key in ("structure", "directional_fit", "survival"):
        assert evidence["short_quality_components"][key] is None
    assert construction_key(short_quality=80, retention=.875, width=400, identity="a") < construction_key(
        short_quality=80, retention=.25, width=100, identity="b")
    assert construction_key(short_quality=90, retention=.25, width=100, identity="a") < construction_key(
        short_quality=80, retention=.875, width=400, identity="b")


def test_zero_spread_limit_supported_without_division_by_zero():
    evidence = short_leg_quality(price=80, premium_reference=80, distance=300, distance_scale=100,
        oi=5000, min_oi=100, volume=500, min_volume=1, spread_pct=0, max_spread_pct=0)
    assert evidence["short_quality_components"]["execution"] == 1
