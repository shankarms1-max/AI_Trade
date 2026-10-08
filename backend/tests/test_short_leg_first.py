"""Deterministic economic examples, never live broker quotes or tuned defaults."""
from dataclasses import replace
from datetime import timedelta

import pytest

from app.core.config import Settings
from app.research.quotes import QuotePolicy
from app.scalper.config import ScalperConfig
from app.scalper.models import ScalperMarketSnapshot, ScalperOptionQuote
from app.scalper.strategy import build_candidates
from app.strategy.candidate_engine import StrategyConfig, generate_candidates
from app.strategy.construction import premium_retention
from app.strategy.policy import CreditSpreadPolicy
from app.regime.models import (Regime, MarketBias, DirectionalStrength,
                               StrategyFamilyEligibility)
from tests.test_phase14_2_strategy_logic import context as phase14_context
from tests.test_scalper import signal_for


@pytest.fixture(params=["phase14", "phase15"])
def engine(request):
    return request.param


@pytest.fixture(params=["BULLISH", "BEARISH"])
def direction(request):
    return request.param


def example(market_snapshot, direction, *, far_ask=10, far_change=None, structure_buffered=True):
    raw, feature, regime, _ = phase14_context.__wrapped__(market_snapshot)
    sign = 1 if direction == "BEARISH" else -1
    kind = "CE" if sign == 1 else "PE"
    short = 25000 + sign*300
    seed = next(item for item in raw.options if item.option_type.value == kind)
    options = []
    # Real-shaped but deliberately artificial OFFLINE fixtures.
    for offset, bid, ask in ((0, 80, 81), (100, 59, 60), (200, 35, 36),
                             (300, 19, 20), (400, far_ask-1, far_ask)):
        strike = short + sign*offset
        change = (far_change or {}) if offset == 400 else {}
        options.append(seed.model_copy(update={"strike": strike, "bid": bid, "ask": ask,
            "ltp": 777, "instrument_token": f"{strike}{kind}", "trading_symbol": f"NIFTY{strike}{kind}",
            "depth_unit": "UNITS", "bid_quantity": 500, "ask_quantity": 500,
            "open_interest": 5000, "volume": 500, **change}))
    raw = raw.model_copy(update={"options": options})
    if structure_buffered:
        # Isolate hedge selection: the premium-rich short already has a full
        # structural buffer. Separate tests exercise a materially safer rival.
        levels = feature.support_resistance
        feature = feature.model_copy(update={"support_resistance": levels.model_copy(update={
            "potential_support_clusters": [c.model_copy(update={"low_strike": 24800, "high_strike": 24850})
                for c in levels.potential_support_clusters],
            "potential_resistance_clusters": [c.model_copy(update={"low_strike": 25150, "high_strike": 25200})
                for c in levels.potential_resistance_clusters]})})
    regime = regime.model_copy(update={"regime": Regime(direction), "market_bias": MarketBias(direction),
        "directional_strength": DirectionalStrength.STRONG, "directional_score": 80,
        "strategy_family_eligibility": StrategyFamilyEligibility.DIRECTIONAL_ONLY})
    return raw, feature, regime


def construct(engine, context, direction, *, cap=20000, capital=None, widths=(100, 400), lots=1):
    raw, feature, regime = context
    if engine == "phase14":
        config = StrategyConfig(credit_spread_policy=CreditSpreadPolicy(enabled=True,
            replay_integrity_enabled=True, expected_move_source="VIX_SCALED_EXPIRY_MOVE",
            research_quote_policy=QuotePolicy(), requested_lots=lots),
            allowed_spread_widths=widths, max_candidates=30,
            max_defined_loss_rupees=cap, max_defined_capital_rupees=capital,
            max_defined_width=max(widths), requested_lots=lots)
        result = generate_candidates(raw, feature, regime, 10, config)
        return result.candidates, result.reason_codes
    quotes = [ScalperOptionQuote(expiry=item.expiry, strike=item.strike,
        option_type=item.option_type.value, exchange=item.exchange,
        instrument_token=item.instrument_token, trading_symbol=item.trading_symbol,
        bid=item.bid, ask=item.ask, ltp=item.ltp, bid_quantity=item.bid_quantity,
        ask_quantity=item.ask_quantity, depth_unit=item.depth_unit,
        source_market_timestamp=item.source_market_timestamp,
        open_interest=item.open_interest, volume=item.volume, tick_size=item.tick_size)
        for item in raw.options]
    capture = ScalperMarketSnapshot(captured_at=raw.timestamp_ist,
        request_started_at=raw.timestamp_ist, response_received_at=raw.timestamp_ist,
        nifty_spot=raw.nifty_spot, nifty_future=raw.nifty_future, india_vix=raw.india_vix,
        atm_strike=raw.atm_strike, lot_size=raw.lot_size, expiry=raw.expiry, quotes=quotes)
    config = ScalperConfig.from_settings(Settings(_env_file=None, kotak_consumer_key="OFFLINE",
        telegram_enabled=False, scalper_max_loss_per_trade=cap, scalper_lots=lots))
    config = replace(config, allowed_widths=widths)
    result = build_candidates(capture, signal_for(capture, "BULL" if direction == "BULLISH" else "BEAR"), config)
    return result.candidates, result.rejection_counts


