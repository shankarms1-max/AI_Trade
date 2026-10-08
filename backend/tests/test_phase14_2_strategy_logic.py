"""Offline economics, eligibility, risk, persistence and causal replay checks."""

from dataclasses import replace
from datetime import datetime, time, timedelta
from math import exp, sqrt
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func
from alembic import command
from alembic.config import Config

from app.core.config import Settings
from app.regime.models import (
    MarketBias,
    DirectionalStrength,
    Regime,
    StrategyFamilyEligibility,
)
from app.regime.engine import RegimeConfig, classify_regime
from app.regime.market_state import market_state
from app.strategy.policy import CreditSpreadPolicy, LOGIC_VERSION, policy_from_settings
from app.strategy.candidate_engine import StrategyConfig, generate_candidates
from app.strategy.service import config_from_settings
from app.strategy.economics import (
    expiry_context,
    expected_move,
    required_buffer,
    cost_estimate,
)
from app.strategy.economics_models import StrategyFamily
from app.risk.engine import evaluate_risk
from app.risk.limits import RiskConfig
from app.risk.models import EvaluationContext
from app.risk.exposure import ResearchRiskStateProvider, RiskState
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.risk.candidate_checks import strategy_fingerprint
from app.shadow.entry import create_shadow_entry
from app.shadow.analytics import breakdown
from app.shadow.repository import ShadowRepository
from app.shadow.exits import ShadowConfig
from app.alpha.experiments import (
    ExperimentParameters,
    bounded_parameter_grid,
    evaluate_parameters,
)
from app.alpha.replay import ReplayRow, replay_parameters
from app.ai.input_builder import build_ai_research_input
from app.ai.prompts import (
    PHASE14_2_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    system_prompt,
    user_payload,
)
from app.features.repository import FeatureRepository
from app.regime.repository import RegimeRepository
from app.strategy.repository import StrategyRepository
from app.risk.repository import RiskRepository
from app.db.models import MarketRegimeSnapshotRecord, StrategyCandidateSetRecord
from app.collector.service import collection_bucket
from tests.test_phase6_strategy import phase6_context


@pytest.fixture
def context(market_snapshot):
    raw, feature, regime = phase6_context(market_snapshot)
    observed = raw.timestamp_ist
    expiry = observed.date() + timedelta(days=3)
    options = []
    for strike in range(23500, 26501, 50):
        for kind in ("CE", "PE"):
            seed = next(c for c in raw.options if c.option_type.value == kind)
            premium = 200 * exp(-abs(strike - 25000) / 500)
            options.append(
                seed.model_copy(
                    update=dict(
                        strike=strike,
                        expiry=expiry,
                        instrument_token=f"{strike}{kind}",
                        trading_symbol=f"NIFTY{strike}{kind}",
                        ltp=premium,
                        bid=premium - 0.5,
                        ask=premium + 0.5,
                        source_market_timestamp=observed,
                        bid_quantity=500,
                        ask_quantity=500,
                    )
                )
            )
    raw = raw.model_copy(
        update=dict(expiry=expiry, india_vix=10, options=options, lot_size=50)
    )
    feature = feature.model_copy(
        update=dict(
            expiry=expiry,
            volatility_features=feature.volatility_features.model_copy(
                update=dict(india_vix=10, vix_available=True, vix_regime="LOW")
            ),
            data_quality=feature.data_quality.model_copy(
                update=dict(
                    intraday_oi_usable=True,
                    intraday_volume_usable=True,
                    vix_available=True,
                )
            ),
            price_structure_features=feature.price_structure_features.model_copy(
                update=dict(
                    spot_change_from_previous_snapshot=5,
                    spot_change_pct_from_previous_snapshot=0.02,
                    collector_observed_high=25020,
                    collector_observed_low=24980,
                )
            ),
            support_resistance=feature.support_resistance.model_copy(
                update=dict(
                    potential_support_clusters=[
                        c.model_copy(update=dict(strength_score=0.9))
                        for c in feature.support_resistance.potential_support_clusters
                    ],
                    potential_resistance_clusters=[
                        c.model_copy(update=dict(strength_score=0.9))
                        for c in feature.support_resistance.potential_resistance_clusters
                    ],
                )
            ),
        )
    )
    prior = feature.model_copy(
        update=dict(snapshot_id=0, timestamp=observed - timedelta(minutes=3))
    )
    p = CreditSpreadPolicy(enabled=True)
    state = market_state(regime, feature, prior, p)
    regime = regime.model_copy(update=state)
    return raw, feature, regime, prior


def strategy_config(policy=None, **updates):
    return StrategyConfig(
        credit_spread_policy=policy or CreditSpreadPolicy(enabled=True),
        allowed_spread_widths=(100, 200, 300, 400),
        max_candidates=50,
        **updates,
    )


def generate(context, policy=None, **updates):
    raw, feature, regime, _ = context
    return generate_candidates(
        raw, feature, regime, 10, strategy_config(policy, **updates)
    )


def risk(context, candidates=None, config=None, state=None, **kwargs):
    raw, feature, regime, _ = context
    return evaluate_risk(
        candidates or generate(context),
        99,
        raw,
        feature,
        regime,
        config
        or RiskConfig(
            credit_spread_policy=CreditSpreadPolicy(enabled=True),
            max_spread_width=400,
            max_loss_per_trade=100000,
            max_capital_per_trade=100000,
            required_consecutive_directional_snapshots=1,
        ),
        kwargs.pop("evaluation_context", EvaluationContext.HISTORICAL),
        state or ResearchRiskStateProvider(),
        ConfiguredMarketEventProvider(),
        **kwargs,
    )


@pytest.mark.parametrize(
    "bull,bear,range_score,bias,strength",
    [
        (9, 1, 0, "BULLISH", "STRONG"),
        (7, 3, 0, "BULLISH", "MODERATE"),
        (3, 1, 3, "BULLISH", "WEAK"),
        (1, 1, 8, "NEUTRAL", "NONE"),
        (7, 7, 0, "CONFLICT", "NONE"),
        (1, 9, 0, "BEARISH", "STRONG"),
    ],
)
def test_bias_strength_separate_from_confidence(
    context, bull, bear, range_score, bias, strength
):
    _, f, r, prior = context
    r = r.model_copy(
        update=dict(
            bull_score=bull, bear_score=bear, range_score=range_score, confidence=10
        )
    )
    state = market_state(r, f, prior, CreditSpreadPolicy(enabled=True))
    assert state["market_bias"] == bias and state["directional_strength"] == strength
    assert state["strategy_logic_version"] == LOGIC_VERSION


