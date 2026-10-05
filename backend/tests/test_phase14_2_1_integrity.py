"""Audit adversaries and isolated chronological evidence, using offline fixtures."""
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from app.alpha.experiments import ExperimentParameters
from app.alpha.replay import ReplayRow, replay_parameters
from app.core.config import Settings
from app.db.models import MarketSnapshotRecord
from app.db.repositories import SnapshotRepository
from app.features.repository import raw_record_to_model
from app.features.engine import build_market_features
from app.regime.engine import RegimeConfig, classify_regime
from app.regime.market_state import market_state
from app.regime.models import MarketBias, DirectionalStrength, Regime, StrategyFamilyEligibility
from app.research.analytics import path_coverage, replay_analytics
from app.research.config import ReplayIntegrityConfig, replay_configuration
from app.research.costs import CostSchedule
from app.research.ledger import ResearchLedger
from app.research.manifest import (authorize_split, canonical, create_manifest, digest,
                                  registered_rows, validate_experiment_axes)
from app.research.moves import fixed_expected_move
from app.research.oi import local_delta, oi_quality
from app.research.quotes import (ContractIdentity, QuotePolicy, exact_contract, executable_quote,
                                execution_pair, validate_book)
from app.research.replay import persistence_identity
from app.risk.engine import evaluate_risk
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.risk.exposure import ResearchRiskStateProvider
from app.risk.limits import RiskConfig
from app.risk.models import EvaluationContext
from app.shadow.exits import ShadowConfig
from app.strategy.candidate_engine import StrategyConfig, generate_candidates
from app.strategy.economics import expiry_context
from app.strategy.policy import CreditSpreadPolicy
from tests.test_phase14_2_strategy_logic import context as baseline_context


@pytest.fixture
def context(market_snapshot):
    raw, feature, regime, prior = baseline_context.__wrapped__(market_snapshot)
    raw = raw.model_copy(update={"options": [row.model_copy(update={"depth_unit": "UNITS"}) for row in raw.options]})
    return raw, feature, regime, prior


def policy_config(**updates):
    return CreditSpreadPolicy(enabled=True, **updates)


def candidates(context, width=100, side="BULLISH", policy=None):
    raw, feature, regime, _ = context
    regime = regime.model_copy(update={"market_bias": MarketBias(side), "regime": Regime(side),
        "directional_strength": DirectionalStrength.STRONG,
        "strategy_family_eligibility": StrategyFamilyEligibility.DIRECTIONAL_ONLY})
    config = StrategyConfig(credit_spread_policy=policy or policy_config(), allowed_spread_widths=(width,), max_candidates=20)
    return generate_candidates(raw, feature, regime, 10, config), regime


def risk_result(context, candidate_set, regime, **changes):
    raw, feature, _, _ = context
    config = RiskConfig(credit_spread_policy=policy_config(), max_spread_width=400,
        max_loss_per_trade=100000, max_capital_per_trade=100000,
        required_consecutive_directional_snapshots=1, **changes)
    return evaluate_risk(candidate_set, None, raw, feature, regime, config, EvaluationContext.HISTORICAL,
                         ResearchRiskStateProvider(), ConfiguredMarketEventProvider())


@pytest.mark.parametrize("side", ["BULLISH", "BEARISH"])
@pytest.mark.parametrize("width", [100, 200, 300, 400])
def test_risk_reprices_against_raw_not_self_consistent_candidate(context, side, width):
    result, regime = candidates(context, width, side)
    assert result.eligible
    candidate = result.candidates[0]
    assert risk_result(context, result, regime).approved_count > 0
    credit = candidate.net_credit + 5
    lot = context[0].lot_size
    mutated = candidate.model_copy(update={"net_credit": credit, "max_profit": credit,
        "max_loss": width-credit, "credit_to_width_ratio": credit/width,
        "max_profit_per_lot": credit*lot, "max_loss_per_lot": (width-credit)*lot,
        "net_credit_per_unit": credit, "gross_credit": credit, "max_loss_per_unit": width-credit,
        "credit_to_max_loss": credit/(width-credit),
        "breakeven": candidate.short_leg.strike + (credit if side == "BEARISH" else -credit)})
    bad = result.model_copy(update={"candidates": [mutated], "candidate_count": 1})
    evaluated = risk_result(context, bad, regime)
    assert evaluated.approved_count == 0
    assert "INDEPENDENT_REPRICE_MISMATCH" in evaluated.decisions[0].reason_codes
    assert evaluated.decisions[0].max_loss_per_unit == pytest.approx(candidate.max_loss)


def test_mutation_cannot_evade_monetary_loss_cap(context):
    result, regime = candidates(context, 400)
    candidate = result.candidates[0]
    credit = candidate.net_credit + 10
    altered = candidate.model_copy(update={"net_credit": credit, "max_profit": credit, "max_loss": 400-credit})
    bad = result.model_copy(update={"candidates": [altered], "candidate_count": 1})
    config = RiskConfig(credit_spread_policy=policy_config(), max_spread_width=400,
        max_loss_per_trade=(candidate.max_loss-1)*50, max_capital_per_trade=100000,
        required_consecutive_directional_snapshots=1)
    raw, feature, _, _ = context
    evaluated = evaluate_risk(bad, None, raw, feature, regime, config, EvaluationContext.HISTORICAL,
                             ResearchRiskStateProvider(), ConfiguredMarketEventProvider())
    assert {"INDEPENDENT_REPRICE_MISMATCH", "MAX_LOSS_EXCEEDED"} <= set(evaluated.decisions[0].reason_codes)


@pytest.mark.parametrize("field,value", [("net_credit", float("nan")), ("max_loss", float("inf")),
    ("max_loss_per_lot", float("nan")), ("spread_width", float("inf"))])
def test_nonfinite_candidate_economics_rejected(context, field, value):
    result, regime = candidates(context)
    bad = result.candidates[0].model_copy(update={field: value})
    result = result.model_copy(update={"candidates": [bad], "candidate_count": 1})
    evaluated = risk_result(context, result, regime)
    assert not evaluated.approved_count
    assert "NONFINITE_CANDIDATE_ECONOMICS" in evaluated.decisions[0].reason_codes


@pytest.mark.parametrize("target", ["candidate", "candidate_set", "regime", "regime_version"])
def test_policy_version_mismatch_rejected(context, target):
    result, regime = candidates(context)
    if target == "candidate":
        result = result.model_copy(update={"candidates": [result.candidates[0].model_copy(update={"strategy_logic_version": "OTHER"})], "candidate_count": 1})
    elif target == "candidate_set":
        result = result.model_copy(update={"strategy_version": "OTHER"})
    elif target == "regime":
        regime = regime.model_copy(update={"strategy_logic_version": "OTHER"})
    else:
        regime = regime.model_copy(update={"regime_version": "OTHER"})
    evaluated = risk_result(context, result, regime)
    assert not evaluated.approved_count
    assert "POLICY_VERSION_MISMATCH" in evaluated.decisions[0].reason_codes


