"""Phase 14.1 causal semantics and conservative research audit checks."""

from datetime import date, datetime, timedelta, time
from math import log
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.alpha.atm import matching_contract, select_atm
from app.alpha.clock import (LookbackClockMode, lookback_window, session_id,
                             trading_minutes_between)
from app.alpha.engine import (AlphaConfig, build_alpha_features, direction,
                              rank_sign_conflict, raw_components, signal_persistence)
from app.alpha.experiments import (ExperimentParameters, bounded_parameter_grid,
                                   chronological_split, evaluate_parameters)
from app.alpha.models import (ALPHA_VERSION, AlphaDirection, AlphaHypothesis,
                              AlphaValidity, CalculationMode, JointAlphaDirection)
from app.alpha.repository import AlphaRepository
from app.alpha.price_alpha import horizon_log_return
from app.alpha.replay import ReplayCosts, ReplayRow, quote_pair, replay_parameters, sample_tier
import app.alpha.replay as replay_module
from app.alpha.validation import prefix_replay_equivalent, validate_session, volume_reconciliation
from app.alpha.volume_alpha import VolumeState, interval_volume, volume_ratio
from app.db.models import AlphaFeatureSnapshotRecord, ResearchExperimentRecord
from app.db.repositories import SnapshotRepository
from app.regime.alpha_signals import statistical_alpha_signals
from app.regime.scoring import RegimeWeights, scored_signal, weighted_scores
from app.regime.futures_signals import basis_signal, futures_signal
from app.regime.engine import RegimeConfig
from app.risk.limits import RiskConfig
from app.shadow.exits import ShadowConfig
from app.strategy.candidate_engine import StrategyConfig
from tests.test_phase6_strategy import phase6_context
from tests.test_phase14_alpha import EXPIRY, alpha_config, snapshot

IST = ZoneInfo("Asia/Kolkata")


def at(day: date, hour: int = 9, minute: int = 15):
    return datetime.combine(day, time(hour, minute), IST)


def move(point, timestamp, **updates):
    return point.model_copy(update={"timestamp_ist": timestamp, **updates})


@pytest.mark.parametrize("mode,expected", [
    (LookbackClockMode.WALL_CLOCK, False),
    (LookbackClockMode.TRADING_MINUTES, True),
    (LookbackClockMode.SESSION_ONLY, False),
])
def test_clock_modes_across_overnight(mode, expected):
    friday = move(snapshot(0), at(date(2026, 10, 2), 15, 20))
    monday = at(date(2026, 10, 5), 9, 20)
    assert (friday in lookback_window([friday], monday, 800, mode)) is expected


def test_weekend_and_observed_holiday_excluded_from_trading_clock():
    friday, monday = at(date(2026, 10, 2), 15, 20), at(date(2026, 10, 5), 9, 20)
    assert trading_minutes_between(friday, monday, {friday.date(), monday.date()}) == 15
    tuesday = at(date(2026, 10, 6), 9, 20)
    assert trading_minutes_between(friday, tuesday, {friday.date(), tuesday.date()}) == 15
    monday_holiday = frozenset({date(2026, 10, 5)})
    assert trading_minutes_between(friday, tuesday,
                                   {friday.date(), monday.date(), tuesday.date()},
                                   monday_holiday) == 15
    holiday_point = move(snapshot(0), monday)
    assert holiday_point not in lookback_window([holiday_point], tuesday, 800,
                                                 LookbackClockMode.TRADING_MINUTES,
                                                 holidays=monday_holiday)


def test_800_trading_minutes_reaches_prior_sessions():
    days = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)]
    points = [move(snapshot(0), at(day)) for day in days]
    current = at(date(2026, 10, 6))
    selected = lookback_window(points, current, 800, LookbackClockMode.TRADING_MINUTES)
    assert points[0] not in selected and points[1] in selected and points[2] in selected


def test_session_id_is_exchange_local():
    assert session_id(at(date(2026, 10, 5))) == "NSE_2026-10-05"
    assert session_id(at(date(2026, 10, 6))) != session_id(at(date(2026, 10, 5)))