@pytest.mark.parametrize(
    "mutation", ["quality", "oi", "history", "session", "expiry", "gap", "future"]
)
def test_insufficient_continuity_is_never_relaxed(context, mutation):
    _, f, r, prior = context
    if mutation == "quality":
        r = r.model_copy(update={"evidence_quality": type(r.evidence_quality).LOW})
    if mutation == "oi":
        f = f.model_copy(
            update={
                "data_quality": f.data_quality.model_copy(
                    update={"static_oi_usable": False}
                )
            }
        )
    if mutation == "history":
        prior = None
    if mutation == "session":
        prior = prior.model_copy(
            update={"timestamp": prior.timestamp - timedelta(days=1)}
        )
    if mutation == "expiry":
        prior = prior.model_copy(update={"expiry": prior.expiry + timedelta(days=1)})
    if mutation == "gap":
        prior = prior.model_copy(
            update={"timestamp": prior.timestamp - timedelta(minutes=11)}
        )
    if mutation == "future":
        prior = prior.model_copy(
            update={"timestamp": f.timestamp + timedelta(minutes=1)}
        )
    state = market_state(
        r, f, prior, CreditSpreadPolicy(enabled=True, theta_carry_enabled=True)
    )
    assert (
        state["market_bias"] == "INSUFFICIENT"
        and state["strategy_family_eligibility"] == "NONE"
    )


@pytest.mark.parametrize(
    "strength,theta,family",
    [
        ("STRONG", False, "DIRECTIONAL_ONLY"),
        ("MODERATE", False, "DIRECTIONAL_ONLY"),
        ("WEAK", True, "THETA_CARRY_ONLY"),
        ("NONE", True, "THETA_CARRY_ONLY"),
        ("STRONG", True, "BOTH"),
        ("WEAK", False, "NONE"),
    ],
)
def test_family_eligibility_matrix(context, strength, theta, family):
    _, f, r, prior = context
    scores = {
        "STRONG": (9, 1, 0),
        "MODERATE": (7, 3, 0),
        "WEAK": (3, 1, 3),
        "NONE": (1, 1, 8),
    }
    bull, bear, range_score = scores[strength]
    p = CreditSpreadPolicy(
        enabled=True, theta_carry_enabled=theta, conflict_min_score=4
    )
    state = market_state(
        r.model_copy(
            update=dict(bull_score=bull, bear_score=bear, range_score=range_score)
        ),
        f,
        prior,
        p,
    )
    assert state["strategy_family_eligibility"] == family


@pytest.mark.parametrize("bias", ["CONFLICT", "INSUFFICIENT"])
def test_no_family_for_conflict_or_insufficient(context, bias):
    raw, f, r, prior = context
    result = generate(
        (raw, f, r.model_copy(update=dict(market_bias=MarketBias(bias))), prior)
    )
    assert not result.eligible


def test_moderate_direction_produces_candidates_without_confirmed_alpha(context):
    raw, f, r, prior = context
    r = r.model_copy(
        update=dict(
            market_bias=MarketBias.BULLISH,
            directional_strength=DirectionalStrength.MODERATE,
            directional_score=50,
            confidence=20,
        )
    )
    result = generate((raw, f, r, prior))
    assert result.candidates
    assert all(c.required_short_strike_buffer == 150 for c in result.candidates)
    assert all(
        c.survival_score >= 60 and c.carry_score >= 30 for c in result.candidates
    )


def test_theta_disabled_blocks_weak_candidate(context):
    raw, f, r, prior = context
    r = r.model_copy(
        update=dict(
            directional_strength=DirectionalStrength.WEAK,
            strategy_family_eligibility=StrategyFamilyEligibility.THETA_CARRY_ONLY,
        )
    )
    assert not generate((raw, f, r, prior)).eligible


@pytest.mark.parametrize("source", ["VIX", "ATM_STRADDLE", "AUTO"])
def test_expected_move_sources(context, source):
    raw, f, _, _ = context
    frac = expiry_context(raw, CreditSpreadPolicy())["fractional_time_to_expiry"]
    move = expected_move(raw, f, frac, source)
    expected = raw.nifty_spot * 0.10 * sqrt(frac / 365) if source == "VIX" else 400
    assert move["expected_move_points"] == pytest.approx(expected)
    assert move["expected_move_percent"] == pytest.approx(expected / 25000 * 100)


@pytest.mark.parametrize(
    "invalid", ["missing", "zero", "negative", "nan", "crossed", "stale"]
)
def test_expected_move_unavailable_or_invalid(context, invalid):
    raw, f, _, _ = context
    if invalid in {"missing", "zero", "negative", "nan"}:
        vix = {"missing": None, "zero": 0, "negative": -1, "nan": float("nan")}[invalid]
        f = f.model_copy(
            update=dict(
                volatility_features=f.volatility_features.model_copy(
                    update=dict(india_vix=vix)
                )
            )
        )
        assert expected_move(raw, f, 3, "VIX") is None
    else:
        changes = (
            dict(bid=500, ask=1)
            if invalid == "crossed"
            else dict(source_market_timestamp=raw.timestamp_ist - timedelta(hours=1))
        )
        raw = raw.model_copy(
            update=dict(options=[c.model_copy(update=changes) for c in raw.options])
        )
        assert expected_move(raw, f, 3, "ATM_STRADDLE") is None


@pytest.mark.parametrize("fractional", [0, -1, float("nan")])
def test_expired_expected_move_invalid(context, fractional):
    raw, f, _, _ = context
    assert expected_move(raw, f, fractional) is None


def test_expected_move_units(context):
    c = generate(context).candidates[0]
    assert c.distance_in_expected_move_units == pytest.approx(
        c.short_strike_distance_points / c.expected_move_points
    )
    assert (
        c.short_strike_distance_expected_move_units == c.distance_in_expected_move_units
    )


@pytest.mark.parametrize(
    "days,bucket",
    [
        (0, "LE_0_DTE"),
        (1, "LE_1_DTE"),
        (3, "LE_3_DTE"),
        (5, "LE_7_DTE"),
        (10, "GT_7_DTE"),
    ],
)
def test_dte_buckets_and_calendar_time(context, days, bucket):
    raw, _, _, _ = context
    raw = raw.model_copy(
        update=dict(expiry=raw.timestamp_ist.date() + timedelta(days=days))
    )
    result = expiry_context(raw, CreditSpreadPolicy())
    assert result["days_to_expiry"] == days and result["dte_bucket"] == bucket
    assert days < result["fractional_time_to_expiry"] < days + 1


def test_expiry_boundary_and_local_date(context):
    raw, _, _, _ = context
    close = datetime.combine(raw.expiry, time(15, 30), raw.timestamp_ist.tzinfo)
    assert (
        expiry_context(
            raw.model_copy(update=dict(timestamp_ist=close)), CreditSpreadPolicy()
        )["fractional_time_to_expiry"]
        == 0
    )
    utc = close.astimezone(__import__("datetime").timezone.utc)
    assert (
        expiry_context(
            raw.model_copy(update=dict(timestamp_ist=utc)), CreditSpreadPolicy()
        )["days_to_expiry"]
        == 0
    )


def test_survival_distance_and_structure_monotonic(context):
    result = generate(context)
    at_width = sorted(
        [c for c in result.candidates if c.spread_width == 300],
        key=lambda c: c.short_strike_distance_points,
    )
    assert len(at_width) > 1
    closer, farther = at_width[0], at_width[-1]
    assert (
        farther.survival_components["distance"]
        >= closer.survival_components["distance"]
    )
    assert (
        farther.survival_components["structure"]
        >= closer.survival_components["structure"]
    )
    assert (
        farther.survival_components["expected_move"]
        >= closer.survival_components["expected_move"]
    )
    assert farther.distance_beyond_structure_points == pytest.approx(
        24750 - farther.short_leg.strike
    )


