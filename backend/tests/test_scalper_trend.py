"""Causal trend context and adaptive paper entry, using synthetic labelled fixtures."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from app.scalper.config import ScalperConfig
from app.scalper.decisions import assess_entry
from app.scalper.features import build_features
from app.scalper.models import ScalperCursor, ScalperMarketSnapshotRecord
from app.scalper.paper import cost_schedule, exit_trigger
from app.scalper.replay import ReplayRow, ScalperReplayEngine
from app.scalper.risk import ScalperRiskState, evaluate_entry
from app.scalper.repository import _aware
from app.scalper.service import ScalperService, verify_scalper_journal
from app.scalper.signals import build_signal
from app.scalper.strategy import build_candidates
from tests.test_scalper import EXPIRY, IST, settings, snapshot


def trend_history(direction=1, count=82):
    history = []
    for index in range(count):
        raw = snapshot(index, direction=direction)
        at = datetime(2099, 10, 1, 9, 15, tzinfo=IST) + timedelta(seconds=index * 15)
        spot = 25000 + direction * index * 3
        history.append(raw.model_copy(update={
            "captured_at": at, "request_started_at": at - timedelta(seconds=1),
            "response_received_at": at, "source_market_timestamp": at,
            "nifty_spot": spot, "nifty_future": spot + 25,
            "future_instrument_id": "NIFTY-FUT", "future_expiry": EXPIRY,
            "atm_strike": round(spot / 50) * 50,
            "quotes": [quote.model_copy(update={"source_market_timestamp": at})
                       for quote in raw.quotes],
        }))
    return history


def last_signals(history, config=None):
    config = config or ScalperConfig.from_settings(settings(scalper_mixed_min_score=80))
    signals = []
    for end in range(1, len(history) + 1):
        features = build_features(history[:end], expected_interval_seconds=15)
        signals.append(build_signal(history[end - 1], features, signals, **config.signal_policy()))
    return signals


def test_postgresql_utc_timestamps_keep_ist_session_gates():
    raw = trend_history()[-1]
    local = _aware(raw.captured_at.astimezone(timezone.utc))
    assert local.isoformat() == raw.captured_at.isoformat()
    signal = last_signals(trend_history())[-1]
    config = ScalperConfig.from_settings(settings())
    candidate = build_candidates(raw, signal, config).candidates[0]
    assert evaluate_entry(candidate, signal, ScalperRiskState(), config, local).approved


@pytest.mark.parametrize("direction,name", [(1, "BULL"), (-1, "BEAR")])
def test_strong_trend_qualifies_below_80_with_two_confirmations(direction, name):
    history = trend_history(direction)
    signals = last_signals(history)
    signal = signals[-1]
    assert signal.fast_direction == signal.slow_direction == name
    assert signal.strong_slow_trend and signal.threshold_regime == "TREND_ALIGNED"
    assert 68 <= signal.combined_score < 80
    assert signal.applicable_entry_threshold == 68 and signal.confirmed
    # Two consecutive observations suffice, without requiring the earlier session sequence.
    first = build_signal(history[-2], build_features(history[:-1]), [],
                         max_confirmation_gap_seconds=22.5)
    second = build_signal(history[-1], build_features(history), [first],
                          max_confirmation_gap_seconds=22.5)
    assert first.confirmation_count == 1 and not first.confirmed
    assert second.confirmation_count == 2 and second.confirmed
    config = ScalperConfig.from_settings(settings(scalper_mixed_min_score=80))
    assessed, built, risk = assess_entry(history[-1], signal, ScalperRiskState(), config)
    assert built.candidates and risk.approved and assessed.entry_qualified
    assert assessed.primary_blockers == []


def test_countertrend_bounce_has_higher_threshold_and_cannot_borrow_confirmation():
    history = trend_history(-1)
    prior = last_signals(history[:-1])
    current = history[-1]
    current = current.model_copy(update={"nifty_spot": current.nifty_spot + 18,
                                         "nifty_future": current.nifty_future + 18})
    features = build_features([*history[:-1], current])
    signal = build_signal(current, features, prior, max_confirmation_gap_seconds=22.5)
    assert signal.fast_direction == "BULL" and signal.slow_direction == "BEAR"
    assert signal.trend_alignment == "OPPOSED"
    assert signal.threshold_regime == "COUNTERTREND" and signal.applicable_entry_threshold == 85
    assert not signal.confirmed and not signal.entry_qualified
    assert signal.contradiction_penalty > 0


def test_mixed_warmup_uses_80_and_execution_or_absolute_basis_cannot_create_direction():
    history = [item.model_copy(update={"nifty_spot": 25000, "nifty_future": 25200})
               for item in trend_history(count=3)]
    signal = last_signals(history)[-1]
    assert signal.threshold_regime == "MIXED" and signal.applicable_entry_threshold == 80
    assert signal.direction == "NEUTRAL" and signal.score == 0
    assert signal.confirmation_count == 0
    assert signal.components["execution"] > 0


def test_qualifying_direction_change_resets_confirmation():
    history = [snapshot(index, direction=-1) for index in range(3)]
    prior = build_signal(history[-1], build_features(history), [])
    assert prior.direction == "BEAR" and prior.score >= prior.applicable_entry_threshold
    current = snapshot(3, direction=1)
    changed = build_signal(current, build_features([*history, current]), [prior],
                           max_confirmation_gap_seconds=22.5)
    assert changed.direction == "BULL" and changed.score >= changed.applicable_entry_threshold
    assert changed.confirmation_count == 1 and not changed.confirmed


def test_adaptive_configuration_and_replay_overrides_are_validated():
    from app.scalper.replay import apply_replay_overrides
    config = ScalperConfig.from_settings(settings(scalper_mixed_min_score=80))
    changed, _ = apply_replay_overrides(config, {
        "SCALPER_TREND_ALIGNED_MIN_SCORE": 69, "SCALPER_COUNTERTREND_MIN_SCORE": 90})
    assert changed.trend_aligned_min_score == 69 and changed.countertrend_min_score == 90
    with pytest.raises(ValueError, match="ADAPTIVE_THRESHOLDS_INVALID"):
        apply_replay_overrides(config, {"SCALPER_MIXED_MIN_SCORE": 60})
    with pytest.raises(ValueError, match="ADAPTIVE_THRESHOLDS_INVALID"):
        ScalperConfig.from_settings(settings(scalper_trend_aligned_min_score=90))


def test_elapsed_horizons_opening_range_and_no_future_leakage():
    history = trend_history(count=82)
    early = build_features(history[:4]).intraday
    assert all(value is None for value in early.returns_bps.values())
    assert not early.opening_range_complete and early.opening_high_distance_bps is None
    context = build_features(history).intraday
    for label, seconds in (("1m", 60), ("3m", 180), ("5m", 300), ("15m", 900)):
        at = history[-1].captured_at - timedelta(seconds=seconds)
        reference = next(item for item in history if item.captured_at == at)
        assert context.horizon_reference_at[label] == at
        assert context.returns_bps[label] == pytest.approx(
            (history[-1].nifty_spot / reference.nifty_spot - 1) * 10000)
    assert context.opening_range_complete
    assert context.session_open == history[0].nifty_spot
    assert context.session_high == history[-1].nifty_spot
    # A reference well before the target is not relabelled as a one-minute return.
    sparse = build_features([history[0], history[-1]]).intraday
    assert sparse.returns_bps["1m"] is None
    engine = ScalperReplayEngine(ScalperConfig.from_settings(settings()), cost_schedule(settings()))
    rows = [ReplayRow(index + 1, item) for index, item in enumerate(history)]
    prefix = engine.run(rows[:70])
    full = engine.run(rows)
    assert prefix.signal_timeline == full.signal_timeline[:70]
    repeat = engine.run(rows)
    assert full.signal_timeline == repeat.signal_timeline
    assert full.trades == repeat.trades and full.summary == repeat.summary
    for key in ("qualified_signals", "wins", "losses", "score_distribution",
                "entry_score_distribution", "missed_strong_trend_signals",
                "signal_threshold_regime_split"):
        assert key in full.summary
    assert full.summary["performance"]["accounting_status"] == "GROSS_ONLY"


@pytest.mark.parametrize("field,value,reason", [
    ("bid", None, "MISSING_EXECUTABLE_SIDE"),
    ("bid_quantity", 0, "INSUFFICIENT_DEPTH"),
    ("source_market_timestamp", None, "MISSING_QUOTE_TIMESTAMP"),
    ("depth_unit", "UNKNOWN", "UNKNOWN_REQUIRED_DEPTH"),
    ("open_interest", 0, "OPEN_INTEREST_TOO_LOW"),
])
def test_trend_does_not_bypass_books(field, value, reason):
    history = trend_history()
    signal = last_signals(history)[-1]
    raw = history[-1].model_copy(update={"quotes": [
        quote.model_copy(update={field: value}) for quote in history[-1].quotes]})
    config = ScalperConfig.from_settings(settings())
    assessed, built, _ = assess_entry(raw, signal, ScalperRiskState(), config)
    assert signal.confirmed and not assessed.entry_qualified and not built.candidates
    assert "NO_EXECUTABLE_CANDIDATE" in assessed.primary_blockers
    assert reason in built.rejection_counts


@pytest.mark.parametrize("changes,reason", [
    ({"open_positions": 1}, "MAX_OPEN_POSITIONS_REACHED"),
    ({"unresolved_positions": 1}, "MAX_OPEN_POSITIONS_REACHED"),
    ({"executed_trades_today": 10}, "MAX_TRADES_PER_DAY_REACHED"),
    ({"gross_realized_today": -50000}, "HARD_DAILY_LOSS_REACHED"),
    ({"consecutive_losses": 3}, "MAX_CONSECUTIVE_LOSSES_REACHED"),
    ({"last_exit_at": True}, "ANY_EXIT_COOLDOWN_ACTIVE"),
    ({"last_loss_at": True}, "LOSS_COOLDOWN_ACTIVE"),
])
def test_trend_does_not_bypass_existing_risk(changes, reason):
    history = trend_history()
    signal = last_signals(history)[-1]
    raw = history[-1]
    changes = {key: raw.captured_at - timedelta(seconds=15) if value is True else value
               for key, value in changes.items()}
    config = ScalperConfig.from_settings(settings())
    candidate = build_candidates(raw, signal, config).candidates[0]
    risk = evaluate_entry(candidate, signal, ScalperRiskState(**changes), config, raw.captured_at)
    assert not risk.approved and reason in risk.reasons


def test_max_loss_event_kill_switch_and_adaptive_exit():
    history = trend_history()
    raw, signal = history[-1], last_signals(history)[-1]
    config = ScalperConfig.from_settings(settings())
    candidate = build_candidates(raw, signal, config).candidates[0]
    for configuration, event, reason in (
        (replace(config, max_loss_per_trade=1), False, "MAX_LOSS_PER_TRADE_EXCEEDED"),
        (replace(config, kill_switch=True), False, "KILL_SWITCH_ACTIVE"),
        (config, True, "MARKET_EVENT_BLOCKED"),
    ):
        decision = evaluate_entry(candidate, signal, ScalperRiskState(), configuration,
                                  raw.captured_at, event_blocked=event)
        assert not decision.approved and reason in decision.reasons
    document = {"entry_credit": 20, "entry_timestamp": raw.captured_at.isoformat(),
                "candidate": {"direction": "BULL"}}
    assert exit_trigger(document, raw, signal, 20, config) is None  # Below 80 is valid here.
    failed = signal.model_copy(update={"score": 67})
    assert exit_trigger(document, raw, failed, 20, config) == "SIGNAL_FAILURE"
    flipped = signal.model_copy(update={"direction": signal.direction.__class__.BEAR})
    assert exit_trigger(document, raw, flipped, 20, config) == "SIGNAL_FAILURE"
    assert exit_trigger(document, raw, signal, 31, config) == "SPREAD_STOP"


def test_every_live_observation_stores_diagnostics_and_replays(session_factory):
    configured = settings(scalper_mixed_min_score=80)
    with session_factory.begin() as session:
        session.add(ScalperCursor(id=1))
    service = ScalperService(session_factory, configured)
    history = trend_history(count=82)
    ids = [service.capture_once(item) for item in history]
    with session_factory() as session:
        records = session.scalars(select(ScalperMarketSnapshotRecord).order_by(
            ScalperMarketSnapshotRecord.id)).all()
        for record in records:
            signal = json.loads(record.signal_json)
            assert signal["applicable_entry_threshold"] in {68, 80, 85}
            assert signal["fast_direction"] and signal["slow_direction"]
            assert signal["primary_blockers"] or signal["entry_qualified"]
        rows = [ReplayRow(snapshot_id, raw, stored_signal_json=record.signal_json,
                          stored_feature_json=record.feature_json)
                for snapshot_id, raw, record in zip(ids, history, records)]
    replay = ScalperReplayEngine(service.config, cost_schedule(configured)).run(rows)
    assert all(row["LIVE_STORED_VS_REPLAY_SIGNAL_MATCH"] for row in replay.signal_timeline)
    assert all(row["LIVE_STORED_VS_REPLAY_FEATURE_MATCH"] for row in replay.signal_timeline)
    verify_scalper_journal(session_factory)