@pytest.mark.parametrize("price,expected_sign", [(25060, 1), (24940, -1)])
def test_signed_log_return(price, expected_sign):
    current = snapshot(6, future=price)
    result = horizon_log_return(current, [snapshot(0, future=25000)], "FUTURE")
    assert result.value == pytest.approx(log(price / 25000))
    assert result.value * expected_sign > 0
    assert result.actual_seconds == 360 and result.error_seconds == 60


def test_no_overnight_return_even_when_clock_spans_sessions():
    prior = snapshot(0)
    current = move(snapshot(0), at(date(2026, 10, 6), 9, 21))
    assert horizon_log_return(current, [prior], "FUTURE").reason == "HORIZON_UNAVAILABLE"


def test_future_expiry_or_identity_change_rejected():
    prior = snapshot(0)
    current = snapshot(6).model_copy(update={"future_expiry": date(2026, 11, 26)})
    assert horizon_log_return(current, [prior], "FUTURE").value is None
    current = snapshot(6).model_copy(update={"future_instrument_id": "OTHER_FUT"})
    assert horizon_log_return(current, [prior], "FUTURE").value is None


def test_stale_reference_rejected_and_source_time_preserved():
    current = snapshot(6)
    current = current.model_copy(update={"source_market_timestamp": current.timestamp_ist - timedelta(minutes=20)})
    result = horizon_log_return(current, [snapshot(0)], "FUTURE")
    assert result.value is None and result.reason == "STALE_REFERENCE_PRICE"
    assert result.reference_age_seconds == 1200


def test_three_minute_observation_is_not_five_minute_return():
    assert horizon_log_return(snapshot(3), [snapshot(0)], "FUTURE").value is None
    assert horizon_log_return(snapshot(6), [], "FUTURE").value is None


def test_horizon_seconds_and_version_persisted_in_result():
    result = build_alpha_features(5, snapshot(6, future=25030), [snapshot(0)], [], alpha_config())
    assert result.actual_horizon_seconds == 360
    assert result.horizon_error_seconds == 60
    assert result.target_horizon_seconds == 300
    assert result.signed_log_return == pytest.approx(log(25030 / 25020))
    assert result.alpha_version == ALPHA_VERSION


@pytest.mark.parametrize("rank,signed,expected", [
    (1.0, -.01, AlphaDirection.NEUTRAL),
    (0.0, .01, AlphaDirection.NEUTRAL),
    (.9, .01, AlphaDirection.STRONG_BULLISH),
    (.1, -.01, AlphaDirection.STRONG_BEARISH),
    (.75, .01, AlphaDirection.BULLISH),
    (.25, -.01, AlphaDirection.BEARISH),
])
def test_continuation_uses_sign_and_rank(rank, signed, expected):
    assert direction(rank, alpha_config(), signed) == expected


def test_rank_sign_conflict_warning_condition():
    assert rank_sign_conflict(.95, -.01, alpha_config())
    assert rank_sign_conflict(.05, .01, alpha_config())
    assert not rank_sign_conflict(.95, .01, alpha_config())


@pytest.mark.parametrize("rank,signed,expected", [
    (.9, .01, AlphaDirection.STRONG_BEARISH),
    (.1, -.01, AlphaDirection.STRONG_BULLISH),
])
def test_reversal_separate_from_continuation(rank, signed, expected):
    config = alpha_config(hypothesis_type=AlphaHypothesis.REVERSAL)
    assert direction(rank, config, signed) == expected
    assert not rank_sign_conflict(rank, signed, config)


def test_first_volume_observation_is_baseline_only():
    current = snapshot(0)
    assert interval_volume(current.options[0], None, current).state == VolumeState.SESSION_BASELINE_ONLY


def test_cross_session_volume_subtraction_is_impossible():
    previous = snapshot(0, ce_volume=10000)
    current = move(snapshot(0, ce_volume=5), at(date(2026, 10, 6)))
    result = interval_volume(current.options[0], previous, current)
    assert result.value is None and result.state == VolumeState.SESSION_BASELINE_ONLY