def test_high_volatility_reduces_survival(context):
    raw, f, r, prior = context
    high = f.model_copy(
        update=dict(
            volatility_features=f.volatility_features.model_copy(
                update=dict(vix_regime="HIGH", india_vix=30)
            )
        )
    )
    result = generate(context)
    altered = generate((raw, high, r, prior))
    by_key = {(c.short_leg.strike, c.spread_width): c for c in result.candidates}
    overlap = [
        (by_key[(c.short_leg.strike, c.spread_width)], c)
        for c in altered.candidates
        if (c.short_leg.strike, c.spread_width) in by_key
    ]
    assert overlap
    assert all(
        b.survival_components["volatility"] < a.survival_components["volatility"]
        for a, b in overlap
    )


@pytest.mark.parametrize("bad", ["oi", "volume", "crossed", "spread", "stale"])
def test_bad_liquidity_quotes_never_qualify(context, bad):
    raw, f, r, prior = context
    update = {
        "oi": {"open_interest": 0},
        "volume": {"volume": 0},
        "crossed": {"bid": 100, "ask": 1},
        "spread": {"bid": 1, "ask": 100},
        "stale": {"source_market_timestamp": raw.timestamp_ist - timedelta(hours=1)},
    }[bad]
    raw = raw.model_copy(
        update=dict(options=[c.model_copy(update=update) for c in raw.options])
    )
    assert not generate((raw, f, r, prior)).eligible


@pytest.mark.parametrize("width", [100, 150, 200, 250, 300, 350, 400])
def test_configurable_width_payoff_and_risk(context, width):
    candidates = generate_candidates(
        context[0],
        context[1],
        context[2],
        10,
        replace(strategy_config(), allowed_spread_widths=(width,)),
    )
    assert candidates.candidates
    c = candidates.candidates[0]
    assert c.spread_width == width == abs(c.short_leg.strike - c.long_leg.strike)
    assert c.net_credit == pytest.approx(c.short_leg.bid - c.long_leg.ask)
    assert c.max_loss_per_unit == pytest.approx(width - c.net_credit)
    assert c.max_loss_per_lot == pytest.approx((width - c.net_credit) * 50)
    decision = risk(context, candidates).decisions[0]
    assert decision.decision == "APPROVED"
    assert decision.estimated_capital_required == pytest.approx(c.max_loss_per_lot)


def test_carry_components_and_costs(context):
    policy = replace(
        CreditSpreadPolicy(enabled=True),
        brokerage_per_order=20,
        exchange_rate=0.001,
        stt_rate=0.002,
        gst_rate=0.18,
        stamp_rate=0.001,
        slippage_points_per_leg=0.2,
    )
    candidates = generate(context, policy)
    assert candidates.candidates
    c = candidates.candidates[0]
    assert c.net_credit_after_cost == pytest.approx(c.net_credit - c.estimated_cost)
    assert c.credit_to_width_ratio == pytest.approx(c.net_credit / c.spread_width)
    assert c.credit_to_max_loss == pytest.approx(c.net_credit / c.max_loss)
    assert c.credit_per_dte == pytest.approx(c.net_credit / c.fractional_time_to_expiry)
    assert set(c.carry_components) == set(policy.carry_weights)
    assert c.carry_model == "TRANSPARENT_CARRY_PROXY_NO_GREEKS"
    assert not c.cost_estimate_complete and "COST_ESTIMATE_INCOMPLETE" in c.warnings
    assert c.ranking_penalties["cost_burden"] > 0


def test_roundtrip_cost_components_are_explicit():
    p = CreditSpreadPolicy(
        brokerage_per_order=20,
        exchange_rate=0.001,
        stt_rate=0.002,
        gst_rate=0.18,
        stamp_rate=0.001,
        slippage_points_per_leg=0.2,
    )
    cost, parts = cost_estimate(60, 20, 50, p)
    assert parts["brokerage"] == 1.6 and parts["exchange_charges"] == 0.16
    assert parts["stt"] == 0.16 and parts["stamp_duty"] == 0.08
    assert cost == pytest.approx(1.6 + 0.16 + 0.16 + 0.08 + (1.6 + 0.16) * 0.18 + 0.8)


def test_poor_credit_or_cost_burden_disqualifies(context):
    assert not generate(
        context, replace(CreditSpreadPolicy(enabled=True), slippage_points_per_leg=1000)
    ).eligible
    assert not generate(context, min_credit_to_width_ratio=0.9).eligible


def test_near_expiry_carry_and_gamma_are_separate(context):
    raw, f, r, prior = context
    raw = raw.model_copy(
        update=dict(
            expiry=raw.timestamp_ist.date(),
            options=[
                c.model_copy(update=dict(expiry=raw.timestamp_ist.date()))
                for c in raw.options
            ],
        )
    )
    f = f.model_copy(update=dict(expiry=raw.expiry))
    near = generate((raw, f, r, prior))
    assert near.candidates
    base = generate(context)
    keys = {(c.short_leg.strike, c.spread_width): c for c in base.candidates}
    overlaps = [
        (keys[(c.short_leg.strike, c.spread_width)], c)
        for c in near.candidates
        if (c.short_leg.strike, c.spread_width) in keys
    ]
    assert overlaps
    assert all(
        c.gamma_risk_state == "HIGH" and c.theta_gamma_balance_state == "UNFAVORABLE"
        for c in near.candidates
    )
    assert all(
        n.carry_components["credit_dte"] >= b.carry_components["credit_dte"]
        for b, n in overlaps
    )
    assert all(
        n.ranking_penalties["gamma_risk"] > b.ranking_penalties["gamma_risk"]
        for b, n in overlaps
    )
    assert all(
        n.survival_components["dte"] < b.survival_components["dte"] for b, n in overlaps
    )


def test_missing_volatility_fails_closed(context):
    raw, f, r, prior = context
    f = f.model_copy(
        update=dict(
            volatility_features=f.volatility_features.model_copy(
                update=dict(vix_regime=None)
            )
        )
    )
    assert "VOLATILITY_CONTEXT_UNAVAILABLE" in generate((raw, f, r, prior)).reason_codes


@pytest.mark.parametrize(
    "strength,family,vol,dte,expected",
    [
        ("STRONG", "DIRECTIONAL_CREDIT_SPREAD", "LOW", 3, 100),
        ("MODERATE", "DIRECTIONAL_CREDIT_SPREAD", "LOW", 3, 150),
        ("WEAK", "THETA_CARRY_CREDIT_SPREAD", "LOW", 3, 200),
        ("MODERATE", "DIRECTIONAL_CREDIT_SPREAD", "HIGH", 3, 225),
        ("STRONG", "DIRECTIONAL_CREDIT_SPREAD", "LOW", 0.5, 150),
    ],
)
def test_adaptive_distance_policy(strength, family, vol, dte, expected):
    assert (
        required_buffer(strategy_config(), strength, family, vol, dte, 25000)
        == expected
    )