def test_duplicate_raw_identity_fails_risk(context):
    result, regime = candidates(context)
    result = result.model_copy(update={"candidates": [result.candidates[0]], "candidate_count": 1})
    row, _ = exact_contract(context[0], ContractIdentity.of(result.candidates[0].short_leg))
    raw = context[0].model_copy(update={"options": [*context[0].options, row]})
    evaluated = risk_result((raw, *context[1:]), result, regime)
    assert not evaluated.approved_count
    assert "EXACT_CONTRACT_AMBIGUOUS" in evaluated.decisions[0].reason_codes


@pytest.mark.parametrize("changes,reason", [
    ({"bid": float("nan")}, "NONFINITE_EXECUTABLE_PRICE"),
    ({"ask": float("inf")}, "NONFINITE_EXECUTABLE_PRICE"),
    ({"bid": 0}, "NONPOSITIVE_EXECUTABLE_PRICE"),
    ({"ask": 0}, "NONPOSITIVE_EXECUTABLE_PRICE"),
    ({"bid": 201, "ask": 200}, "CROSSED_BOOK"),
    ({"bid": None}, "MISSING_EXECUTABLE_SIDE"),
    ({"ask": None}, "MISSING_EXECUTABLE_SIDE"),
    ({"source_market_timestamp": None}, "MISSING_QUOTE_TIMESTAMP"),
    ({"instrument_token": None}, "INVALID_EXACT_CONTRACT_IDENTITY"),
    ({"tick_size": .3}, "INVALID_TICK_PRICE"),
])
def test_book_validity_matrix(context, changes, reason):
    raw = context[0]
    row = next(row for row in raw.options if row.strike == 25000)
    quality = validate_book(row.model_copy(update=changes), raw.timestamp_ist)
    assert not quality.valid and quality.reason == reason


@pytest.mark.parametrize("offset,reason", [(-31, "STALE_LEG_QUOTE"), (1, "FUTURE_SOURCE_TIMESTAMP")])
def test_source_time_fails_closed(context, offset, reason):
    raw = context[0]
    row = raw.options[0].model_copy(update={"source_market_timestamp": raw.timestamp_ist+timedelta(seconds=offset)})
    assert validate_book(row, raw.timestamp_ist).reason == reason


@pytest.mark.parametrize("offset", [-31, 1])
def test_observation_age_separate_from_source(context, offset):
    raw = context[0]
    at = raw.timestamp_ist+timedelta(seconds=offset)
    assert not validate_book(raw.options[0], at, evaluated_at=raw.timestamp_ist).valid


@pytest.mark.parametrize("lots,depth,unit,expected", [
    (1, 1, "UNITS", "INSUFFICIENT_DEPTH"), (1, 50, "UNITS", "OK"),
    (2, 50, "UNITS", "INSUFFICIENT_DEPTH"), (2, 100, "UNITS", "OK"),
    (2, 2, "LOTS", "OK"), (1, 1000, "UNKNOWN", "UNKNOWN_REQUIRED_DEPTH"),
    (1, None, "UNITS", "UNKNOWN_REQUIRED_DEPTH"), (1, 0, "UNKNOWN", "INSUFFICIENT_DEPTH"),
])
def test_depth_units_and_multiple_lots(context, lots, depth, unit, expected):
    raw = context[0]
    row = next(row for row in raw.options if row.strike == 25000)
    row = row.model_copy(update={"bid_quantity": depth, "depth_unit": unit, "ask_quantity": None})
    raw = raw.model_copy(update={"options": [row]})
    quote = executable_quote(raw, row, "bid", lots)
    assert quote.reason == expected
    assert quote.requested_units == lots*50
    if expected == "OK":
        assert quote.depth_status == "EXECUTABLE_DEPTH"


def test_partial_depth_preserves_known_zero_failure(context):
    candidate_set, _ = candidates(context)
    candidate = candidate_set.candidates[0]
    raw = context[0]
    options = [row.model_copy(update={"bid_quantity": 0}) if row.instrument_token == candidate.short_leg.instrument_token
               else row.model_copy(update={"ask_quantity": None}) if row.instrument_token == candidate.long_leg.instrument_token else row
               for row in raw.options]
    pair, reason, detail = execution_pair(raw.model_copy(update={"options": options}), candidate, entry=True, lots=1)
    assert pair is None and reason == "INSUFFICIENT_DEPTH"
    assert {quote.depth_status for quote in detail} == {"INSUFFICIENT", "UNKNOWN"}


def test_unknown_depth_is_separate_opt_in_simulation(context):
    raw = context[0]
    row = raw.options[0].model_copy(update={"depth_unit": "UNKNOWN"})
    raw = raw.model_copy(update={"options": [row]})
    quote = executable_quote(raw, row, "bid", 1, QuotePolicy(allow_unknown_depth=True))
    assert quote.valid and quote.depth_status == "UNKNOWN_DEPTH_SIMULATION"
    assert quote.available_units is None


@pytest.mark.parametrize("entry,short_field,long_field", [(True, "bid_quantity", "ask_quantity"), (False, "ask_quantity", "bid_quantity")])
def test_required_fill_sides_independent_of_unrelated_depth(context, entry, short_field, long_field):
    candidate_set, _ = candidates(context)
    candidate = candidate_set.candidates[0]
    options = []
    for row in context[0].options:
        update = {"bid_quantity": None, "ask_quantity": None}
        if row.instrument_token == candidate.short_leg.instrument_token:
            update[short_field] = 50
        if row.instrument_token == candidate.long_leg.instrument_token:
            update[long_field] = 50
        options.append(row.model_copy(update=update))
    pair, reason, _ = execution_pair(context[0].model_copy(update={"options": options}), candidate, entry=entry, lots=1)
    assert pair is not None and reason == "OK"


@pytest.mark.parametrize("change", ["missing", "zero"])
def test_static_oi_does_not_depend_on_broker_delta(context, change):
    raw, feature, regime, prior = context
    raw = raw.model_copy(update={"options": [row.model_copy(update={"change_in_open_interest": None if change == "missing" else 0,
                                                                   "previous_open_interest": None}) for row in raw.options]})
    assert len(raw.options) == 122
    quality = oi_quality(raw)
    assert quality["static_oi_usable"] and not quality["broker_oi_change_nonzero"]
    feature = feature.model_copy(update={"data_quality": feature.data_quality.model_copy(update={**quality, "intraday_oi_usable": False})})
    assert market_state(regime, feature, prior, policy_config())["market_bias"] != "INSUFFICIENT"
    assert generate_candidates(raw, feature, regime, 10, StrategyConfig(credit_spread_policy=policy_config())).eligible