@pytest.mark.parametrize("field,value", [
    ("instrument_token", "other"), ("expiry", date(2026, 10, 15)),
    ("strike", 25100), ("option_type", "PE"), ("exchange", "bse_fo"),
])
def test_volume_contract_identity_is_strict(field, value):
    previous, current = snapshot(0), snapshot(3)
    changed = current.options[0].model_copy(update={field: value})
    assert interval_volume(changed, previous, current).state == VolumeState.CONTRACT_CHANGED
    assert matching_contract(previous, changed) is None


@pytest.mark.parametrize("volume,state,delta", [
    (120, VolumeState.VALID_INTERVAL, 20),
    (100, VolumeState.VALID_ZERO, 0),
    (5, VolumeState.RESET, None),
    (None, VolumeState.MISSING, None),
])
def test_volume_states(volume, state, delta):
    prior, current = snapshot(0, ce_volume=100), snapshot(3, ce_volume=volume)
    result = interval_volume(current.options[0], prior, current)
    assert result.state == state and result.value == delta


def test_gap_volume_is_diagnostic_not_activity():
    prior, current = snapshot(0, ce_volume=100), snapshot(12, ce_volume=1000)
    result = interval_volume(current.options[0], prior, current, max_interval_seconds=420)
    assert result.state == VolumeState.GAP_VOLUME_INTERVAL
    assert result.value is None and result.gap_cumulative_delta == 900
    assert result.duration_seconds == 720


def test_stale_and_corrected_volume_states():
    prior, current = snapshot(0), snapshot(3)
    stale = current.options[0].model_copy(update={
        "source_market_timestamp": current.timestamp_ist - timedelta(minutes=20)})
    assert interval_volume(stale, prior, current).state == VolumeState.STALE
    assert interval_volume(prior.options[0], current, prior).state == VolumeState.CORRECTED


def test_robust_activity_baseline_and_cap():
    ratio, baseline = volume_ratio(100, [1, 2, 2, 1000], cap=10)
    assert baseline == 2 and ratio == 10
    assert volume_ratio(100, [0, 0], minimum_baseline=1)[0] is None


def test_atm_pair_incomplete_warning_and_distance():
    base = snapshot(0)
    extra = base.options[0].model_copy(update={"strike": 24999,
                                               "instrument_token": "24999-CE"})
    selected = select_atm(base.model_copy(update={"options": base.options + [extra]}), 24999)
    assert selected.nearest_pair_incomplete and selected.distance_points == 1
    assert select_atm(base, 26000).distance_percent > .5


def test_new_atm_contract_does_not_match_old_strike_or_token():
    old, new = snapshot(0), snapshot(3, strike=25050)
    assert matching_contract(old, new.options[0]) is None
    raw = raw_components(new, [old], alpha_config())
    assert raw.ce_history_count == 1 and raw.ce_volatility is None


def test_broker_oi_change_is_separate_from_local_delta():
    history = [snapshot(0), snapshot(3)]
    current = snapshot(8)
    raw = raw_components(current, history, alpha_config())
    assert raw.broker_oi_ce == (1000, 900, 100)
    assert raw.local_oi_ce == 0


def test_underlying_volatility_and_standardized_return():
    prices = [25000, 25020, 24995, 25040, 25010, 25055, 25015, 25065,
              25035, 25080, 25045, 25090]
    history = [snapshot(3 * index, future=value, ce_volume=100+20*index,
                        pe_volume=100+25*index) for index, value in enumerate(prices)]
    current = snapshot(36, future=25100, ce_volume=360, pe_volume=400)
    raw = raw_components(current, history, alpha_config(min_volatility_returns=2))
    assert raw.underlying_volatility is not None and raw.underlying_volatility > 0
    assert raw.standardized_return == pytest.approx(raw.price_return / raw.underlying_volatility)
    floor = raw_components(current, history, alpha_config(volatility_min_valid_level=1))
    assert floor.underlying_volatility is None and floor.standardized_return is None


def _valid_seed():
    return build_alpha_features(1, snapshot(0), [], [], alpha_config()).model_copy(update={
        "timestamp": snapshot(0).timestamp_ist,
        "session_id": session_id(snapshot(0).timestamp_ist),
        "reference_instrument_id": "NIFTY-FUT-OCT26", "reference_expiry": date(2026, 10, 29),
        "atm_ce_token": "25000-CE", "atm_pe_token": "25000-PE",
        "validity_state": AlphaValidity.VALID,
        "joint_alpha_direction": JointAlphaDirection.BULLISH_CONFIRMATION,
    })