def test_component_ranking_reconciles(context):
    p = CreditSpreadPolicy(enabled=True)
    result = generate(context, p)
    assert result.candidates == sorted(
        result.candidates,
        key=lambda c: (
            -c.selection_score,
            c.strategy_family.value,
            c.short_leg.strike,
            c.spread_width,
        ),
    )
    for c in result.candidates:
        raw = (
            100
            * sum(c.ranking_components[k] * v for k, v in p.ranking_weights.items())
            / sum(p.ranking_weights.values())
        )
        # The former vertical score remains auditable, but no longer selects
        # hedges. New selection_score is the explicit short-first ordinal rank.
        assert c.construction_evidence["prior_vertical_score"] == pytest.approx(
            max(0, round(raw, 4) - sum(c.ranking_penalties.values())), abs=0.0001
        )
        evidence = c.construction_evidence
        assert c.selection_score == pytest.approx(100 * (
            evidence["eligible_pair_count"]-evidence["ordinal_rank"]+1) / evidence["eligible_pair_count"])


def test_cost_and_liquidity_reduce_ranking(context):
    raw, f, r, prior = context
    base = generate(context)
    costly = generate(
        context, replace(CreditSpreadPolicy(enabled=True), slippage_points_per_leg=0.1)
    )
    low = raw.model_copy(
        update=dict(
            options=[c.model_copy(update=dict(open_interest=100)) for c in raw.options]
        )
    )
    poorer = generate((low, f, r, prior))
    base_by_key = {(c.short_leg.strike, c.spread_width): c for c in base.candidates}
    for results in (costly, poorer):
        overlap = [
            (base_by_key[(c.short_leg.strike, c.spread_width)], c)
            for c in results.candidates
            if (c.short_leg.strike, c.spread_width) in base_by_key
        ]
        assert overlap
        assert all(b.construction_evidence["prior_vertical_score"] >
                   a.construction_evidence["prior_vertical_score"] for b, a in overlap)


def test_no_alpha_or_confidence_only_hard_veto(context):
    raw, f, r, prior = context
    r = r.model_copy(
        update=dict(
            confidence=1,
            directional_strength=DirectionalStrength.MODERATE,
            directional_score=50,
        )
    )
    result = risk((raw, f, r, prior))
    assert result.approved_count > 0
    assert all(
        "REGIME_CONFIDENCE_TOO_LOW" not in d.reason_codes for d in result.decisions
    )


@pytest.mark.parametrize(
    "limit,reason",
    [
        ("max_loss_per_trade", "MAX_LOSS_EXCEEDED"),
        ("max_capital_per_trade", "MAX_CAPITAL_EXCEEDED"),
    ],
)
@pytest.mark.parametrize("width", [300, 400])
def test_wider_spread_monetary_veto(context, limit, reason, width):
    candidates = generate_candidates(
        context[0],
        context[1],
        context[2],
        10,
        replace(strategy_config(), allowed_spread_widths=(width,)),
    )
    cfg = RiskConfig(
        credit_spread_policy=CreditSpreadPolicy(enabled=True),
        max_spread_width=400,
        max_loss_per_trade=100000,
        max_capital_per_trade=100000,
        required_consecutive_directional_snapshots=1,
    )
    result = risk(context, candidates, replace(cfg, **{limit: 100}))
    assert result.approved_count == 0
    assert reason in result.decisions[0].reason_codes


def test_explicit_width_cap_remains_strict(context):
    result = risk(
        context,
        config=RiskConfig(
            credit_spread_policy=CreditSpreadPolicy(enabled=True),
            max_spread_width=200,
            max_loss_per_trade=100000,
            max_capital_per_trade=100000,
            required_consecutive_directional_snapshots=1,
        ),
    )
    widths = {c.candidate_id: c.spread_width for c in generate(context).candidates}
    assert all(
        d.decision == "REJECTED"
        for d in result.decisions
        if widths[d.candidate_reference] > 200
    )


def test_stale_live_snapshot_is_vetoed(context):
    raw, _, _, _ = context
    result = risk(
        context,
        evaluation_context=EvaluationContext.LIVE,
        evaluated_at=raw.timestamp_ist + timedelta(hours=1),
    )
    assert (
        result.approved_count == 0
        and "SNAPSHOT_STALE" in result.decisions[0].reason_codes
    )


def test_invalid_structure_and_duplicate_still_veto(context):
    candidates = generate(context)
    c = candidates.candidates[0]
    broken = c.model_copy(
        update=dict(long_leg=c.long_leg.model_copy(update=dict(action="SELL")))
    )
    only = candidates.model_copy(update=dict(candidates=[broken], candidate_count=1))
    assert "INVALID_SPREAD_STRUCTURE" in risk(context, only).decisions[0].reason_codes
    key = strategy_fingerprint(c, context[0].timestamp_ist.date())
    provider = SimpleNamespace(
        get_state=lambda _: RiskState(
            0, 0, frozenset({key}), False, False, "RESEARCH", True
        )
    )
    assert (
        "DUPLICATE_STRATEGY"
        in risk(context, candidates, state=provider).decisions[0].reason_codes
    )


@pytest.mark.parametrize(
    "safer,expected",
    [("down", "BULL_PUT_SPREAD"), ("up", "BEAR_CALL_SPREAD"), ("symmetric", None)],
)
@pytest.mark.parametrize(
    "strength", [DirectionalStrength.NONE, DirectionalStrength.WEAK]
)
def test_neutral_asymmetric_side_selection(context, safer, expected, strength):
    raw, f, r, prior = context
    p = CreditSpreadPolicy(enabled=True, theta_carry_enabled=True)
    r = r.model_copy(
        update=dict(
            market_bias=MarketBias.NEUTRAL,
            directional_strength=strength,
            directional_score=0,
            strategy_family_eligibility=StrategyFamilyEligibility.THETA_CARRY_ONLY,
        )
    )
    sr = f.support_resistance
    support = sr.potential_support_clusters[0].model_copy(
        update=dict(
            strength_score=0.9 if safer == "down" else 0.3,
            low_strike=24750,
            high_strike=24800,
            center_strike=24775,
        )
    )
    resistance = sr.potential_resistance_clusters[0].model_copy(
        update=dict(
            strength_score=0.9 if safer == "up" else 0.3,
            low_strike=25200,
            high_strike=25250,
            center_strike=25225,
        )
    )
    f = f.model_copy(
        update=dict(
            support_resistance=sr.model_copy(
                update=dict(
                    potential_support_clusters=[support],
                    potential_resistance_clusters=[resistance],
                )
            ),
            price_structure_features=f.price_structure_features.model_copy(
                update=dict(spot_change_from_previous_snapshot=0)
            ),
        )
    )
    result = generate((raw, f, r, prior), p)
    assert result.eligible is (expected is not None)
    if expected:
        assert all(
            c.strategy_type == expected
            and c.strategy_family == "THETA_CARRY_CREDIT_SPREAD"
            for c in result.candidates
        )
        assert all(c.required_short_strike_buffer == 200 for c in result.candidates)
        cfg = RiskConfig(
            credit_spread_policy=p,
            max_spread_width=400,
            max_loss_per_trade=100000,
            max_capital_per_trade=100000,
            required_consecutive_directional_snapshots=1,
        )
        assert risk((raw, f, r, prior), result, cfg).approved_count > 0