def test_wide_hedge_retains_premium_short_is_dominant(engine, direction, market_snapshot):
    candidates, _ = construct(engine, example(market_snapshot, direction), direction)
    assert candidates
    best = candidates[0]
    assert best.short_leg.strike == (25300 if direction == "BEARISH" else 24700)
    assert best.spread_width == 400
    assert best.premium_retention_ratio == .875
    assert best.construction_method == "SHORT_LEG_FIRST_V1"
    assert {"SHORT_LEG_SELECTED", "HEDGE_SELECTED_FOR_PREMIUM_RETENTION"} <= set(best.reason_codes)
    assert any(item.short_leg.strike == best.short_leg.strike and item.spread_width == 100 for item in candidates)
    # These fields are independent of the unrelated LTP=777 fixture value.
    if engine == "phase14":
        assert best.net_credit == 70 and best.max_loss_per_lot == 16500
    else:
        assert best.executable_credit == 70 and best.defined_max_loss_per_lot == 16500


def test_risk_cap_wins_and_nearer_hedge_is_retained(engine, direction, market_snapshot):
    candidates, codes = construct(engine, example(market_snapshot, direction), direction, cap=10000)
    assert candidates[0].spread_width == 100
    assert "HEDGE_REJECTED_MAX_LOSS" in codes


def test_risk_cap_boundary_and_no_valid_hedge(engine, direction, market_snapshot):
    context = example(market_snapshot, direction)
    candidates, _ = construct(engine, context, direction, cap=16500)
    assert candidates[0].spread_width == 400
    candidates, codes = construct(engine, context, direction, cap=1000)
    assert not candidates and "HEDGE_REJECTED_MAX_LOSS" in codes


@pytest.mark.parametrize("change", [{"ask": None}, {"bid": None}, {"ask_quantity": 0},
    {"open_interest": 0}, {"volume": 0}, {"depth_unit": "UNKNOWN"}, {"bid": 0},
    {"source_market_timestamp": None}, {"ask": 8}, {"ask": 10.01, "tick_size": .05}])
def test_illiquid_far_hedge_never_wins(engine, direction, market_snapshot, change):
    candidates, codes = construct(engine, example(market_snapshot, direction, far_change=change), direction)
    assert candidates and candidates[0].spread_width == 100
    assert "HEDGE_REJECTED_EXECUTION" in codes


def test_stale_far_hedge_is_not_selected(engine, direction, market_snapshot):
    context = example(market_snapshot, direction, far_change={
        "source_market_timestamp": market_snapshot.timestamp_ist - timedelta(seconds=31)})
    candidates, codes = construct(engine, context, direction)
    assert candidates and candidates[0].spread_width == 100
    assert "HEDGE_REJECTED_EXECUTION" in codes


def test_narrow_hedge_can_win_when_far_protection_cost_is_worse(engine, direction, market_snapshot):
    candidates, _ = construct(engine, example(market_snapshot, direction, far_ask=70), direction)
    assert candidates[0].short_leg.strike == (25300 if direction == "BEARISH" else 24700)
    assert candidates[0].spread_width == 100
    assert candidates[0].premium_retention_ratio == .25


def test_existing_width_allowlist_not_relaxed(engine, direction, market_snapshot):
    candidates, _ = construct(engine, example(market_snapshot, direction), direction, widths=(100,))
    assert candidates and all(item.spread_width == 100 for item in candidates)