@pytest.mark.parametrize("change,reason", [
    ({"session_id": "NSE_2026-10-04"}, "SESSION_CHANGED"),
    ({"reference_instrument_id": "OLD_FUT"}, "REFERENCE_CHANGED"),
    ({"atm_ce_token": "OLD_CE"}, "CONTRACT_POLICY_CHANGED"),
    ({"expiry": date(2026, 10, 15)}, "EXPIRY_OR_HYPOTHESIS_CHANGED"),
    ({"validity_state": AlphaValidity.INVALID}, "PRIOR_ALPHA_INVALID"),
    ({"joint_alpha_direction": JointAlphaDirection.BEARISH_CONFIRMATION}, "OPPOSITE_OR_NEUTRAL_SIGNAL"),
])
def test_signal_persistence_resets(change, reason):
    prior = _valid_seed().model_copy(update=change)
    count, reset = signal_persistence(
        JointAlphaDirection.BULLISH_CONFIRMATION, [prior],
        timestamp=snapshot(3).timestamp_ist, session="NSE_2026-10-05",
        reference_id="NIFTY-FUT-OCT26", reference_expiry=date(2026, 10, 29),
        option_expiry=EXPIRY, ce_token="25000-CE", pe_token="25000-PE",
        hypothesis=AlphaHypothesis.CONTINUATION, max_gap_seconds=600,
        validity=AlphaValidity.VALID)
    assert count == 1 and reset == reason


def test_missing_poll_resets_signal_persistence():
    prior = _valid_seed()
    count, reason = signal_persistence(
        JointAlphaDirection.BULLISH_CONFIRMATION, [prior],
        timestamp=snapshot(12).timestamp_ist, session=prior.session_id,
        reference_id=prior.reference_instrument_id, reference_expiry=prior.reference_expiry,
        option_expiry=prior.expiry, ce_token=prior.atm_ce_token, pe_token=prior.atm_pe_token,
        hypothesis=prior.hypothesis_type, max_gap_seconds=600, validity=AlphaValidity.VALID)
    assert count == 1 and reason == "SIGNIFICANT_POLLING_GAP"


def test_hard_validity_gate_precedes_regime_soft_quality():
    alpha = _valid_seed().model_copy(update={"validity_state": AlphaValidity.INVALID,
                                             "alpha_1": .95, "alpha_2": .95})
    assert all(item.score == 0 for item in statistical_alpha_signals(alpha))


def test_price_movement_has_one_shared_cap():
    names = ["PRICE_STRUCTURE", "FUTURES", "STATISTICAL_PRICE_ALPHA",
             "STATISTICAL_VOLUME_VOL_ALPHA"]
    groups = [scored_signal(name, bull=1) for name in names]
    bull, _, _, contributions = weighted_scores(groups, RegimeWeights(price_movement_cap=5),
                                                group_caps_enabled=True)
    assert bull == pytest.approx(5) and sum(contributions.values()) == pytest.approx(5)


def test_positioning_group_has_shared_cap_when_alpha_enabled():
    names = ["DYNAMIC_OI", "OPTION_POSITIONING", "STATIC_OI", "PCR"]
    groups = [scored_signal(name, bull=1) for name in names]
    bull, _, _, _ = weighted_scores(groups, RegimeWeights(oi_dependency_cap=2),
                                    group_caps_enabled=True)
    assert bull == pytest.approx(2)


def test_basis_requires_synchronized_quotes_and_futures_price_does_not_double_vote(market_snapshot):
    _, feature, _ = phase6_context(market_snapshot, "BULLISH")
    assert basis_signal(feature, feature).score == 0
    price_only = futures_signal(feature, feature, include_basis=False)
    assert price_only.details.get("basis_change") is None


def test_activity_cannot_flip_direction():
    config = alpha_config()
    assert direction(.95, config, -.01) != AlphaDirection.BULLISH
    assert direction(.95, config, -.01) != AlphaDirection.STRONG_BULLISH