def test_opposing_strong_alpha_vetoes_carry(context):
    raw, f, r, prior = context
    p = CreditSpreadPolicy(
        enabled=True, theta_carry_enabled=True, family_mode="THETA_CARRY_ONLY"
    )
    r = r.model_copy(
        update=dict(
            statistical_alpha_bias="BEARISH",
            statistical_alpha_strength="STRONG",
            strategy_family_eligibility=StrategyFamilyEligibility.THETA_CARRY_ONLY,
        )
    )
    assert not generate((raw, f, r, prior), p).eligible


def shadow_entry(context):
    raw, f, r, _ = context
    candidates = generate(context)
    decisions = risk(context, candidates).decisions
    return create_shadow_entry(
        1,
        raw,
        f,
        r,
        candidates,
        list(enumerate(decisions, 1)),
        trades_today=0,
        open_trade_exists=False,
        max_new_trades_per_day=1,
        allow_multiple_open_trades=False,
    ).trade


def test_shadow_metadata_preserves_economics(context):
    trade = shadow_entry(context)
    assert trade
    for key in (
        "strategy_family",
        "directional_strength",
        "survival_score",
        "carry_score",
        "gamma_risk_state",
        "theta_gamma_balance_state",
        "expected_move_points",
        "distance_in_expected_move_units",
        "days_to_expiry",
        "spread_width_points",
        "distance_beyond_structure_points",
        "estimated_cost",
    ):
        assert getattr(trade, key) == trade.source_candidate[key]
    assert trade.strategy_logic_version == LOGIC_VERSION


@pytest.mark.parametrize(
    "dimension",
    [
        "strategy_family",
        "spread_width",
        "dte_bucket",
        "carry_bucket",
        "survival_bucket",
        "expected_move_distance",
        "gamma_risk_state",
        "theta_gamma_state",
    ],
)
def test_analytics_dimensions(context, dimension):
    trade = shadow_entry(context)
    report = breakdown([trade])
    assert report[dimension]
    assert sum(bucket["total_trades"] for bucket in report[dimension].values()) == 1


def test_width_analytics_do_not_pool(context):
    trade = shadow_entry(context)
    report = breakdown(
        [trade.model_copy(update=dict(spread_width=w)) for w in (100, 200, 300, 400)]
    )
    assert set(report["spread_width"]) == {"100", "200", "300", "400"}


@pytest.mark.parametrize(
    "axis,values,field",
    [
        ("spread_width_sets", [(300,), (400,)], "spread_widths"),
        (
            "strategy_family_modes",
            ["DIRECTIONAL_ONLY", "THETA_CARRY_ONLY"],
            "strategy_family_mode",
        ),
        ("expected_move_minimums", [1, 2], "expected_move_min_distance_units"),
        ("carry_thresholds", [30, 50], "minimum_carry_score"),
        ("directional_strengths", ["MODERATE", "STRONG"], "directional_min_strength"),
        ("dte_buckets", ["LE_1_DTE", "LE_3_DTE"], "dte_bucket"),
    ],
)
def test_experiment_axes_require_full_replay(context, axis, values, field):
    grid = bounded_parameter_grid(
        [0.8], [1], [(time(9, 35), time(13, 30))], 10, **{axis: values}
    )
    assert [getattr(p, field) for p in grid] == values
    result = evaluate_parameters([shadow_entry(context)], grid[0], 1)
    assert result["status"] == "NOT_EVALUABLE" and result["net_pnl"] is None


def test_replay_phase142_does_not_require_universal_strong_alpha(context):
    raw, f, r, prior = context
    # Real classification uses causal prior features; quote path determines evaluability.
    rows = []
    for i in range(5):
        point = raw.model_copy(
            update=dict(
                timestamp_ist=raw.timestamp_ist + timedelta(minutes=3 * i),
                options=[
                    c.model_copy(
                        update=dict(
                            source_market_timestamp=raw.timestamp_ist
                            + timedelta(minutes=3 * i)
                        )
                    )
                    for c in raw.options
                ],
            )
        )
        feature = f.model_copy(
            update=dict(snapshot_id=i + 1, timestamp=point.timestamp_ist)
        )
        rows.append(ReplayRow(i + 1, point, feature, i + 1, None))
    params = ExperimentParameters(
        0.8,
        1,
        time(9, 35),
        time(13, 30),
        spread_widths=(300, 400),
        expected_move_min_distance_units=0.5,
    )
    cfg = RiskConfig(
        credit_spread_policy=CreditSpreadPolicy(enabled=True),
        max_spread_width=400,
        max_loss_per_trade=100000,
        max_capital_per_trade=100000,
        required_consecutive_directional_snapshots=1,
    )
    report = replay_parameters(
        rows,
        params,
        strategy_config(),
        cfg,
        RegimeConfig(credit_spread_policy=CreditSpreadPolicy(enabled=True)),
        ShadowConfig(),
    )
    assert report["status"] == "NOT_EVALUABLE" and report["net_pnl"] is None
    assert report["fill_method"] == "NEXT_OBSERVATION_SIMULATION"


@pytest.mark.parametrize(
    "term",
    [
        "Strong direction is not mandatory",
        "Moderate alpha does not mean no trade",
        "gamma risk",
        "never override candidate ranking",
        "300/400",
        "not expected return",
    ],
)
def test_ai_philosophy_and_authority(term):
    assert term in PHASE14_2_SYSTEM_PROMPT


def test_ai_input_contains_current_economics(context):
    _, f, r, _ = context
    payload = build_ai_research_input(f, r, candidate_set=generate(context))
    assert payload.phase14_2["candidates"][0]["carry_score"] is not None
    assert payload.phase14_2["risk_authority"] == "DETERMINISTIC_VETO_ONLY"
    assert system_prompt(payload) == PHASE14_2_SYSTEM_PROMPT
    legacy = payload.model_copy(update=dict(phase14_2=None))
    assert system_prompt(legacy) == SYSTEM_PROMPT and '"phase14_2"' not in user_payload(
        legacy
    )


def test_flags_and_legacy_behavior_unchanged(market_snapshot):
    settings = Settings(kotak_consumer_key="test", _env_file=None)
    assert (
        not settings.phase14_2_strategy_logic_enabled
        and not settings.theta_carry_enabled
    )
    assert (
        not settings.alpha_engine_enabled and not settings.regime_use_statistical_alpha
    )
    assert (
        not settings.strategy_volatility_buffer_enabled
        and not settings.pipeline_run_ai_research
    )
    assert settings.configured_strategy_widths == (50, 100, 150, 200, 300, 400)
    raw, f, r = phase6_context(market_snapshot)
    default = generate_candidates(raw, f, r, 10)
    actual = generate_candidates(raw, f, r, 10, config_from_settings(settings))
    assert [c.model_dump(exclude={"created_at"}) for c in actual.candidates] == [
        c.model_dump(exclude={"created_at"}) for c in default.candidates
    ]
    assert (
        actual.strategy_version == "phase6_v1" and actual.strategy_logic_version is None
    )
    assert all(c.strategy_family is None for c in actual.candidates)