def test_requested_lots_apply_to_defined_risk(engine, direction, market_snapshot):
    if engine == "phase15":
        # Phase 15's one-lot safety ceiling must not be relaxed by construction.
        with pytest.raises(ValueError, match="scalper_lots"):
            construct(engine, example(market_snapshot, direction), direction, lots=2)
        return
    candidates, codes = construct(engine, example(market_snapshot, direction), direction, lots=2)
    assert candidates[0].spread_width == 100
    assert "HEDGE_REJECTED_MAX_LOSS" in codes


def test_capital_limit_is_not_broker_margin(direction, market_snapshot):
    candidates, codes = construct("phase14", example(market_snapshot, direction), direction, capital=10000)
    assert candidates[0].spread_width == 100
    assert "HEDGE_REJECTED_MAX_CAPITAL" in codes


def test_deterministic_when_chain_order_changes(engine, direction, market_snapshot):
    raw, feature, regime = example(market_snapshot, direction)
    first, _ = construct(engine, (raw, feature, regime), direction)
    reverse, _ = construct(engine, (raw.model_copy(update={"options": list(reversed(raw.options))}), feature, regime), direction)
    assert [(c.candidate_id, c.premium_retention_ratio) for c in first] == [
        (c.candidate_id, c.premium_retention_ratio) for c in reverse]


def test_gross_retention_is_not_changed_by_cost_estimates(market_snapshot):
    raw, feature, regime = example(market_snapshot, "BEARISH")
    config = StrategyConfig(credit_spread_policy=CreditSpreadPolicy(enabled=True, slippage_points_per_leg=.1),
        allowed_spread_widths=(100, 400), max_defined_loss_rupees=20000)
    result = generate_candidates(raw, feature, regime, 10, config)
    assert result.candidates[0].premium_retention_ratio == .875


@pytest.mark.parametrize("values", [(None, 10), (0, 10), (80, float("nan")), (80, -1)])
def test_retention_never_fabricates_missing_premiums(values):
    with pytest.raises(ValueError):
        premium_retention(*values)


def test_before_after_vertical_ranking_fixture(engine, direction, market_snapshot):
    candidates, _ = construct(engine, example(market_snapshot, direction), direction)
    if engine == "phase14":
        before = min(candidates, key=lambda c: (-c.construction_evidence["prior_vertical_score"],
            c.strategy_family.value, c.short_leg.strike, c.spread_width))
    else:
        before = min(candidates, key=lambda c: (-c.construction_evidence["prior_vertical_score"],
            c.defined_max_loss_per_lot, c.short_distance_points, c.candidate_id))
    sign = 1 if direction == "BEARISH" else -1
    assert before.spread_width == 100
    assert before.short_leg.strike == 25000 + sign * (400 if engine == "phase14" else 300)
    after = candidates[0]
    assert after.short_leg.strike == 25000 + sign * 300
    assert after.long_leg.strike == 25000 + sign * 700
    assert after.premium_retention_ratio == .875


def test_candidate_evidence_roundtrip_and_legacy_defaults(engine, direction, market_snapshot):
    candidates, _ = construct(engine, example(market_snapshot, direction), direction)
    candidate = candidates[0]
    assert type(candidate).model_validate_json(candidate.model_dump_json()) == candidate
    legacy = candidate.model_dump()
    for key in ("premium_retention_ratio", "construction_method", "construction_evidence"):
        legacy.pop(key)
    restored = type(candidate).model_validate(legacy)
    assert restored.construction_method is None and restored.construction_evidence == {}


def test_phase14_construction_mirrors_applicable_risk_limits():
    from app.pipeline.service import shadow_risk_config_from_settings
    from app.strategy.service import config_from_settings
    settings = Settings(_env_file=None, kotak_consumer_key="OFFLINE", telegram_enabled=False)
    normal = config_from_settings(settings)
    assert normal.max_defined_loss_rupees == settings.risk_max_loss_per_trade
    assert normal.max_defined_capital_rupees == settings.risk_max_capital_per_trade
    assert normal.max_defined_width == settings.risk_max_spread_width
    risk = shadow_risk_config_from_settings(settings)
    shadow = config_from_settings(settings, risk=risk)
    assert shadow.max_defined_loss_rupees == risk.max_loss_per_trade
    assert shadow.max_defined_capital_rupees == risk.max_capital_per_trade
    assert shadow.max_defined_width == risk.max_spread_width
