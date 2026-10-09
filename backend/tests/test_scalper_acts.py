"""Frozen ACTS_V1 behavior on labelled synthetic, causally ordered observations."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from math import log
from pathlib import Path

import pytest
from sqlalchemy import insert

from app.alpha.engine import (AlphaConfig, build_alpha_features, direction,
                              joint_direction, raw_components)
from app.alpha.models import (AlphaDirection, AlphaEvidenceQuality, AlphaHypothesis, AlphaValidity,
                              CalculationMode, JointAlphaDirection)
from app.alpha.price_alpha import horizon_log_return
from app.alpha.ranks import percentile_rank
from app.db.models import AlphaFeatureSnapshotRecord, MarketSnapshotRecord
from app.scalper.config import ScalperConfig
from app.scalper.models import ScalperMarketSnapshotRecord, ScalperOptionQuoteRecord
from app.scalper.paper import cost_schedule
from app.scalper_lab import STRATEGY_IDS, acts
from app.scalper_lab.context import context_at
from app.scalper_lab.engine import (LabTrade, Ledger, ScalperLab, _candidate,
                                    _exit_reason, _risk_blockers)
from app.scalper_lab.io import load_local_sqlite
from tests.test_phase14_alpha import alpha_config, snapshot
from tests.test_scalper import settings
from tests.test_scalper_lab import observed


def alpha_for(row, classification="BULL", *, calculated_shift=0):
    seed = build_alpha_features(1, snapshot(0), [], [], alpha_config())
    value = {"STRONG_BULL": (.9, AlphaDirection.STRONG_BULLISH,
                             JointAlphaDirection.STRONG_BULLISH_CONFIRMATION),
             "BULL": (.75, AlphaDirection.BULLISH,
                      JointAlphaDirection.BULLISH_CONFIRMATION),
             "NEUTRAL": (.5, AlphaDirection.NEUTRAL, JointAlphaDirection.NEUTRAL),
             "BEAR": (.25, AlphaDirection.BEARISH,
                      JointAlphaDirection.BEARISH_CONFIRMATION),
             "STRONG_BEAR": (.1, AlphaDirection.STRONG_BEARISH,
                             JointAlphaDirection.STRONG_BEARISH_CONFIRMATION)}[classification]
    raw = row.snapshot
    return seed.model_copy(update={
        "snapshot_id": row.snapshot_id, "timestamp": raw.captured_at,
        "expiry": raw.expiry, "reference_instrument_id": raw.future_instrument_id,
        "reference_expiry": raw.future_expiry,
        "feature_calculated_at": raw.captured_at + timedelta(seconds=calculated_shift),
        "response_received_at": raw.captured_at, "alpha_1": value[0],
        "alpha_2": value[0], "alpha_1_direction": value[1],
        "alpha_2_direction": value[1], "joint_alpha_direction": value[2],
        "validity_state": AlphaValidity.VALID,
        "evidence_quality": AlphaEvidenceQuality.HIGH,
        "hypothesis_type": AlphaHypothesis.CONTINUATION,
        "calculation_mode": CalculationMode.LIVE_ORIGINAL})


def acts_context(rows, index=-1):
    row = rows[index]
    prior = rows[:index] if index >= 0 else rows[:-1]
    ctx = context_at(row, prior)
    ctx.update(future_instrument_id=row.snapshot.future_instrument_id,
               future_expiry=row.snapshot.future_expiry,
               expiry_date=row.snapshot.expiry)
    ctx["acts"] = acts.evidence(ctx, row.alpha)
    return ctx


def qualified_rows(direction=1, count=70, classification=None):
    rows = observed(count, direction)
    label = classification or ("BULL" if direction > 0 else "BEAR")
    return [replace(row, alpha=alpha_for(row, label)) for row in rows]


def test_original_alpha_formula_and_joint_mapping_are_reused():
    config = AlphaConfig()
    current = snapshot(8, future=25040)
    prior = snapshot(3, future=25020)
    raw = horizon_log_return(current, [snapshot(0, future=25000), prior], "FUTURE")
    assert raw.value == pytest.approx(log(25040 / 25020))
    assert percentile_rank(4, [1, 2, 4, 5], 4) == .625
    assert direction(.9, config, raw.value) == AlphaDirection.STRONG_BULLISH
    assert joint_direction(AlphaDirection.STRONG_BULLISH,
                           AlphaDirection.STRONG_BULLISH) == (
                               JointAlphaDirection.STRONG_BULLISH_CONFIRMATION)
    row = qualified_rows()[0]
    for label in ("STRONG_BULL", "BULL", "NEUTRAL", "BEAR", "STRONG_BEAR"):
        evidence = acts.alpha_evidence(alpha_for(row, label), row.snapshot.captured_at,
                                       row.snapshot.future_instrument_id,
                                       row.snapshot.future_expiry, row.snapshot.expiry)
        assert evidence["classification"] == label
    assert acts.alpha_state("STRONG_BULL", "BEAR") == "VETO"
    assert acts.alpha_state("NEUTRAL", "BEAR") == "NEUTRAL"
    assert acts.alpha_state("BEAR", "BEAR") == "SUPPORTIVE"
    conflicting = acts.alpha_evidence(alpha_for(row, "NEUTRAL"),
                                      row.snapshot.captured_at,
                                      row.snapshot.future_instrument_id,
                                      row.snapshot.future_expiry, row.snapshot.expiry)
    conflicting["alpha_1_direction"] = "STRONG_BULLISH"
    assert acts.relative_alpha_state(conflicting, "BEAR") == "VETO"


def test_original_alpha2_standardized_rank_flows_into_acts_unchanged():
    history = [snapshot(index, future=25000 + (index % 4) * 10 + index,
                        ce_volume=100 + index * 10, pe_volume=100 + index * 8)
               for index in range(20)]
    current = snapshot(20, future=25080, ce_volume=310, pe_volume=265)
    raw = raw_components(current, history, alpha_config())
    assert raw.standardized_return == pytest.approx(
        raw.price_return / raw.underlying_volatility)
    original = build_alpha_features(21, current, history, [], alpha_config())
    assert original.alpha_1 == 1.0
    assert original.alpha_2 == pytest.approx(6 / 7)
    live = original.model_copy(update={
        "calculation_mode": CalculationMode.LIVE_ORIGINAL,
        "feature_calculated_at": current.timestamp_ist})
    evidence = acts.alpha_evidence(live, current.timestamp_ist,
                                   current.future_instrument_id,
                                   current.future_expiry, current.expiry)
    assert evidence["alpha_1"] == original.alpha_1
    assert evidence["alpha_2"] == original.alpha_2
    assert evidence["classification"] == "STRONG_BULL"


def test_alpha_missing_or_future_calculated_fails_closed():
    rows = qualified_rows(-1)
    ctx = acts_context(rows)
    assert acts.decide(ctx, {})["alpha_state"] == "SUPPORTIVE"
    missing = replace(rows[-1], alpha=None)
    unavailable = acts_context([*rows[:-1], missing])
    decision = acts.decide(unavailable, {})
    assert decision["status"] == "NO_SETUP"
    assert "ALPHA_UNAVAILABLE" in decision["blockers"]
    assert decision["alpha"]["alpha_1"] is None
    future = replace(rows[-1], alpha=alpha_for(rows[-1], "BEAR", calculated_shift=1))
    assert acts_context([*rows[:-1], future])["acts"]["alpha"]["status"] == "ALPHA_UNAVAILABLE"
    wrong_id = rows[-1].alpha.model_copy(update={"reference_instrument_id": "OTHER"})
    assert acts_context([*rows[:-1], replace(rows[-1], alpha=wrong_id)])[
        "acts"]["alpha"]["status"] == "ALPHA_UNAVAILABLE"
    veto = replace(rows[-1], alpha=alpha_for(rows[-1], "STRONG_BULL"))
    assert "STRONG_OPPOSITE_ALPHA_VETO" in acts.decide(
        acts_context([*rows[:-1], veto]), {})["blockers"]
    neutral = replace(rows[-1], alpha=alpha_for(rows[-1], "NEUTRAL"))
    neutral_decision = acts.decide(acts_context([*rows[:-1], neutral]), {})
    assert neutral_decision["alpha_state"] == "NEUTRAL"
    assert "ALPHA_UNAVAILABLE" not in neutral_decision["blockers"]


def test_sqlite_loader_uses_only_matching_already_calculated_alpha(engine):
    row = observed(10)[-1]
    raw = row.snapshot
    original = alpha_for(row, "BULL")
    earlier = original.model_copy(update={
        "timestamp": raw.captured_at - timedelta(seconds=75),
        "feature_calculated_at": raw.captured_at - timedelta(seconds=70),
        "response_received_at": raw.captured_at - timedelta(seconds=70)})
    future = original.model_copy(update={
        "feature_calculated_at": raw.captured_at + timedelta(seconds=1)})
    with engine.begin() as connection:
        connection.execute(insert(ScalperMarketSnapshotRecord), [{
            **raw.model_dump(exclude={"quotes"}), "id": row.snapshot_id,
            "capture_key": "acts-loader", "feature_json": "{}", "signal_json": "{}"}])
        connection.execute(insert(ScalperOptionQuoteRecord), [
            {**quote.model_dump(), "scalper_snapshot_id": row.snapshot_id}
            for quote in raw.quotes])
        for identity, alpha in ((1, earlier), (2, future)):
            connection.execute(insert(MarketSnapshotRecord), [{
                "id": identity, "timestamp_ist": alpha.timestamp,
                "collection_bucket_ist": alpha.timestamp + timedelta(seconds=identity),
                "nifty_spot": raw.nifty_spot, "atm_strike": raw.atm_strike,
                "expiry": raw.expiry, "source": "TEST"}])
            connection.execute(insert(AlphaFeatureSnapshotRecord), [{
                "market_snapshot_id": identity, "alpha_version": alpha.alpha_version,
                "calculation_mode": alpha.calculation_mode.value,
                "timestamp": alpha.timestamp, "expiry": alpha.expiry,
                "price_source": alpha.price_source,
                "alpha_1_direction": alpha.alpha_1_direction.value,
                "alpha_2_direction": alpha.alpha_2_direction.value,
                "joint_alpha_direction": alpha.joint_alpha_direction.value,
                "consecutive_confirmation_count": 1,
                "evidence_quality": alpha.evidence_quality.value,
                "warnings_json": [], "result_json": alpha.model_dump(mode="json")}])
    loaded = load_local_sqlite(Path(engine.url.database), raw.captured_at.date(),
                               raw.captured_at.date())
    assert len(loaded) == 1
    assert loaded[0].alpha is not None
    assert loaded[0].alpha.timestamp == earlier.timestamp
    assert loaded[0].alpha.feature_calculated_at <= loaded[0].snapshot.captured_at


@pytest.mark.parametrize("direction", [1, -1])
def test_regime_uses_futures_structure_and_only_authoritative_vwap(direction):
    rows = qualified_rows(direction)
    ctx = acts_context(rows)
    expected = "BULL" if direction > 0 else "BEAR"
    assert acts.regime(ctx)[0] == expected
    missing = replace(rows[-1], futures=None)
    unavailable = acts_context([*rows[:-1], missing])
    assert unavailable["vwap"]["value"] is None
    assert unavailable["acts"]["regime"]["vwap"]["status"] != "AVAILABLE"
    assert acts.regime(unavailable)[0] == expected
    reversed_5m = deepcopy(ctx)
    reversed_5m["futures_returns_bps"]["5m"] = -direction * 5
    assert acts.regime(reversed_5m)[0] is None
    reversed_mid = deepcopy(ctx)
    reversed_mid["spot"] = (ctx["opening_high"] + ctx["opening_low"]) / 2 - direction
    assert acts.regime(reversed_mid)[0] is None


@pytest.mark.parametrize("direction", [1, -1])
def test_positioning_requires_two_slow_checks_and_vetoes_strong_opposite(direction):
    ctx = acts_context(qualified_rows(direction))
    side = "BULL" if direction > 0 else "BEAR"
    result = acts.positioning(ctx, side)
    assert result["qualified"] and result["checks"]["A"] and result["checks"]["B"]
    assert result["checks"]["D"]
    assert result["reference_timestamps"]["3m"] is not None
    assert result["reference_timestamps"]["5m"] is not None
    assert ctx["options"]["oi_basis"] == "SELF_COMPUTED_EXACT_CONTRACT_HISTORY"
    alone = deepcopy(ctx)
    wall = alone["options"]["walls"]["put_support" if side == "BULL" else "call_resistance"]
    opposing = alone["options"]["walls"]["call_resistance" if side == "BULL" else "put_support"]
    for horizon in ("3m", "5m"):
        wall["cluster_delta_oi"][horizon] = None
        wall["activity"][horizon] = "UNKNOWN"
        opposing["cluster_delta_oi"][horizon] = None
        for radius in ("3", "5"):
            alone["options"]["local_pcr"][radius]["oi_slope_per_minute"][horizon] = None
    assert wall["cluster_delta_oi"]["1m"] is not None
    assert not acts.positioning(alone, side)["qualified"]
    pcr_only = deepcopy(alone)
    pcr_only["options"]["local_pcr"]["3"]["oi_slope_per_minute"]["3m"] = direction * .01
    assert acts.positioning(pcr_only, side)["checks"]["D"]
    assert not acts.positioning(pcr_only, side)["qualified"]
    opposite = deepcopy(ctx)
    wall = opposite["options"]["walls"]["put_support" if side == "BULL" else "call_resistance"]
    other = opposite["options"]["walls"]["call_resistance" if side == "BULL" else "put_support"]
    for horizon in ("3m", "5m"):
        wall["cluster_delta_oi"][horizon] = -100
        wall["activity"][horizon] = "LONG_BUILDUP"
        other["cluster_delta_oi"][horizon] = 100
        for radius in ("3", "5"):
            slope = opposite["options"]["local_pcr"][radius]["oi_slope_per_minute"]
            slope[horizon] = -direction * .01
    assert acts.positioning(opposite, side)["opposite_veto"]


def test_pullback_60_seconds_and_three_point_resumption_without_chasing():
    rows = qualified_rows(1, 74)
    peak = rows[64].snapshot.nifty_spot
    values = {65: peak - 2, 66: peak - 3, 67: peak - 4,
              68: peak - 5, 69: peak - 6, 70: peak - 6,
              71: peak - 3, 72: peak - 1, 73: peak + 1}
    for index, value in values.items():
        rows[index] = replace(rows[index], snapshot=rows[index].snapshot.model_copy(
            update={"nifty_spot": value}))
    state = {}
    assert acts.decide(acts_context(rows, 64), state)["status"] == "NO_SETUP"
    gap_state = {}
    assert acts.decide(acts_context(rows, 65), gap_state)["status"] == "WATCH"
    assert acts.decide(acts_context(rows, 70), gap_state)["status"] == "NO_SETUP"
    statuses = [acts.decide(acts_context(rows, index), state)["status"]
                for index in range(65, 72)]
    assert statuses == ["WATCH"] * 6 + ["ENTER"]
    lab = ScalperLab(ScalperConfig.from_settings(settings()), cost_schedule(settings()))
    result = lab.run(rows)
    decisions = [row for row in result.decisions if row["strategy_id"] == acts.STRATEGY_ID]
    entered = [row for row in decisions if row["status"] == "ENTER"]
    assert len(entered) == 1
    thesis = entered[0]["thesis"]
    assert thesis["pullback_duration_seconds"] >= 60
    assert thesis["positioning_checks"]["A"] and thesis["positioning_checks"]["B"]
    assert thesis["alpha"]["classification"] == "BULL"
    assert result.summaries[acts.STRATEGY_ID]["executed_trades"] == 1
    trade = next(row for row in result.trades if row["strategy_id"] == acts.STRATEGY_ID)
    assert "entry_credit" not in thesis
    assert trade["decision"]["thesis"]["entry_credit"] == trade["entry_credit"]
    assert trade["decision"]["thesis"]["defined_max_loss"] <= (
        ScalperConfig.from_settings(settings()).max_loss_per_trade)
    assert trade["candidate"]["construction_method"] == "SHORT_LEG_FIRST_V1"
    assert trade["candidate"]["spread_width"] in (100, 200, 300, 400)
    assert trade["entry_quotes"]["short"]["fill_price"] == pytest.approx(
        trade["candidate"]["quote_evidence"]["short"]["bid"])
    assert len(result.acts_ledger) == 1
    assert result.manifest["acts_v1"]["strategy_version"] == acts.STRATEGY_ID
    assert result == ScalperLab(ScalperConfig.from_settings(settings()),
                                cost_schedule(settings())).run(rows)
    prefix = lab.run(rows[:72])
    assert prefix.decisions == result.decisions[:72 * len(STRATEGY_IDS)]
    high_score = ScalperLab(replace(ScalperConfig.from_settings(settings()),
                                    signal_min_score=100), cost_schedule(settings())).run(rows)
    assert high_score.summaries[acts.STRATEGY_ID]["executed_trades"] == 1


def test_phase15_only_observations_report_alpha_unavailable_without_trades():
    rows = observed(70, -1)
    result = ScalperLab(ScalperConfig.from_settings(settings()),
                        cost_schedule(settings())).run(rows)
    decisions = [row for row in result.decisions if row["strategy_id"] == acts.STRATEGY_ID]
    assert all(row["alpha"]["classification"] == "ALPHA_UNAVAILABLE" for row in decisions)
    assert all(row["status"] != "ENTER" for row in decisions)
    assert result.summaries[acts.STRATEGY_ID]["executed_trades"] == 0


def held_trade(direction=1):
    rows = qualified_rows(direction)
    ctx = acts_context(rows)
    decision = acts.decide(ctx, {})
    decision["direction"] = "BULL" if direction > 0 else "BEAR"
    decision["status"] = "ENTER"
    decision["thesis"] = {"wall": ctx["options"]["walls"][
        "put_support" if direction > 0 else "call_resistance"]}
    config = ScalperConfig.from_settings(settings())
    candidate, _ = _candidate(rows[-1].snapshot, decision, ctx, config)
    assert candidate is not None
    trade = LabTrade(acts.STRATEGY_ID, candidate, decision, rows[-1].snapshot_id,
                     state="OPEN", entry_credit=20,
                     entry_timestamp=rows[-1].snapshot.captured_at)
    return trade, ctx, config


def test_slow_thesis_exits_require_elapsed_confirmation_not_noisy_flips():
    trade, ctx, config = held_trade()
    now = datetime.fromisoformat(ctx["timestamp"])
    noise = deepcopy(ctx)
    noise["fast_direction"] = "BEAR"
    assert _exit_reason(trade, noise, 20, config, event_blocked=False) is None
    one_minute = deepcopy(ctx)
    row = next(row for row in one_minute["options"]["contracts"]
               if row["identity"] == trade.decision["thesis"]["wall"]["identity"])
    row["delta_oi"]["1m"] = -10000
    row["activity"]["1m"] = "SHORT_COVERING"
    assert _exit_reason(trade, one_minute, 20, config, event_blocked=False) is None
    trend = deepcopy(ctx)
    trend["futures_returns_bps"].update({"5m": -5, "15m": -9})
    assert _exit_reason(trade, trend, 20, config, event_blocked=False) is None
    for seconds in (15, 30, 45):
        trend["timestamp"] = (now + timedelta(seconds=seconds)).isoformat()
        assert _exit_reason(trade, trend, 20, config, event_blocked=False) is None
    trend["timestamp"] = (now + timedelta(seconds=60)).isoformat()
    assert _exit_reason(trade, trend, 20, config, event_blocked=False) == (
        "SUSTAINED_TREND_REVERSAL")


def test_strong_alpha_wall_and_structure_reversals_are_sustained():
    for reason in ("alpha", "wall", "structure"):
        trade, ctx, config = held_trade(-1)
        now = datetime.fromisoformat(ctx["timestamp"])
        adverse = deepcopy(ctx)
        if reason == "alpha":
            adverse["acts"]["alpha"]["classification"] = "STRONG_BULL"
            adverse["acts"]["alpha"]["alpha_1_direction"] = "STRONG_BULLISH"
            adverse["acts"]["alpha"]["alpha_2_direction"] = "STRONG_BULLISH"
            expected, seconds = "SUSTAINED_STRONG_ALPHA_REVERSAL", 60
        elif reason == "wall":
            row = next(row for row in adverse["options"]["contracts"]
                       if row["identity"] == trade.decision["thesis"]["wall"]["identity"])
            for horizon in ("3m", "5m"):
                row["delta_oi"][horizon] = -1000
                row["activity"][horizon] = "SHORT_COVERING"
            expected, seconds = "SUSTAINED_OI_POSITIONING_REVERSAL", 180
        else:
            strike = trade.decision["thesis"]["wall"]["strike"]
            adverse["spot"] = strike + 11
            adverse["future"] = strike + 11
            expected, seconds = "SUSTAINED_STRUCTURE_BREAK", 60
        assert _exit_reason(trade, adverse, 20, config, event_blocked=False) is None
        for elapsed in range(15, seconds, 15):
            adverse["timestamp"] = (now + timedelta(seconds=elapsed)).isoformat()
            assert _exit_reason(trade, adverse, 20, config, event_blocked=False) is None
        adverse["timestamp"] = (now + timedelta(seconds=seconds)).isoformat()
        assert _exit_reason(trade, adverse, 20, config, event_blocked=False) == expected


def test_frozen_profit_time_hard_risk_and_daily_limits():
    trade, ctx, config = held_trade()
    now = datetime.fromisoformat(ctx["timestamp"])
    assert _exit_reason(trade, ctx, 10, config, event_blocked=False) == "PROFIT_CAPTURE"
    late = deepcopy(ctx)
    late["timestamp"] = (now + timedelta(minutes=30)).isoformat()
    assert _exit_reason(trade, late, 20, config, event_blocked=False) == "TIME_STOP"
    late["timestamp"] = now.replace(hour=15, minute=20).isoformat()
    assert _exit_reason(trade, late, 20, config, event_blocked=False) == "FORCED_CLOSE"
    assert _exit_reason(trade, ctx, 31, config, event_blocked=False) == "SPREAD_STOP"
    assert _exit_reason(trade, ctx, 20, replace(config, kill_switch=True),
                        event_blocked=False) == "KILL_SWITCH"
    assert _exit_reason(trade, ctx, 20, config, event_blocked=True) == "MARKET_EVENT_EXIT"
    ledger = Ledger(trades=[trade])
    assert "MAX_OPEN_POSITIONS_REACHED" in _risk_blockers(
        ledger, replace(config, max_trades_per_day=3), now, False)
    for index in range(3):
        item = replace(trade, state="CLOSED", entry_timestamp=now - timedelta(hours=1),
                       exit_timestamp=now - timedelta(minutes=45 - index),
                       outcome={"gross_rupees": 100})
        ledger.trades.append(item)
    ledger.trades.remove(trade)
    assert "MAX_TRADES_PER_DAY_REACHED" in _risk_blockers(
        ledger, replace(config, max_trades_per_day=3), now, False)


def test_original_four_comparison_rows_unchanged(monkeypatch):
    rows = qualified_rows(1, 70)
    lab = ScalperLab(ScalperConfig.from_settings(settings()), cost_schedule(settings()))
    five = lab.run(rows)
    assert [row["strategy_id"] for row in five.comparison] == list(STRATEGY_IDS)
    with monkeypatch.context() as patch:
        patch.setattr("app.scalper_lab.engine.STRATEGY_IDS", STRATEGY_IDS[:4])
        four = lab.run(rows)
    assert five.comparison[:4] == four.comparison
    assert [row for row in five.decisions if row["strategy_id"] != acts.STRATEGY_ID] == (
        list(four.decisions))