def test_new_width_and_policy_settings():
    settings = Settings(
        kotak_consumer_key="test",
        _env_file=None,
        phase14_2_strategy_logic_enabled=True,
        strategy_allowed_widths_points="100,150,200,250,300,350,400",
    )
    assert settings.configured_strategy_widths == (100, 150, 200, 250, 300, 350, 400)
    assert policy_from_settings(settings).enabled


@pytest.mark.parametrize(
    "update",
    [
        {"strategy_family_mode": "NAKED"},
        {"expected_move_source": "IV"},
        {"survival_weights": {"distance": 1}},
        {"dte_buckets": (3, 1)},
        {"strong_directional_score": 10},
        {"expected_move_min_distance_units": -1},
    ],
)
def test_policy_settings_validation(update):
    with pytest.raises(ValueError):
        Settings(kotak_consumer_key="test", _env_file=None, **update)


def test_versioned_persistence_retains_legacy_records(
    context, repository, session_factory
):
    raw, f, r, _ = context
    run = repository.create_collector_run(raw.timestamp_ist)
    stored = repository.save_market_snapshot(
        raw, collection_bucket(raw.timestamp_ist, 3), run
    )
    f = f.model_copy(update=dict(snapshot_id=stored.snapshot_id))
    feature_id = FeatureRepository(session_factory).upsert(f)
    r = r.model_copy(
        update=dict(snapshot_id=stored.snapshot_id, feature_snapshot_id=feature_id)
    )
    regimes = RegimeRepository(session_factory)
    regimes.upsert(
        r.model_copy(
            update=dict(regime_version="phase4_v1", strategy_logic_version=None)
        )
    )
    regimes.upsert(r)
    regimes.upsert(r)
    new = StrategyRepository(
        session_factory, regime_version=LOGIC_VERSION, strategy_version=LOGIC_VERSION
    )
    assert (
        new.load_context(stored.snapshot_id)[4].strategy_logic_version == LOGIC_VERSION
    )
    current = generate_candidates(raw, f, r, 10, strategy_config())
    new.upsert(current)
    new.upsert(current)
    legacy = generate_candidates(raw, f, r, 10, StrategyConfig())
    new.upsert(legacy)
    assert new.get(stored.snapshot_id)["strategy_version"] == LOGIC_VERSION
    assert (
        StrategyRepository(session_factory).get(stored.snapshot_id)["strategy_version"]
        == "phase6_v1"
    )
    with session_factory() as session:
        assert session.scalar(select(func.count(MarketRegimeSnapshotRecord.id))) == 2
        assert session.scalar(select(func.count(StrategyCandidateSetRecord.id))) == 2
        row = session.scalar(
            select(StrategyCandidateSetRecord).where(
                StrategyCandidateSetRecord.strategy_version == LOGIC_VERSION
            )
        )
        assert row.strategy_context_json["candidates"][0]["survival_score"] > 0
    risk_repo = RiskRepository(
        session_factory, regime_version=LOGIC_VERSION, strategy_version=LOGIC_VERSION
    )
    assert (
        risk_repo.load_context(stored.snapshot_id)[5].strategy_version == LOGIC_VERSION
    )


def test_additive_migration_preserves_rows(tmp_path, monkeypatch, context):
    # Isolated SQLite only; never use configured application/production DB.
    root = Path(__file__).resolve().parents[2]
    url = f"sqlite:///{tmp_path/'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0012_phase14_1")
    from sqlalchemy import create_engine, inspect, Table, MetaData
    from sqlalchemy.orm import sessionmaker
    from app.db.repositories import SnapshotRepository

    engine = create_engine(url)
    sessions = sessionmaker(bind=engine)
    raw, f, _, _ = context
    # Seed the old schema through reflected Core tables; today's ORM includes
    # later additive fields which did not exist at revision 0012.
    with engine.begin() as connection:
        table = Table("market_snapshots", MetaData(), autoload_with=connection)
        values = raw.model_dump(mode="python")
        values["collection_bucket_ist"] = collection_bucket(raw.timestamp_ist, 3)
        result = connection.execute(table.insert().values(**{key: value for key, value in values.items() if key in table.c}))
        stored = SimpleNamespace(snapshot_id=result.inserted_primary_key[0])
        options = Table("option_contract_snapshots", MetaData(), autoload_with=connection)
        for contract in raw.options:
            values = contract.model_dump(mode="python")
            values.update(market_snapshot_id=stored.snapshot_id, option_type=contract.option_type.value)
            connection.execute(options.insert().values(**{key: value for key, value in values.items() if key in options.c}))
    f = f.model_copy(update=dict(snapshot_id=stored.snapshot_id))
    fid = FeatureRepository(sessions).upsert(f)
    old_result = phase6_context(raw)[2].model_dump(mode="json", exclude_none=True)
    with engine.begin() as connection:
        table = Table("market_regime_snapshots", MetaData(), autoload_with=connection)
        connection.execute(
            table.insert().values(
                market_snapshot_id=stored.snapshot_id,
                feature_snapshot_id=fid,
                regime_version="phase4_v1",
                regime="BULLISH",
                confidence=80,
                evidence_quality="HIGH",
                bull_score=8,
                bear_score=1,
                range_score=0,
                result_json=old_result,
            )
        )
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    for table in (
        "market_regime_snapshots",
        "strategy_candidate_sets",
        "shadow_trades",
    ):
        names = {c["name"] for c in inspector.get_columns(table)}
        assert {"strategy_logic_version", "strategy_context_json"} <= names
    with sessions() as session:
        old = session.scalar(select(MarketRegimeSnapshotRecord))
        assert old.result_json == old_result
        assert old.strategy_logic_version is None and old.market_bias is None
    engine.dispose()