def test_recorded_outcome_filter_is_not_a_trade_replay():
    parameters = ExperimentParameters(.8, 2, time(9, 35), time(13, 30))
    result = evaluate_parameters([], parameters, 30)
    assert result["status"] == "NOT_EVALUABLE" and result["net_pnl"] is None


@pytest.mark.parametrize("updates", [
    {"spread_widths": (100,)}, {"short_strike_buffer": 150},
    {"entry_start": time(10, 0)}, {"force_exit_time": time(14, 0)},
])
def test_altered_trade_parameters_cannot_reuse_recorded_outcomes(updates):
    base = dict(alpha_threshold=.8, confirmations=2,
                entry_start=time(9, 35), entry_end=time(13, 30))
    result = evaluate_parameters([], ExperimentParameters(**(base | updates)), 30)
    assert result["status"] == "NOT_EVALUABLE" and result["expectancy"] is None


def test_parameter_grid_varies_construction_and_exit_rules_with_bound():
    grid = bounded_parameter_grid([.8], [2], [(time(9, 35), time(13, 30))], 4,
                                  spread_width_sets=[(50,), (100,)],
                                  force_exit_times=[time(14), time(15)])
    assert len(grid) == 4 and len({item.spread_widths for item in grid}) == 2
    with pytest.raises(ValueError, match="maximum"):
        bounded_parameter_grid([.8], [2], [(time(9, 35), time(13, 30))], 3,
                               spread_width_sets=[(50,), (100,)],
                               force_exit_times=[time(14), time(15)])


def test_split_boundary_purges_overlapping_trade_outcome():
    start = at(date(2026, 10, 5))
    trades = [SimpleNamespace(entry_timestamp=start + timedelta(minutes=index),
                              exit_timestamp=start + timedelta(minutes=10) if index == 0 else
                              start + timedelta(minutes=index)) for index in range(5)]
    split = chronological_split(trades)
    assert trades[0] not in split["TRAIN"]
    assert "FINAL_TEST" in split


@pytest.mark.parametrize("trades,sessions,expected", [
    (29, 20, "INSUFFICIENT"), (30, 20, "EARLY"), (100, 20, "MODERATE"),
    (500, 2, "MODERATE"), (500, 20, "STRONG_CANDIDATE"),
])
def test_sample_tiers_require_sessions(trades, sessions, expected):
    assert sample_tier(trades, sessions) == expected


def test_replay_cost_model_includes_all_components():
    costs = ReplayCosts(brokerage_per_order=20, exchange_rate=.001,
                        stt_rate=.001, gst_rate=.18, stamp_rate=.001,
                        slippage_points_per_leg=.5)
    assert costs.per_unit(50, 100, 100) > 2


def _quote_fixture(age: int = 2, skew: int = 0, depth: int | None = 10):
    current = snapshot(3)
    now = current.timestamp_ist
    short = current.options[0].model_copy(update={"bid": 19, "ask": 20,
        "source_market_timestamp": now - timedelta(seconds=age),
        "bid_quantity": depth, "ask_quantity": depth})
    long = current.options[1].model_copy(update={"bid": 9, "ask": 10,
        "source_market_timestamp": now - timedelta(seconds=age + skew),
        "bid_quantity": depth, "ask_quantity": depth})
    current = current.model_copy(update={"options": [short, long]})
    candidate = SimpleNamespace(short_leg=SimpleNamespace(instrument_token=short.instrument_token,
        strike=short.strike, expiry=short.expiry, option_type="CE"),
        long_leg=SimpleNamespace(instrument_token=long.instrument_token,
        strike=long.strike, expiry=long.expiry, option_type="PE"))
    return current, candidate


@pytest.mark.parametrize("age,skew,depth,expected", [
    (2, 0, 10, "OK"), (40, 0, 10, "STALE_LEG_QUOTE"),
    (2, 15, 10, "LEG_TIME_SKEW"), (2, 0, 0, "INSUFFICIENT_DEPTH"),
])
def test_replay_quote_freshness_skew_and_depth(age, skew, depth, expected):
    current, candidate = _quote_fixture(age, skew, depth)
    quote, reason = quote_pair(current, candidate, entry=True, quantity=1)
    assert reason == expected
    assert (quote is not None) == (expected == "OK")
    if quote:
        assert quote.fill_quality_state == "NEXT_OBSERVATION_SIMULATION"