def test_missing_current_oi_is_unusable(context):
    raw = context[0].model_copy(update={"options": [row.model_copy(update={"open_interest": None}) for row in context[0].options]})
    assert not oi_quality(raw)["static_oi_usable"]


@pytest.mark.parametrize("field,value", [("instrument_token", "changed"), ("exchange", "other"),
    ("strike", 23499), ("expiry", date(2099, 10, 5)), ("option_type", "PE")])
def test_local_delta_requires_exact_identity(context, field, value):
    raw = context[0]
    previous = raw.model_copy(update={"timestamp_ist": raw.timestamp_ist-timedelta(minutes=3),
        "options": [row.model_copy(update={field: value}) for row in raw.options]})
    assert local_delta(raw, previous, raw.options[0])[0] is None


@pytest.mark.parametrize("elapsed,reason", [(86400, "SESSION_CHANGED"), (1800, "INVALID_OI_INTERVAL"), (0, "INVALID_OI_INTERVAL")])
def test_local_delta_session_and_interval(context, elapsed, reason):
    raw = context[0]
    previous = raw.model_copy(update={"timestamp_ist": raw.timestamp_ist-timedelta(seconds=elapsed)})
    assert local_delta(raw, previous, raw.options[0])[1] == reason


def test_local_delta_zero_is_valid(context):
    raw = context[0]
    previous = raw.model_copy(update={"timestamp_ist": raw.timestamp_ist-timedelta(minutes=3),
        "options": [row.model_copy(update={"source_market_timestamp": raw.timestamp_ist-timedelta(minutes=3)}) for row in raw.options]})
    assert local_delta(raw, previous, raw.options[0]) == (0, "VALID_ZERO")


def complete_schedule(**updates):
    return CostSchedule(**({"cost_schedule_version": "TEST_FIXTURE_NOT_STATUTORY", "effective_from": date(2000, 1, 1),
        "brokerage_per_order": 2, "exchange_rate": .001, "stt_rate": .002, "gst_rate": .1,
        "stamp_rate": .003, "sebi_rate": .0001, "slippage_points_per_leg": .05, "declared_complete": True} | updates))


def accounting(schedule, lots=1, day=date(2026, 10, 5)):
    return schedule.calculate(day=day, lot_size=50, lots=lots, entry_short=25, entry_long=10, exit_short=12, exit_long=5)


def test_fixed_brokerage_and_actual_turnover_scale_correctly():
    one, two = accounting(complete_schedule()), accounting(complete_schedule(), 2)
    assert one["brokerage"] == two["brokerage"] == 8
    assert one["order_count"] == two["order_count"] == 4
    assert two["sold_turnover_rupees"] == (25+5)*100
    assert two["bought_turnover_rupees"] == (10+12)*100
    assert two["gross_rupees"] == 2*one["gross_rupees"]
    assert two["net_rupees"] > 2*one["net_rupees"]
    assert two["turnover_basis"] == "ACTUAL"


@pytest.mark.parametrize("component", CostSchedule.rate_fields())
def test_missing_cost_component_is_gross_only(component):
    result = accounting(complete_schedule(**{component: None}))
    assert result["cost_completeness"] == "GROSS_ONLY"
    assert result["net_rupees"] is None and result["total_cost_rupees"] is None


@pytest.mark.parametrize("day,complete", [(date(2026, 10, 4), False), (date(2026, 10, 5), True),
    (date(2026, 10, 6), True), (date(2026, 10, 7), False)])
def test_dated_schedule_boundaries(day, complete):
    schedule = complete_schedule(effective_from=date(2026, 10, 5), effective_to=date(2026, 10, 6))
    assert schedule.complete_at(day) == complete


def test_zero_rates_clear_complete_state():
    schedule = complete_schedule(**{key: 0 for key in CostSchedule.rate_fields()})
    assert accounting(schedule)["cost_completeness"] == "GROSS_ONLY"


@pytest.mark.parametrize("mutation", ["wide", "stale", "wrong_atm", "duplicate", "skew"])
def test_straddle_move_quality(context, mutation):
    raw, feature, _, _ = context
    updates = {"bid": 1, "ask": 1000} if mutation == "wide" else (
        {"source_market_timestamp": raw.timestamp_ist-timedelta(seconds=599)} if mutation == "stale" else {})
    options = [row.model_copy(update=updates) if row.strike == raw.atm_strike else row for row in raw.options]
    if mutation == "skew":
        options = [row.model_copy(update={"source_market_timestamp": raw.timestamp_ist-timedelta(seconds=11)})
                   if row.strike == raw.atm_strike and row.option_type.value == "CE" else row for row in options]
    if mutation == "duplicate":
        options.append(next(row for row in options if row.strike == raw.atm_strike))
    raw = raw.model_copy(update={"options": options, "atm_strike": 25100 if mutation == "wrong_atm" else raw.atm_strike})
    assert fixed_expected_move(raw, feature, 3, "ATM_STRADDLE_PREMIUM", policy_config()) is None


def test_fixed_move_source_does_not_fallback(context):
    raw, feature, _, _ = context
    assert fixed_expected_move(raw, feature, 3, "VIX_SCALED_EXPIRY_MOVE", policy_config())["expected_move_source"] == "VIX_SCALED_EXPIRY_MOVE"
    assert fixed_expected_move(raw.model_copy(update={"india_vix": None}), feature, 3, "VIX_SCALED_EXPIRY_MOVE", policy_config()) is None
    with pytest.raises(ValueError, match="fixed"):
        fixed_expected_move(raw, feature, 3, "AUTO", policy_config())