@pytest.mark.parametrize(
    "missing,expected", [(False, "EVALUABLE"), (True, "NOT_EVALUABLE")]
)
@pytest.mark.parametrize("width", [300, 400])
def test_full_replay_reconstructs_width_and_requires_exact_exit(
    context, missing, expected, width
):
    raw, f, _, prior = context
    feature = f.model_copy(
        update=dict(
            price_structure_features=f.price_structure_features.model_copy(
                update=dict(
                    spot_change_from_previous_snapshot=150,
                    spot_change_pct_from_previous_snapshot=0.6,
                    future_change_from_previous_snapshot=150,
                    future_change_pct_from_previous_snapshot=0.6,
                    session_open_proxy=24900,
                )
            )
        )
    )
    policy = CreditSpreadPolicy(enabled=True)
    cfg = replace(
        strategy_config(policy), allowed_spread_widths=(width,), max_candidates=1
    )
    # Use real regime, construction and risk. Fill/exit quote observations are controlled.
    regime = classify_regime(
        1, feature, prior, RegimeConfig(credit_spread_policy=policy)
    )
    assert regime.directional_strength in {"STRONG", "MODERATE"}
    candidate = generate_candidates(raw, feature, regime, 10, cfg).candidates[0]
    rows = []
    for i in range(4):
        timestamp = raw.timestamp_ist + timedelta(minutes=3 * i)
        options = [
            c.model_copy(update=dict(source_market_timestamp=timestamp))
            for c in raw.options
        ]
        if i == 3:
            # Valid observed exit captures >50% of the entry credit.
            options = [
                (
                    c.model_copy(update=dict(bid=19, ask=20))
                    if c.instrument_token == candidate.short_leg.instrument_token
                    else (
                        c.model_copy(update=dict(bid=19, ask=20))
                        if c.instrument_token == candidate.long_leg.instrument_token
                        else c
                    )
                )
                for c in options
            ]
            if missing:
                options = [
                    c
                    for c in options
                    if c.instrument_token != candidate.long_leg.instrument_token
                ]
        point = raw.model_copy(update=dict(timestamp_ist=timestamp, options=options))
        current = feature.model_copy(
            update=dict(snapshot_id=i + 1, timestamp=timestamp)
        )
        rows.append(ReplayRow(i + 1, point, current, i + 1, None))
    riskcfg = RiskConfig(
        credit_spread_policy=policy,
        max_spread_width=400,
        max_loss_per_trade=100000,
        max_capital_per_trade=100000,
        required_consecutive_directional_snapshots=1,
    )
    report = replay_parameters(
        rows,
        ExperimentParameters(0.8, 1, time(9, 35), time(13, 30), spread_widths=(width,)),
        cfg,
        riskcfg,
        RegimeConfig(credit_spread_policy=policy),
        ShadowConfig(),
    )
    assert report["status"] == expected
    if not missing:
        assert report["trades"] == 1
        assert set(report["performance_by_width"]) == {str(float(width))}
    else:
        assert report["net_pnl"] is None and report["counts"]["missing"] >= 1


@pytest.mark.parametrize(
    "broken", ["invalid", "missing", "stale", "wrong_contract", "conflict"]
)
def test_alpha_quality_and_identity_stay_strict(context, broken):
    from app.alpha.models import AlphaValidity, JointAlphaDirection

    _, f, r, prior = context
    alpha = SimpleNamespace(
        validity_state=AlphaValidity.VALID,
        joint_alpha_direction=JointAlphaDirection.NEUTRAL,
        timestamp=f.timestamp,
        snapshot_id=f.snapshot_id,
        expiry=f.expiry,
        warnings=[],
    )
    if broken == "invalid":
        alpha.validity_state = AlphaValidity.INVALID
    if broken == "missing":
        alpha = None
    if broken == "stale":
        alpha.timestamp -= timedelta(minutes=3)
    if broken == "wrong_contract":
        alpha.expiry += timedelta(days=1)
    if broken == "conflict":
        alpha.joint_alpha_direction = JointAlphaDirection.CONFLICT
    state = market_state(
        r,
        f,
        prior,
        CreditSpreadPolicy(enabled=True, theta_carry_enabled=True),
        alpha,
        True,
    )
    assert state["strategy_family_eligibility"] == "NONE"


def test_valid_neutral_alpha_allows_moderate_direction(context):
    from app.alpha.models import AlphaValidity, JointAlphaDirection

    _, f, r, prior = context
    alpha = SimpleNamespace(
        validity_state=AlphaValidity.VALID,
        joint_alpha_direction=JointAlphaDirection.NEUTRAL,
        timestamp=f.timestamp,
        snapshot_id=f.snapshot_id,
        expiry=f.expiry,
        warnings=[],
    )
    r = r.model_copy(update=dict(bull_score=7, bear_score=3, range_score=0))
    state = market_state(r, f, prior, CreditSpreadPolicy(enabled=True), alpha, True)
    assert (
        state["directional_strength"] == "MODERATE"
        and state["strategy_family_eligibility"] == "DIRECTIONAL_ONLY"
    )


def test_observed_straddle_skew_fails_closed(context):
    raw, f, _, _ = context
    raw = raw.model_copy(
        update=dict(
            options=[
                (
                    c.model_copy(
                        update=dict(
                            source_market_timestamp=raw.timestamp_ist
                            - timedelta(seconds=30)
                        )
                    )
                    if c.strike == 25000 and c.option_type.value == "CE"
                    else c
                )
                for c in raw.options
            ]
        )
    )
    assert expected_move(raw, f, 3, "ATM_STRADDLE") is None
    assert expected_move(raw, f, 3, "AUTO")["expected_move_source"] == "VIX"


def test_actual_quotes_cannot_be_spoofed_at_hard_risk(context):
    candidates = generate(context)
    c = candidates.candidates[0]
    fake = c.model_copy(
        update=dict(
            short_leg=c.short_leg.model_copy(update=dict(bid=c.short_leg.bid + 1))
        )
    )
    result = risk(
        context,
        candidates.model_copy(update=dict(candidates=[fake], candidate_count=1)),
    )
    assert "STALE_OR_INVALID_EXACT_LEG_QUOTE" in result.decisions[0].reason_codes


@pytest.mark.parametrize(
    "state_update,config_update,reason",
    [
        ({"trades_today": 1}, {}, "MAX_TRADES_REACHED"),
        (
            {"realized_pnl_today": -1000},
            {"max_daily_loss": 500},
            "DAILY_LOSS_LIMIT_REACHED",
        ),
    ],
)
def test_phase142_daily_limits_are_strict(context, state_update, config_update, reason):
    state = RiskState(0, 0, frozenset(), False, False, "RESEARCH", True)
    provider = SimpleNamespace(get_state=lambda _: replace(state, **state_update))
    cfg = RiskConfig(
        credit_spread_policy=CreditSpreadPolicy(enabled=True),
        max_spread_width=400,
        max_loss_per_trade=100000,
        max_capital_per_trade=100000,
        required_consecutive_directional_snapshots=1,
        **config_update,
    )
    result = risk(context, config=cfg, state=provider)
    assert result.approved_count == 0 and reason in result.decisions[0].reason_codes


@pytest.mark.parametrize("gap", [3, 11, 1440])
def test_phase142_confirmation_requires_current_session_continuity(context, gap):
    raw, f, r, _ = context
    cfg = RiskConfig(
        credit_spread_policy=CreditSpreadPolicy(enabled=True),
        max_spread_width=400,
        max_loss_per_trade=100000,
        max_capital_per_trade=100000,
        required_consecutive_directional_snapshots=2,
    )
    older = r.model_copy(update=dict(timestamp=r.timestamp - timedelta(minutes=gap)))
    result = risk(context, config=cfg, prior_regimes=[older])
    assert bool(result.approved_count) == (gap == 3)