def test_missing_exact_contract_or_quote_time_is_not_evaluable():
    current, candidate = _quote_fixture()
    candidate.long_leg.instrument_token = "WRONG"
    assert quote_pair(current, candidate, entry=False, quantity=1)[1] == "MISSING_EXACT_CONTRACT"
    current, candidate = _quote_fixture()
    current.options[0].source_market_timestamp = None
    assert quote_pair(current, candidate, entry=True, quantity=1)[1] == "MISSING_QUOTE_TIMESTAMP"


def test_crossed_book_is_not_a_replay_fill():
    current, candidate = _quote_fixture()
    current.options[0].bid = 25
    assert quote_pair(current, candidate, entry=True, quantity=1)[1] == "INVALID_OR_CROSSED_BOOK"


def test_volume_reconciliation_and_session_diagnostics():
    points = [snapshot(0, ce_volume=100, pe_volume=100),
              snapshot(3, ce_volume=110, pe_volume=120),
              snapshot(6, ce_volume=140, pe_volume=130)]
    report = volume_reconciliation(points)
    assert report["discrepancies"] == 0 and report["checked_intervals"] == 4
    session = validate_session(points, [])
    assert session["snapshot_count"] == 3
    assert session["cumulative_volume_baselines"] == 2
    assert session["actual_horizon_distribution_seconds"] == {}


def test_prefix_replay_does_not_use_later_snapshots():
    values = [snapshot(point, future=25000 + 10 * index,
                       ce_volume=100 + 10 * index, pe_volume=100 + 12 * index)
              for index, point in enumerate((0, 3, 6, 9, 12))]
    assert prefix_replay_equivalent(4, values[3], values, alpha_config())


def test_production_flags_remain_disabled():
    template = (Path(__file__).resolve().parents[2] / ".env.production.example").read_text()
    for flag in ("ALPHA_ENGINE_ENABLED", "REGIME_USE_STATISTICAL_ALPHA",
                 "STRATEGY_VOLATILITY_BUFFER_ENABLED", "PIPELINE_RUN_AI_RESEARCH"):
        assert f"{flag}=false" in template


def test_replay_runs_chronologically_without_existing_shadow_outcomes(market_snapshot):
    raw, feature, _ = phase6_context(market_snapshot, "NO_TRADE")
    parameters = ExperimentParameters(.8, 2, time(9, 35), time(13, 30))
    result = replay_parameters([ReplayRow(1, raw, feature, 1, None)], parameters,
                               StrategyConfig(), RiskConfig(), RegimeConfig(), ShadowConfig())
    assert result["status"] == "NOT_EVALUABLE"
    assert result["trades"] == 0 and result["net_pnl"] is None