def replay_fixture(context, count=5, close=True):
    raw, feature, _, prior = context
    feature = feature.model_copy(update={"price_structure_features": feature.price_structure_features.model_copy(update={
        "spot_change_from_previous_snapshot": 150, "spot_change_pct_from_previous_snapshot": .6,
        "future_change_from_previous_snapshot": 150, "future_change_pct_from_previous_snapshot": .6,
        "session_open_proxy": 24900})})
    policy = policy_config(family_mode="DIRECTIONAL_ONLY")
    strategy = StrategyConfig(credit_spread_policy=policy, allowed_spread_widths=(100,), max_candidates=1)
    risk = RiskConfig(credit_spread_policy=policy, max_spread_width=400, max_loss_per_trade=100000,
                      max_capital_per_trade=100000, required_consecutive_directional_snapshots=1,
                      max_trades_per_day=3, max_daily_loss=100000)
    regime = RegimeConfig(credit_spread_policy=policy)
    shadow = ShadowConfig(max_new_trades_per_day=3)
    parameters = ExperimentParameters(.8, 2, time(9, 35), time(13, 30), spread_widths=(100,))
    classified = classify_regime(1, feature, prior, regime)
    selected = generate_candidates(raw, feature, classified, 1, strategy).candidates[0]
    rows = []
    for index in range(count):
        at = raw.timestamp_ist+timedelta(minutes=3*index)
        options = []
        for contract in raw.options:
            center = 24500 if contract.option_type.value == "PE" else 25500
            oi = 100000 if contract.strike == center else 100
            update = {"source_market_timestamp": at, "open_interest": oi,
                      "previous_open_interest": oi, "change_in_open_interest": 0}
            options.append(contract.model_copy(update=update))
        spot = 24850 if index == 0 else raw.nifty_spot + 25*(index-1)
        point = raw.model_copy(update={"timestamp_ist": at, "options": options, "nifty_spot": spot,
                                      "nifty_future": raw.nifty_future-150 if index == 0 else raw.nifty_future+25*(index-1)})
        current_feature = feature.model_copy(update={"timestamp": at, "snapshot_id": index+1, "spot": spot})
        rows.append(ReplayRow(index+1, point, current_feature, index+1, None))
    integrity = ReplayIntegrityConfig(enabled=True, session_start=raw.timestamp_ist.time().replace(tzinfo=None),
                                     session_end=(raw.timestamp_ist+timedelta(minutes=3*(count-1))).time().replace(tzinfo=None))
    first = build_market_features(rows[0].snapshot_id, rows[0].snapshot, [])
    second = build_market_features(rows[1].snapshot_id, rows[1].snapshot, [rows[0].snapshot])
    classified = classify_regime(rows[1].feature_id, second, first, regime)
    selected = generate_candidates(rows[1].snapshot, second, classified, rows[1].snapshot_id, strategy,
                                   enforce_freshness=False).candidates[0]
    if close:
        for index in range(3, len(rows)):
            point = rows[index].snapshot
            ask = selected.long_leg.bid + selected.net_credit*.2
            options = [contract.model_copy(update={"bid": ask-.2, "ask": ask, "ltp": ask-.1})
                       if contract.instrument_token == selected.short_leg.instrument_token else contract
                       for contract in point.options]
            rows[index] = replace(rows[index], snapshot=point.model_copy(update={"options": options}))
    return rows, (parameters, strategy, risk, regime, shadow, integrity), selected


def registered_run(tmp_path, rows, configs, schedule=None, source="VIX_SCALED_EXPIRY_MOVE", sessions=None, **extra):
    schedule = schedule or complete_schedule()
    days = sorted({row.snapshot.timestamp_ist.date().isoformat() for row in rows})
    manifest = create_manifest(rows, code_sha="a"*40, config=replay_configuration(*configs), cost_schedule=schedule,
        expected_move_source=source, sessions=sessions or {"TRAIN": days, "VALIDATION": [], "FINAL_TEST": []},
        event_config=[], **extra)
    ledger = ResearchLedger(tmp_path, manifest, create=True)
    return manifest, ledger, schedule


def execute(tmp_path, rows, configs, schedule=None):
    manifest, ledger, schedule = registered_run(tmp_path, rows, configs, schedule)
    parameters, strategy, risk, regime, shadow, integrity = configs
    result = replay_parameters(rows, parameters, strategy, risk, regime, shadow, integrity_config=integrity,
                               manifest=manifest, ledger=ledger, cost_schedule=schedule)
    return result, ledger