def test_phase142_event_veto(context):
    from app.risk.event_checks import MarketEvent

    raw, f, r, _ = context
    events = ConfiguredMarketEventProvider(
        [
            MarketEvent(
                name="Research Event",
                start_time=raw.timestamp_ist - timedelta(minutes=1),
                end_time=raw.timestamp_ist + timedelta(minutes=1),
                severity="HIGH",
                block_entries=True,
            )
        ]
    )
    cfg = RiskConfig(
        credit_spread_policy=CreditSpreadPolicy(enabled=True),
        max_spread_width=400,
        max_loss_per_trade=100000,
        max_capital_per_trade=100000,
        required_consecutive_directional_snapshots=1,
    )
    result = evaluate_risk(
        generate(context),
        99,
        raw,
        f,
        r,
        cfg,
        EvaluationContext.HISTORICAL,
        ResearchRiskStateProvider(),
        events,
    )
    assert (
        result.approved_count == 0
        and "BLOCKING_MARKET_EVENT" in result.decisions[0].reason_codes
    )


def test_phase142_shadow_context_roundtrips_with_approved_risk(
    context, repository, session_factory
):
    raw, f, r, _ = context
    run_id = repository.create_collector_run(raw.timestamp_ist)
    stored = repository.save_market_snapshot(
        raw, collection_bucket(raw.timestamp_ist, 3), run_id
    )
    f = f.model_copy(update=dict(snapshot_id=stored.snapshot_id))
    fid = FeatureRepository(session_factory).upsert(f)
    r = r.model_copy(
        update=dict(snapshot_id=stored.snapshot_id, feature_snapshot_id=fid)
    )
    regime_repo = RegimeRepository(
        session_factory, regime_version=LOGIC_VERSION, strategy_version=LOGIC_VERSION
    )
    rid = regime_repo.upsert(r)
    candidates = generate_candidates(raw, f, r, rid, strategy_config())
    candidate_repo = StrategyRepository(
        session_factory, regime_version=LOGIC_VERSION, strategy_version=LOGIC_VERSION
    )
    cid = candidate_repo.upsert(candidates)
    evaluated = risk((raw, f, r, context[3]), candidates)
    risk_repo = RiskRepository(
        session_factory, regime_version=LOGIC_VERSION, strategy_version=LOGIC_VERSION
    )
    risk_repo.upsert(evaluated, cid)
    shadow_repo = ShadowRepository(
        session_factory, regime_version=LOGIC_VERSION, strategy_version=LOGIC_VERSION
    )
    _, _, _, _, approved, _ = shadow_repo.load_entry_context(stored.snapshot_id)
    trade = create_shadow_entry(
        stored.snapshot_id,
        raw,
        f,
        r,
        candidates,
        approved,
        trades_today=0,
        open_trade_exists=False,
        max_new_trades_per_day=1,
        allow_multiple_open_trades=False,
    ).trade
    assert trade and trade.risk_state == "APPROVED"
    saved = shadow_repo.create_trade(trade)
    payload = shadow_repo.get_trade(saved.id)
    assert payload["carry_score"] == trade.carry_score
    assert payload["strategy_logic_version"] == LOGIC_VERSION
    assert (
        payload["source_candidate"]["survival_components"] == trade.survival_components
    )


def test_extreme_near_expiry_gamma_never_ranks_best(context):
    raw, f, r, prior = context
    timestamp = raw.timestamp_ist.replace(hour=15, minute=0)
    raw = raw.model_copy(
        update=dict(
            timestamp_ist=timestamp,
            expiry=timestamp.date(),
            options=[
                c.model_copy(
                    update=dict(
                        expiry=timestamp.date(), source_market_timestamp=timestamp
                    )
                )
                for c in raw.options
            ],
        )
    )
    f = f.model_copy(update=dict(timestamp=timestamp, expiry=timestamp.date()))
    r = r.model_copy(update=dict(timestamp=timestamp))
    result = generate((raw, f, r, prior))
    assert all(c.gamma_risk_state != "EXTREME" for c in result.candidates)
    # Distance under 0.5 straddle units has extreme gamma context and is excluded.
    assert all(c.distance_in_expected_move_units >= 0.5 for c in result.candidates)


def test_both_families_do_not_duplicate_physical_spreads(context):
    raw, f, r, _ = context
    policy = CreditSpreadPolicy(enabled=True, theta_carry_enabled=True)
    r = r.model_copy(
        update=dict(strategy_family_eligibility=StrategyFamilyEligibility.BOTH)
    )
    result = generate((raw, f, r, context[3]), policy)
    keys = [
        strategy_fingerprint(c, raw.timestamp_ist.date()) for c in result.candidates
    ]
    assert result.candidates and len(keys) == len(set(keys))


def test_pipeline_metadata_and_counts_select_requested_logic(
    context, repository, session_factory
):
    from app.pipeline.repository import PipelineRepository

    raw, f, r, _ = context
    run_id = repository.create_collector_run(raw.timestamp_ist)
    stored = repository.save_market_snapshot(
        raw, collection_bucket(raw.timestamp_ist, 3), run_id
    )
    f = f.model_copy(update=dict(snapshot_id=stored.snapshot_id))
    fid = FeatureRepository(session_factory).upsert(f)
    r = r.model_copy(
        update=dict(snapshot_id=stored.snapshot_id, feature_snapshot_id=fid)
    )
    regimes = RegimeRepository(session_factory)
    legacy_id = regimes.upsert(
        r.model_copy(
            update=dict(regime_version="phase4_v1", strategy_logic_version=None)
        )
    )
    new_id = regimes.upsert(r)
    candidate_repo = StrategyRepository(session_factory)
    legacy = generate_candidates(raw, f, r, legacy_id, StrategyConfig())
    legacy_cid = candidate_repo.upsert(legacy)
    new = generate_candidates(raw, f, r, new_id, strategy_config())
    new_cid = candidate_repo.upsert(new)
    for regime_version, strategy_version, rid, cid in (
        ("phase4_v1", "phase6_v1", legacy_id, legacy_cid),
        (LOGIC_VERSION, LOGIC_VERSION, new_id, new_cid),
    ):
        pipeline = PipelineRepository(
            session_factory,
            regime_version=regime_version,
            strategy_version=strategy_version,
        )
        related = pipeline.related_ids(stored.snapshot_id)
        assert (
            related["regime_snapshot_id"] == rid
            and related["strategy_candidate_set_id"] == cid
        )
        summary = pipeline.daily_summary(raw.timestamp_ist.date())
        assert sum(summary["regime_counts"].values()) == 1
        assert summary["candidate_sets_created"] == 1


def test_explicit_high_vix_policy_is_preserved(context):
    raw, f, r, prior = context
    f = f.model_copy(
        update=dict(
            volatility_features=f.volatility_features.model_copy(
                update=dict(vix_regime="HIGH")
            )
        )
    )
    result = generate(
        (raw, f, r, prior),
        volatility_buffer_enabled=True,
        no_candidate_on_high_vix=True,
    )
    assert "VOLATILITY_POLICY_NO_CANDIDATE" in result.reason_codes