@pytest.mark.parametrize("missing_exit,expected_status", [
    (False, "EVALUABLE"), (True, "NOT_EVALUABLE"),
])
def test_replay_fills_at_next_observation_and_requires_complete_exit_path(
    monkeypatch, missing_exit, expected_status
):
    start = at(date(2026, 10, 5), 9, 44)
    base, candidate = _quote_fixture()
    snapshots = []
    for index in range(3):
        timestamp = start + timedelta(minutes=3*index)
        options = [item.model_copy(update={"source_market_timestamp": timestamp})
                   for item in base.options]
        if index == 2:
            options[0] = options[0].model_copy(update={"bid": 11, "ask": 12})
            options[1] = options[1].model_copy(update={"bid": 9, "ask": 10})
            if missing_exit:
                options = options[:1]
        snapshots.append(base.model_copy(update={"timestamp_ist": timestamp,
                                                 "options": options, "lot_size": 50}))
    candidate.candidate_id = "CANDIDATE"
    candidate.selection_score = 80
    candidate.expiry = EXPIRY
    candidate.strategy_type = SimpleNamespace(value="BULL_PUT_SPREAD")
    candidate.support_or_resistance_reference = None
    alpha = _valid_seed().model_copy(update={"confirmed": True})
    feature = SimpleNamespace(volatility_features=SimpleNamespace(vix_regime="NORMAL"))
    rows = [ReplayRow(index+1, point, feature, index+1, alpha)
            for index, point in enumerate(snapshots)]
    monkeypatch.setattr(replay_module, "_relabel_alpha", lambda a, p, prior: a)
    monkeypatch.setattr(replay_module, "classify_regime",
                        lambda *a, **k: SimpleNamespace(regime=SimpleNamespace(value="BULLISH")))
    monkeypatch.setattr(replay_module, "generate_candidates",
                        lambda *a, **k: SimpleNamespace(candidates=[candidate]))
    monkeypatch.setattr(replay_module, "strategy_fingerprint", lambda *a: "FINGERPRINT")
    monkeypatch.setattr(replay_module, "evaluate_risk", lambda *a, **k: SimpleNamespace(
        decisions=[SimpleNamespace(decision="APPROVED", candidate_reference="CANDIDATE")]))
    parameters = ExperimentParameters(.8, 1, time(9, 35), time(13, 30))
    result = replay_parameters(rows, parameters, StrategyConfig(), RiskConfig(),
                               RegimeConfig(), ShadowConfig())
    assert result["status"] == expected_status
    if missing_exit:
        assert result["net_pnl"] is None and result["counts"]["missing"] >= 1
    else:
        assert result["net_pnl"] == pytest.approx(6)
        assert "09:45" in result["performance_by_entry_window"]
        assert result["sampled_MAE"] == result["sampled_MFE"] == 6
    boundary = replay_parameters(rows, parameters, StrategyConfig(), RiskConfig(),
                                 RegimeConfig(), ShadowConfig(), entry_period=(start, start),
                                 purge_unclosed_at_boundary=True)
    assert boundary["status"] == "NOT_EVALUABLE"
    assert boundary["counts"]["split_overlap_purged"] == 1
    assert boundary["counts"].get("missing", 0) == 0


def test_live_original_is_immutable_and_recompute_is_separate(session_factory):
    raw_repository = SnapshotRepository(session_factory)
    point = snapshot(0).model_copy(update={
        "source_market_timestamp": snapshot(0).timestamp_ist - timedelta(seconds=2),
        "request_started_at": snapshot(0).timestamp_ist - timedelta(seconds=4),
        "response_received_at": snapshot(0).timestamp_ist,
    })
    raw_id = raw_repository.save_market_snapshot(point, point.timestamp_ist).snapshot_id
    loaded = AlphaRepository(session_factory).load_raw(raw_id)
    assert loaded.future_instrument_id == "NIFTY-FUT-OCT26"
    assert loaded.future_expiry == date(2026, 10, 29)
    assert loaded.source_market_timestamp < loaded.response_received_at
    repository = AlphaRepository(session_factory)
    live = build_alpha_features(raw_id, point, [], [], alpha_config(), CalculationMode.LIVE_ORIGINAL)
    first_id = repository.upsert(live)
    changed = live.model_copy(update={"warnings": ["LATE_CORRECTION"]})
    assert repository.upsert(changed) == first_id
    assert "LATE_CORRECTION" not in repository.get(raw_id)["warnings"]
    recompute = changed.model_copy(update={"calculation_mode": CalculationMode.RESEARCH_RECOMPUTE})
    repository.upsert(recompute)
    assert "LATE_CORRECTION" not in repository.get(raw_id)["warnings"]
    with session_factory() as session:
        assert session.scalar(select(func.count(AlphaFeatureSnapshotRecord.id))) == 2


def test_historical_phase14_version_remains_distinguishable(session_factory):
    point = snapshot(0)
    raw_id = SnapshotRepository(session_factory).save_market_snapshot(point, point.timestamp_ist).snapshot_id
    repository = AlphaRepository(session_factory)
    current = build_alpha_features(raw_id, point, [], [], alpha_config())
    legacy = current.model_copy(update={"alpha_version": "phase14_v1"})
    repository.upsert(legacy)
    repository.upsert(current)
    assert repository.get(raw_id, "phase14_v1")["alpha_version"] == "phase14_v1"
    assert repository.get(raw_id)["alpha_version"] == ALPHA_VERSION