def test_authoritative_real_path_uses_next_observation_and_observed_exit(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    result, ledger = execute(tmp_path, rows, configs)
    closed = [item for item in result["attempts"] if item["state"] == "CLOSED"]
    assert closed
    trade = closed[0]
    assert trade["entry_at"] > trade["decision_timestamp"]
    assert trade["decision_to_fill_delay_seconds"] == 180
    assert trade["exit_reason"] == "PROFIT_TARGET_EXIT"
    assert trade["sampled_MAE_points_per_unit"] == 0
    assert trade["sampled_MFE_points_per_unit"] > 0
    assert trade["exit_execution"]["fill_method"] == "CONTEMPORANEOUS_OBSERVED_BOOK_LIQUIDATION"
    assert trade["decision_time_candidate"]["net_credit"] != trade["exit_execution"]["short"]["fill_price"]
    assert result["path_coverage_ratio"] == 1
    assert result["net_rupees_for_quantity"] is not None
    assert "VIX_SCALED_EXPIRY_MOVE" in result["breakdowns"]["move_source"]
    assert ledger.events() and (ledger.directory/"summary.json").exists()


@pytest.mark.parametrize("failure", ["single_gap", "long_gap", "missing_exit", "missing_feature", "session_end", "cross_session"])
def test_incomplete_path_retains_exposure_and_blocks_state(context, tmp_path, failure):
    rows, configs, selected = replay_fixture(context, close=False)
    if failure == "single_gap":
        rows.pop(3)
    elif failure == "long_gap":
        at = rows[3].snapshot.timestamp_ist+timedelta(minutes=30)
        rows = rows[:3] + [replace(rows[3], snapshot=rows[3].snapshot.model_copy(update={"timestamp_ist": at}))]
        configs = (*configs[:-1], replace(configs[-1], session_end=at.time().replace(tzinfo=None)))
    elif failure == "missing_exit":
        raw = rows[3].snapshot.model_copy(update={"options": [row for row in rows[3].snapshot.options
            if row.instrument_token != selected.short_leg.instrument_token]})
        rows[3] = replace(rows[3], snapshot=raw)
    elif failure == "missing_feature":
        rows[3] = replace(rows[3], feature=None, feature_id=None)
    elif failure == "session_end":
        rows = rows[:3]
    else:
        rows = rows[:3]+[replace(rows[3], snapshot=rows[3].snapshot.model_copy(update={
            "timestamp_ist": rows[3].snapshot.timestamp_ist+timedelta(days=1)}))]
    result, ledger = execute(tmp_path, rows, configs)
    unresolved = [item for item in result["attempts"] if item["state"] == "UNRESOLVED_EXPOSURE"]
    assert unresolved and unresolved[0]["entry_execution"] is not None
    assert unresolved[0]["final_outcome"] is None and unresolved[0]["exit_execution"] is None
    assert result["denominators"]["unresolved"] >= 1
    assert result["status"] == "PARTIAL_PATH"
    assert result["diagnostic_all_depth_sampled_marked_equity_gross_drawdown_rupees"] is None
    if failure not in {"session_end", "cross_session"}:
        assert result["denominators"]["blocked_incomplete_state"] >= 1
    if failure in {"single_gap", "long_gap", "session_end"}:
        assert result["path_coverage_ratio"] < 1
    if failure == "missing_feature":
        assert result["missing_feature_observations"] == 1
        assert result["path_coverage_ratio"] < 1
    assert any(event["event_type"] == "FINAL_OUTCOME" and event["outcome"]["state"] == "UNRESOLVED_EXPOSURE" for event in ledger.events())


def test_fill_ttl_rejects_delayed_next_observation(context, tmp_path):
    rows, configs, _ = replay_fixture(context, close=False)
    at = rows[2].snapshot.timestamp_ist+timedelta(minutes=30)
    rows = rows[:2]+[replace(rows[2], snapshot=rows[2].snapshot.model_copy(update={"timestamp_ist": at}))]
    result, _ = execute(tmp_path, rows, configs)
    assert any(item["missing_reason"] == "DECISION_TO_FILL_TTL_EXCEEDED" for item in result["attempts"])
    assert result["denominators"]["filled_trades"] == 0


def test_gross_only_does_not_enter_net_aggregate(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    result, _ = execute(tmp_path, rows, configs, CostSchedule())
    assert result["denominators"]["gross_only"] >= 1
    assert result["gross_rupees_for_quantity"] is not None and result["net_rupees_for_quantity"] is None
    assert result["denominators"]["blocked_incomplete_state"] >= 1


def test_dataset_growth_cannot_change_frozen_splits(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    manifest, _, _ = registered_run(tmp_path, rows, configs)
    later = replace(rows[-1], snapshot_id=99, snapshot=rows[-1].snapshot.model_copy(update={"timestamp_ist": rows[-1].snapshot.timestamp_ist+timedelta(days=1)}))
    assert registered_rows(manifest, [*rows, later]) == rows
    assert manifest.payload()["sessions"]["TRAIN"] == [rows[0].snapshot.timestamp_ist.date().isoformat()]


@pytest.mark.parametrize("changed", ["code", "events", "costs", "raw", "feature"])
def test_manifest_hash_captures_reproducibility_inputs(context, tmp_path, changed):
    rows, configs, _ = replay_fixture(context)
    base = dict(code_sha="a"*40, config=replay_configuration(*configs), cost_schedule=complete_schedule(),
        expected_move_source="VIX_SCALED_EXPIRY_MOVE", sessions={"TRAIN": [rows[0].snapshot.timestamp_ist.date().isoformat()], "VALIDATION": [], "FINAL_TEST": []},
        event_config=[], run_id=str(uuid4()), created_at=datetime(2026, 10, 5, tzinfo=timezone.utc))
    first = create_manifest(rows, **base)
    if changed == "code": base["code_sha"] = "b"*40
    if changed == "events": base["event_config"] = [{"name": "fixture"}]
    if changed == "costs": base["cost_schedule"] = complete_schedule(brokerage_per_order=3)
    if changed == "raw": rows = [replace(rows[0], snapshot=rows[0].snapshot.model_copy(update={"india_vix": 11})), *rows[1:]]
    if changed == "feature": rows = [replace(rows[0], feature=rows[0].feature.model_copy(update={"spot": 25000})), *rows[1:]]
    assert create_manifest(rows, **base).manifest_hash != first.manifest_hash


@pytest.mark.parametrize("axis", ["confirmations", "volatility_distance_multiplier", "alpha_threshold", "imaginary"])
def test_inactive_axes_rejected(axis):
    with pytest.raises(ValueError, match="INACTIVE"):
        validate_experiment_axes({axis: [1, 2]})


def test_final_test_grid_stays_sealed_and_overlap_visible(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    membership = {"TRAIN": [], "VALIDATION": [], "FINAL_TEST": [rows[0].snapshot.timestamp_ist.date().isoformat()]}
    manifest, _, _ = registered_run(tmp_path/"first", rows, configs, sessions=membership)
    with pytest.raises(ValueError, match="FINAL_TEST_SEALED"):
        authorize_split(manifest, "FINAL_TEST", grid_size=20, policy_hash=manifest.payload()["policy_hash"])
    first = ResearchLedger(tmp_path/"registry", manifest, create=True, split="FINAL_TEST")
    assert first.record_final_exposure()["warning"] is None
    second_manifest = create_manifest(rows, code_sha="a"*40, config=replay_configuration(*configs), cost_schedule=complete_schedule(),
        expected_move_source="VIX_SCALED_EXPIRY_MOVE", sessions=membership, event_config=[])
    second = ResearchLedger(tmp_path/"registry", second_manifest, create=True, split="FINAL_TEST")
    assert second.record_final_exposure()["warning"] == "FINAL_TEST_REUSED"


@pytest.mark.parametrize("field", ["policy_hash", "reference", "calculation_mode", "expiry", "session", "strategy_logic_version", "expected_move_source_policy"])
def test_persistence_identity_changes_reset(field, context):
    raw, feature, _, _ = context
    policy = policy_config(replay_integrity_enabled=True, policy_hash="policy1", research_run_id="run1",
                           execution_mode="ISOLATED_OFFLINE_REPLAY", expected_move_source="VIX_SCALED_EXPIRY_MOVE")
    result, regime = candidates(context, policy=policy)
    identity = persistence_identity(raw, "policy1", "HISTORICAL_REPLAY", "VIX_SCALED_EXPIRY_MOVE")
    regime = regime.model_copy(update={"policy_hash": "policy1", "research_run_id": "run1",
        "execution_mode": "ISOLATED_OFFLINE_REPLAY", "persistence_identity": identity})
    previous = regime.model_copy(update={"timestamp": regime.timestamp-timedelta(minutes=3)})
    config = RiskConfig(credit_spread_policy=policy, required_consecutive_directional_snapshots=2,
                        max_loss_per_trade=100000, max_capital_per_trade=100000)
    def evaluate(prior):
        return evaluate_risk(result, None, raw, feature, regime, config, EvaluationContext.HISTORICAL,
            ResearchRiskStateProvider(), ConfiguredMarketEventProvider(), prior_regimes=[prior], evaluated_at=raw.timestamp_ist)
    assert "INSUFFICIENT_REGIME_CONFIRMATION" not in evaluate(previous).decisions[0].reason_codes
    previous = previous.model_copy(update={"persistence_identity": identity | {field: "different"}})
    rejected = evaluate(previous)
    assert not rejected.approved_count
    assert "INSUFFICIENT_REGIME_CONFIRMATION" in rejected.decisions[0].reason_codes


def test_ledger_run_isolation_and_immutability(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    manifest, first, _ = registered_run(tmp_path, rows, configs)
    first.append({"event_type": "ENTRY_EXECUTION", "attempt_id": "1"})
    _, second, _ = registered_run(tmp_path, rows, configs)
    assert second.events() == [] and len(first.events()) == 1
    with pytest.raises(ValueError, match="IDENTITY"):
        first.events(identity=(second.manifest.run_id, "phase14_2_v1", manifest.payload()["policy_hash"], "ISOLATED_OFFLINE_REPLAY"))
    with pytest.raises(FileExistsError):
        ResearchLedger(tmp_path, manifest, create=True)
    first.finish({"status": "EVALUABLE"})
    with pytest.raises(FileExistsError):
        first.finish({"status": "OTHER"})


def test_zero_dte_replay_restriction(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    modified = []
    for row in rows:
        expiry = row.snapshot.timestamp_ist.date()
        raw = row.snapshot.model_copy(update={"expiry": expiry, "options": [contract.model_copy(update={"expiry": expiry}) for contract in row.snapshot.options]})
        modified.append(replace(row, snapshot=raw, feature=row.feature.model_copy(update={"expiry": expiry})))
    result, _ = execute(tmp_path, modified, configs)
    assert result["denominators"]["filled_trades"] == 0
    assert all(item["missing_reason"] == "ZERO_DTE_EXCLUDED" for item in result["attempts"])


def test_width_axis_changes_real_construction(context):
    first, _ = candidates(context, 100)
    second, _ = candidates(context, 400)
    assert {row.spread_width for row in first.candidates} == {100}
    assert {row.spread_width for row in second.candidates} == {400}


def test_family_axis_changes_eligibility(context):
    raw, feature, regime, prior = context
    directional = market_state(regime, feature, prior, policy_config(theta_carry_enabled=True, family_mode="DIRECTIONAL_ONLY"))
    carry = market_state(regime, feature, prior, policy_config(theta_carry_enabled=True, family_mode="THETA_CARRY_ONLY"))
    assert directional["strategy_family_eligibility"] == "DIRECTIONAL_ONLY"
    assert carry["strategy_family_eligibility"] == "THETA_CARRY_ONLY"


def test_missing_expected_slots_are_not_survivor_only_coverage(context):
    rows, configs, _ = replay_fixture(context, count=12)
    report = path_coverage([rows[0], rows[-1]], {rows[0].snapshot.timestamp_ist.date().isoformat()}, configs[-1])
    assert report["expected_observations"] == 12
    assert report["observed_observations"] == 2 and report["missing_observations"] == 10
    assert report["path_coverage_ratio"] == pytest.approx(2/12)


def test_additive_0014_upgrade_preserves_0013_rows(tmp_path):
    project = Path(__file__).resolve().parents[2]
    url = f"sqlite:///{tmp_path/'migration.db'}"
    config = Config(str(project/"alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0013_phase14_2_strategy_logic")
    engine = create_engine(url)
    with engine.begin() as connection:
        raw = Table("market_snapshots", MetaData(), autoload_with=connection)
        connection.execute(raw.insert().values(id=1, timestamp_ist=datetime(2026, 10, 5, 10), collection_bucket_ist=datetime(2026, 10, 5, 10),
            nifty_spot=25000, atm_strike=25000, expiry=date(2026, 10, 8), source="FIXTURE"))
        options = Table("option_contract_snapshots", MetaData(), autoload_with=connection)
        connection.execute(options.insert().values(market_snapshot_id=1, strike=25000, option_type="CE", expiry=date(2026, 10, 8),
            trading_symbol="FIXTURE", exchange="nse_fo", ltp=100))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        options = Table("option_contract_snapshots", MetaData(), autoload_with=connection)
        row = connection.execute(select(options)).mappings().one()
        assert row["trading_symbol"] == "FIXTURE" and row["depth_unit"] == "UNKNOWN" and row["tick_size"] is None
    command.downgrade(config, "0013_phase14_2_strategy_logic")
    assert "depth_unit" not in {row["name"] for row in inspect(engine).get_columns("option_contract_snapshots")}
    engine.dispose()


def test_confirmed_depth_round_trip(repository, session_factory, context):
    raw = context[0].model_copy(update={"options": [row.model_copy(update={"tick_size": .05}) for row in context[0].options]})
    stored = repository.save_market_snapshot(raw, raw.timestamp_ist)
    with session_factory() as session:
        point = session.get(MarketSnapshotRecord, stored.snapshot_id)
        loaded = raw_record_to_model(point)
        assert loaded.options[0].depth_unit == "UNITS" and loaded.options[0].tick_size == .05


def test_production_and_research_flags_default_off():
    settings = Settings(kotak_consumer_key="OFFLINE_TEST", _env_file=None)
    for flag in ("alpha_engine_enabled", "regime_use_statistical_alpha", "strategy_volatility_buffer_enabled",
                 "pipeline_run_ai_research", "phase14_2_strategy_logic_enabled", "theta_carry_enabled",
                 "phase14_2_1_replay_integrity_enabled", "replay_allow_unknown_depth", "replay_allow_0dte"):
        assert getattr(settings, flag) is False


@pytest.mark.parametrize("reason", ["STOP_LOSS_EXIT", "OPPOSITE_REGIME_EXIT", "STRUCTURAL_INVALIDATION_EXIT", "TIME_EXIT"])
def test_authoritative_exit_reasons(context, tmp_path, reason):
    rows, configs, selected = replay_fixture(context, close=False)
    raw = rows[3].snapshot
    if reason == "STOP_LOSS_EXIT":
        ask = selected.long_leg.bid + selected.net_credit*3
        options = [item.model_copy(update={"bid": ask-.2, "ask": ask, "ltp": ask-.1})
                   if item.instrument_token == selected.short_leg.instrument_token else item for item in raw.options]
        rows[3] = replace(rows[3], snapshot=raw.model_copy(update={"options": options}))
    elif reason in {"OPPOSITE_REGIME_EXIT", "STRUCTURAL_INVALIDATION_EXIT"}:
        spot = 24900 if reason == "OPPOSITE_REGIME_EXIT" else selected.support_or_resistance_reference-1
        rows[3] = replace(rows[3], snapshot=raw.model_copy(update={"nifty_spot": spot, "nifty_future": spot+10}),
                          feature=rows[3].feature.model_copy(update={"spot": spot}))
        if reason == "STRUCTURAL_INVALIDATION_EXIT":
            configs = (*configs[:4], replace(configs[4], exit_on_opposite_regime=False), configs[5])
    else:
        configs = (*configs[:4], replace(configs[4], force_exit_time=raw.timestamp_ist.time().replace(tzinfo=None)), configs[5])
    result, _ = execute(tmp_path, rows, configs)
    assert result["exit_reason_counts"].get(reason, 0) >= 1
    trade = next(item for item in result["attempts"] if item.get("exit_reason") == reason)
    assert min(trade["marks"]) <= 0 <= max(trade["marks"])


def test_unknown_depth_simulation_never_enters_primary_evidence(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    rows = [replace(row, snapshot=row.snapshot.model_copy(update={"options": [item.model_copy(update={"depth_unit": "UNKNOWN"})
             for item in row.snapshot.options]})) for row in rows]
    integrity = replace(configs[-1], quote_policy=replace(configs[-1].quote_policy, allow_unknown_depth=True))
    result, _ = execute(tmp_path, rows, (*configs[:-1], integrity))
    assert result["denominators"]["closed_unknown_depth_simulations"] >= 1
    assert result["net_rupees_for_quantity"] is None and result["gross_rupees_for_quantity"] is None
    assert result["denominators"]["net_complete"] == 0
    assert any("UNKNOWN_DEPTH_SIMULATION" in key for key in result["cohorts"])


def test_shared_sql_rejects_research_before_any_queries():
    from app.pipeline.repository import PipelineRepository
    from app.regime.repository import RegimeRepository
    from app.risk.repository import RiskRepository
    from app.shadow.repository import ShadowRepository
    from app.strategy.repository import StrategyRepository
    tagged = SimpleNamespace(research_run_id="research-run", execution_mode="ISOLATED_OFFLINE_REPLAY")
    # None session factory proves no shared-state read/write can happen first.
    for repository, method, args in ((StrategyRepository(None), "upsert", (tagged,)),
        (RegimeRepository(None), "upsert", (tagged,)), (RiskRepository(None), "upsert", (tagged, 1)),
        (ShadowRepository(None), "create_trade", (tagged,)), (ShadowRepository(None), "save_update", (tagged, None)),
        (PipelineRepository(None), "save", (tagged,))):
        with pytest.raises(ValueError, match="RESEARCH_NAMESPACE"):
            getattr(repository, method)(*args)


def test_scoped_ledger_counts_open_fingerprints_latest_and_updates(context, tmp_path):
    rows, configs, _ = replay_fixture(context, close=False)
    result, first = execute(tmp_path, rows, configs)
    _, second, _ = registered_run(tmp_path, rows, configs)
    day = rows[0].snapshot.timestamp_ist.date()
    assert first.count_entries_on(day) == 1 and second.count_entries_on(day) == 0
    trade = first.open_trades()[0]
    assert trade["state"] == "UNRESOLVED_EXPOSURE"
    assert first.has_fingerprint(trade["fingerprint"]) and not second.has_fingerprint(trade["fingerprint"])
    assert first.latest() is not None and second.latest() is None
    assert all(item["policy_hash"] == first.manifest.payload()["policy_hash"] for item in first.attempts())
    assert first.latest()["run_id"] != second.manifest.run_id


def test_ledger_detects_external_event_tampering(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    _, ledger, _ = registered_run(tmp_path, rows, configs)
    ledger.append({"event_type": "OBSERVATION", "snapshot_id": 1})
    path = ledger.directory/"events"/"00000000.json"
    event = json.loads(path.read_text())
    event["snapshot_id"] = 999
    path.write_text(json.dumps(event), encoding="utf-8")
    with pytest.raises(ValueError, match="LEDGER_INTEGRITY_FAILURE"):
        ledger.events()


def test_cli_configuration_round_trip(context):
    import importlib.util
    path = Path(__file__).resolve().parents[2]/"scripts"/"run_integrity_replay.py"
    spec = importlib.util.spec_from_file_location("integrity_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, configs, _ = replay_fixture(context)
    assert module.configurations(json.loads(canonical(replay_configuration(*configs)))) == configs


def test_missing_and_misaligned_features_reduce_coverage(context, tmp_path):
    from app.research.features import reconstruct_feature
    rows, configs, _ = replay_fixture(context)
    original = reconstruct_feature(rows[1], [rows[0].snapshot], configs[-1].feature_engine_config)
    poisoned = replace(rows[1], feature=rows[1].feature.model_copy(update={"price_structure_features":
        rows[1].feature.price_structure_features.model_copy(update={"spot_change_from_previous_snapshot": -10000})}))
    assert reconstruct_feature(poisoned, [rows[0].snapshot], configs[-1].feature_engine_config) == original
    missing = replace(rows[2], feature=rows[2].feature.model_copy(update={"timestamp": rows[2].feature.timestamp+timedelta(minutes=1)}))
    assert reconstruct_feature(missing, [row.snapshot for row in rows[:2]], configs[-1].feature_engine_config) is None
    report = path_coverage([*rows[:2], missing, *rows[3:]], {rows[0].snapshot.timestamp_ist.date().isoformat()}, configs[-1])
    assert report["missing_feature_observations"] == 1 and report["path_coverage_ratio"] < 1


def test_duplicate_runtime_snapshot_ids_rejected(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    manifest, _, _ = registered_run(tmp_path, rows, configs)
    with pytest.raises(ValueError, match="DUPLICATE"):
        registered_rows(manifest, [*rows, rows[0]])


def test_runtime_quantity_cannot_depart_from_registration(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    manifest, ledger, schedule = registered_run(tmp_path, rows, configs)
    with pytest.raises(ValueError, match="RUNTIME_MANIFEST_MISMATCH"):
        replay_parameters(rows, *configs[:-1], integrity_config=configs[-1], manifest=manifest,
                          ledger=ledger, cost_schedule=schedule, quantity=2)


def test_final_selected_policy_authorization_is_single_policy(context):
    rows, configs, _ = replay_fixture(context)
    kwargs = dict(code_sha="a"*40, config=replay_configuration(*configs), cost_schedule=complete_schedule(),
        expected_move_source="VIX_SCALED_EXPIRY_MOVE", sessions={"TRAIN": [], "VALIDATION": [],
        "FINAL_TEST": [rows[0].snapshot.timestamp_ist.date().isoformat()]}, event_config=[])
    first = create_manifest(rows, **kwargs)
    manifest = create_manifest(rows, **kwargs, selected_policy_hash=first.payload()["policy_hash"])
    assert authorize_split(manifest, "FINAL_TEST", policy_hash=manifest.payload()["policy_hash"])
    with pytest.raises(ValueError, match="FINAL_TEST_SEALED"):
        authorize_split(manifest, "FINAL_TEST", grid_size=2, policy_hash=manifest.payload()["policy_hash"])


def test_singleton_axis_must_bind_to_effective_configuration(context):
    from app.research.manifest import validate_axis_bindings
    _, configs, _ = replay_fixture(context)
    parameters, strategy, risk, _, shadow, _ = configs
    validate_axis_bindings({"spread_widths": [(100,)], "regime_persistence": [1]}, parameters, strategy, risk, shadow)
    with pytest.raises(ValueError, match="CONFIGURATION_MISMATCH"):
        validate_axis_bindings({"spread_widths": [(400,)]}, parameters, strategy, risk, shadow)


def test_sealed_ledger_rejects_append_and_detects_tail_truncation(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    _, ledger, _ = registered_run(tmp_path, rows, configs)
    ledger.append({"event_type": "OBSERVATION"})
    ledger.finish({"status": "NOT_EVALUABLE"})
    with pytest.raises(ValueError, match="SEALED_REPLAY_LEDGER"):
        ledger.append({"event_type": "OBSERVATION"})
    (ledger.directory/"events"/"00000000.json").unlink()
    with pytest.raises(ValueError, match="LEDGER_SEAL_INTEGRITY_FAILURE"):
        ledger.events()


def test_offline_cli_register_and_train_without_clients(context, tmp_path):
    import os
    import subprocess
    import sys
    rows, configs, _ = replay_fixture(context)
    root = Path(__file__).resolve().parents[2]
    dataset = tmp_path/"dataset.json"
    dataset.write_text(canonical([{"snapshot_id": row.snapshot_id, "snapshot": row.snapshot,
        "feature_id": row.feature_id, "feature": row.feature, "alpha": row.alpha} for row in rows]), encoding="utf-8")
    configuration = tmp_path/"configuration.json"
    registration = replay_configuration(*configs) | {"cost_schedule": complete_schedule(),
        "expected_move_source": "VIX_SCALED_EXPIRY_MOVE", "sessions": {"TRAIN": [rows[0].snapshot.timestamp_ist.date().isoformat()],
        "VALIDATION": [], "FINAL_TEST": []}, "event_config": [], "experiment_axes": {"spread_widths": [[100]]}}
    configuration.write_text(canonical(registration), encoding="utf-8")
    arguments = [sys.executable, str(root/"scripts"/"run_integrity_replay.py"), "--dataset-json", str(dataset),
                 "--namespace", str(tmp_path/"evidence")]
    environment = {key: value for key, value in os.environ.items() if not any(
        secret in key.upper() for secret in ("KOTAK", "OPENAI", "TELEGRAM", "DATABASE_URL"))}
    registered = subprocess.run([*arguments, "--register", "--configuration-json", str(configuration)],
        cwd=root, env=environment, capture_output=True, text=True, timeout=60, check=True)
    run_id = json.loads(registered.stdout)["run_id"]
    executed = subprocess.run([*arguments, "--run-id", run_id, "--split", "TRAIN"],
        cwd=root, env=environment, capture_output=True, text=True, timeout=60, check=True)
    summary = json.loads(executed.stdout)
    assert summary["denominators"]["closed_evaluable_trades"] == 1
    assert summary["net_rupees_for_quantity"] is not None
    final = subprocess.run([*arguments, "--run-id", run_id, "--split", "FINAL_TEST"],
        cwd=root, env=environment, capture_output=True, text=True, timeout=60)
    assert final.returncode != 0 and "invalid choice" in final.stderr


def test_postgresql_migration_ddl_compiles_offline():
    from io import StringIO
    project = Path(__file__).resolve().parents[2]
    buffer = StringIO()
    config = Config(str(project/"alembic.ini"), output_buffer=buffer)
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline:dummy@localhost/offline")
    command.upgrade(config, "0013_phase14_2_strategy_logic:0014_phase14_2_1_replay_integrity", sql=True)
    sql = buffer.getvalue()
    assert "ADD COLUMN depth_unit" in sql and "DEFAULT 'UNKNOWN'" in sql
    assert "ADD COLUMN tick_size NUMERIC(20, 6)" in sql


def test_raw_loader_preserves_missing_features_and_legacy_time_semantics(repository, session_factory, context):
    import importlib.util
    path = Path(__file__).resolve().parents[2]/"scripts"/"run_alpha_experiments.py"
    spec = importlib.util.spec_from_file_location("legacy_experiments", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = context[0].model_copy(update={"response_received_at": context[0].timestamp_ist+timedelta(seconds=2)})
    repository.save_market_snapshot(raw, raw.timestamp_ist)
    with session_factory() as session:
        assert module.load_rows(session) == []
        rows = module.load_rows(session, preserve_missing=True)
        assert len(rows) == 1 and rows[0].feature is None and rows[0].feature_id is None
        assert rows[0].snapshot.options[0].source_market_timestamp.tzinfo is not None
        record = session.get(MarketSnapshotRecord, rows[0].snapshot_id)
        assert raw_record_to_model(record).response_received_at == record.response_received_at
        assert raw_record_to_model(record).options[0].source_market_timestamp == record.options[0].source_market_timestamp


@pytest.mark.parametrize("changes", [{"spread_widths": (float("nan"),)}, {"minimum_credit_to_width": 0},
    {"profit_target_credit_capture_pct": 0}, {"stop_loss_credit_multiple": float("inf")},
    {"dte_bucket": "LE_0_DTE"}, {"dte_bucket": "imaginary"}])
def test_invalid_or_inactive_replay_parameters_rejected(context, changes):
    from app.research.config import validate_replay_parameters
    _, configs, _ = replay_fixture(context)
    with pytest.raises(ValueError, match="INVALID|INACTIVE"):
        validate_replay_parameters(replace(configs[0], **changes), configs[1], configs[4])


def test_two_lot_authoritative_replay_preserves_fixed_order_brokerage(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    one, _ = execute(tmp_path/"one", rows, configs)
    schedule = complete_schedule()
    manifest = create_manifest(rows, code_sha="a"*40, config=replay_configuration(*configs, quantity=2),
        cost_schedule=schedule, expected_move_source="VIX_SCALED_EXPIRY_MOVE", event_config=[],
        sessions={"TRAIN": [rows[0].snapshot.timestamp_ist.date().isoformat()], "VALIDATION": [], "FINAL_TEST": []})
    ledger = ResearchLedger(tmp_path/"two", manifest, create=True)
    two = replay_parameters(rows, *configs[:-1], integrity_config=configs[-1], manifest=manifest,
                            ledger=ledger, cost_schedule=schedule, quantity=2)
    a = next(item for item in one["attempts"] if item["state"] == "CLOSED")
    b = next(item for item in two["attempts"] if item["state"] == "CLOSED")
    assert b["requested_units"] == 100
    assert b["final_outcome"]["brokerage"] == a["final_outcome"]["brokerage"]
    assert b["final_outcome"]["order_count"] == 4
    assert b["final_outcome"]["gross_rupees"] == pytest.approx(2*a["final_outcome"]["gross_rupees"], abs=.02)
    assert b["gross_max_loss_rupees_for_quantity"] == pytest.approx(2*a["gross_max_loss_rupees_for_quantity"])