def test_validation_counts_duplicate_timestamps_and_slots():
    point = snapshot(3)
    report = validate_session([point, point], [])
    assert report["duplicate_timestamps"] == 1
    assert report["duplicate_collection_slots"] == 1


def test_validation_estimates_missing_full_session_slots():
    report = validate_session([snapshot(3)], [])
    assert report["missing_snapshots_estimate"] == report["expected_collection_slots"] - 1


def test_validation_reports_full_option_token_identity_changes():
    first, later = snapshot(3), snapshot(6)
    options = [item.model_copy(update={"instrument_token": "NEW_CE"})
               if item.option_type.value == "CE" else item for item in later.options]
    report = validate_session([first, later.model_copy(update={"options": options})], [])
    assert report["option_token_changes"] == 1


def test_validation_reports_atm_switch_independently():
    first, second = snapshot(3), snapshot(6, spot=25050, strike=25050)
    report = validate_session([first, second], [])
    assert report["atm_changes"] == 1 and report["atm_token_changes"] == 2


def test_validation_reports_horizon_distribution_and_rank_times():
    point = snapshot(3)
    alpha = _valid_seed().model_copy(update={"timestamp": point.timestamp_ist,
        "actual_horizon_seconds": 360, "alpha_1": .75, "alpha_2": .65,
        "rank_observations_alpha1": 55, "rank_observations_alpha2": 20})
    report = validate_session([point], [alpha])
    assert report["actual_horizon_distribution_seconds"] == {360: 1}
    assert report["first_alpha1_rank_at"] == point.timestamp_ist.isoformat()
    assert report["alpha1_history_count_max"] == 55


@pytest.mark.parametrize("warning,key", [
    ("VOLATILITY_TOO_SMALL", "volatility_floor_rejections"),
    ("STALE_REFERENCE_PRICE", "stale_data_rejections"),
])
def test_validation_counts_quality_rejections(warning, key):
    alpha = _valid_seed().model_copy(update={"warnings": [warning]})
    assert validate_session([snapshot(0)], [alpha])[key] == 1


def test_validation_reports_signal_persistence_reset_and_extreme_conflict():
    alpha = _valid_seed().model_copy(update={
        "confirmation_reset_reason": "SESSION_CHANGED", "alpha_1": 1.0,
        "alpha_2": 0.0, "warnings": ["RANK_SIGN_CONFLICT"]})
    report = validate_session([snapshot(0)], [alpha])
    assert report["signal_persistence_resets"]["SESSION_CHANGED"] == 1
    assert report["extreme_alpha_diagnostics"]["rank_sign_conflicts"] == 1
    assert report["extreme_alpha_diagnostics"]["alpha1_upper_1pct"] == 1


def test_validation_reports_source_receipt_gap_without_equating_them():
    point = snapshot(0)
    point = point.model_copy(update={
        "source_market_timestamp": point.timestamp_ist - timedelta(seconds=7),
        "response_received_at": point.timestamp_ist})
    report = validate_session([point], [])
    assert report["source_receipt_gap_seconds"] == {"known_count": 1, "min": 7.0, "max": 7.0}


def test_validation_counts_broker_vs_local_oi_mismatch():
    first, second = snapshot(0), snapshot(3)
    report = validate_session([first, second], [])
    assert report["oi_local_observations"] == 2
    assert report["oi_broker_vs_local_mismatch"] == 2


def test_volume_reconciliation_breaks_on_long_gap():
    first, late = snapshot(0, ce_volume=100), snapshot(15, ce_volume=1000)
    report = volume_reconciliation([first, late], max_interval_seconds=420)
    assert report["checked_intervals"] == 0 and report["classified_segment_breaks"] == 2


def test_trial_registry_can_audit_attempts(session_factory):
    with session_factory.begin() as session:
        session.add(ResearchExperimentRecord(
            config_hash="a" * 64, parameters_json={"width": 50}, split_name="TRAIN",
            train_period={"start": "2026-10-05", "end": "2026-10-05"},
            validation_period=None, test_period=None,
            result_summary={"status": "NOT_EVALUABLE"}, sample_tier="INSUFFICIENT"))
    with session_factory() as session:
        assert session.scalar(select(func.count(ResearchExperimentRecord.id))) == 1
