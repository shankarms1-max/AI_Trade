"""ACTS_V3_MCX_FUTURES replay + research harness tests (SYNTHETIC random walks only)."""

from __future__ import annotations

from datetime import date, time, timedelta

import pandas as pd
import pytest

from app.mcx_futures.config import CRUDEOILM, NATGASMINI
from app.mcx_futures.costs import DEFAULT_SCHEDULE
from app.mcx_futures.replay import ReplayInputError, replay, spread_sensitivity
from app.mcx_futures.research import DEFAULT_GRID, INSUFFICIENT_DATA, ResearchPlan, grid_configs, run_research
from app.mcx_futures.session import SessionCalendar
from tests.test_mcx_futures_support import random_walk_bars, spec_for, synth_spec, trading_days, trend_bars

CRUDE_SPEC = spec_for("CRUDEOILM")
TREND = trend_bars(date(2026, 11, 24), start_price=5600, slope=0.6, tick=1.0,
                   minutes=600, wiggle=2.0).assign(bid_qty=1000.0, ask_qty=1000.0)
# SYNTHETIC short evening sessions keep the long-series research test fast.
SYNTH_CAL = SessionCalendar(source="SYNTHETIC_TEST_CALENDAR", open_time=time(20, 0), default_close=time(22, 30),
                            seasonal_closes=())


def test_replay_has_no_lookahead() -> None:
    full = replay(TREND, CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE, collect_events=True)
    for cut in (170, 340, 455):
        partial = replay(TREND.iloc[:cut], CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE, collect_events=True)
        # Events produced from the first `cut` bars never depend on later bars.
        assert partial["events"] == full["events"][:len(partial["events"])]
    assert full["metrics"]["trades"] >= 1


def test_replay_without_depth_cannot_claim_executable_fill() -> None:
    no_depth = replay(TREND.drop(columns=["bid_qty", "ask_qty"]), CRUDEOILM,
                      CRUDE_SPEC, DEFAULT_SCHEDULE)
    assert no_depth["metrics"]["trades"] == 0
    assert no_depth["open_position_at_end"] is None
    assert no_depth["assumptions"]["depth"].startswith("explicit")


def _with_extra_bar(base: pd.DataFrame, **bar: float) -> pd.DataFrame:
    ts = base["ts"].iloc[-1] + timedelta(minutes=1)
    return pd.concat([base, pd.DataFrame([{"ts": ts, "volume": 100.0,
                                           "bid_qty": 1000.0, "ask_qty": 1000.0, **bar}])], ignore_index=True)


def test_intrabar_stop_rule_crossing_and_gap_through() -> None:
    head = TREND.iloc[:200]
    opened = replay(head, CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE)
    position = opened["open_position_at_end"]
    assert position is not None and position["direction"] == "LONG"
    stop, last = position["stop"], float(head["close"].iloc[-1])
    crossing = _with_extra_bar(head, open=last, high=last, low=stop - 30, close=stop - 25)
    trade = replay(crossing, CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE)["trades"][-1]
    assert trade["exit_reason"] in ("STOP", "TRAILING_STOP") and trade["exit_price"] == stop - 1  # stop + 1 tick slip
    gapped = _with_extra_bar(head, open=stop - 30, high=stop - 28, low=stop - 35, close=stop - 31)
    trade = replay(gapped, CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE)["trades"][-1]
    assert trade["exit_price"] == stop - 30 - 1 - 1  # open bid (half spread 1 tick) - 1 tick slippage


def test_replay_input_validation() -> None:
    naive = TREND.iloc[:5].copy()
    naive["ts"] = naive["ts"].apply(lambda value: value.replace(tzinfo=None))
    with pytest.raises(ReplayInputError, match="TZ_AWARE"):
        replay(naive, CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE)
    with pytest.raises(ReplayInputError, match="STRICTLY_INCREASING"):
        replay(pd.concat([TREND.iloc[:3], TREND.iloc[2:3]]), CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE)
    broken = TREND.iloc[:3].copy()
    broken.loc[1, "high"] = broken.loc[1, "low"] - 1
    with pytest.raises(ReplayInputError, match="OHLC_INCONSISTENT"):
        replay(broken, CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE)
    with pytest.raises(ReplayInputError, match="MISSING_COLUMNS"):
        replay(TREND.drop(columns=["volume"]), CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE)


def test_spread_sensitivity_scenarios_and_metrics_shape() -> None:
    result = spread_sensitivity(TREND.iloc[:400], CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE)
    assert set(result) == {"0.5", "1.0", "2.0"}
    for item in result.values():
        for key in ("trades", "win_rate", "expectancy", "avg_win", "avg_loss", "profit_factor", "max_drawdown",
                    "max_consecutive_losses", "avg_holding_minutes", "avg_mae_rupees", "avg_mfe_rupees",
                    "cost_totals", "worst_day"):
            assert key in item


def test_default_grid_is_pre_declared_and_registered() -> None:
    configs = grid_configs(CRUDEOILM, DEFAULT_GRID)
    assert len(configs) == 4 * 3 * 2 == 24
    assert len({cfg.config_hash for _, cfg in configs}) == 24
    assert all(cfg.calibration_status == "PRE_REGISTERED_UNVALIDATED" for _, cfg in configs)


def test_research_returns_insufficient_data_on_short_or_gappy_input() -> None:
    short = random_walk_bars(trading_days(date(2027, 4, 5), 3), start_price=5600, step=1.0, tick=1.0, seed=1,
                             open_time=time(20, 0), minutes=150)
    result = run_research(short, CRUDEOILM, synth_spec("CRUDEOILM"), DEFAULT_SCHEDULE, calendar=SYNTH_CAL)
    assert result["status"] == INSUFFICIENT_DATA and result["evaluated_config_count"] == 0
    assert any(reason.startswith("TOO_FEW_SESSIONS") for reason in result["data_reasons"])
    assert "selected" not in result and "walk_forward" not in result
    days = trading_days(date(2027, 4, 5), 22)
    gappy = random_walk_bars(days, start_price=5600, step=1.0, tick=1.0, seed=1, open_time=time(20, 0), minutes=150)
    gappy = gappy.drop(index=range(200, 260)).reset_index(drop=True)  # 60 missing minutes in session 2
    result = run_research(gappy, CRUDEOILM, synth_spec("CRUDEOILM"), DEFAULT_SCHEDULE, calendar=SYNTH_CAL,
                          plan=ResearchPlan(min_sessions=20))
    assert result["status"] == INSUFFICIENT_DATA
    assert any(reason.startswith("MISSING_BARS") for reason in result["data_reasons"])


def test_research_end_to_end_random_walk_is_not_a_credible_edge() -> None:
    days = trading_days(date(2027, 4, 5), 22)
    walk = random_walk_bars(days, start_price=5600, step=1.2, tick=1.0, seed=20261124, open_time=time(20, 0),
                            minutes=150)
    grid = {"entry_score_floor": (55.0, 70.0), "stop_atr_multiple": (1.5,), "feature_timeframe_minutes": (3, 5)}
    result = run_research(walk, CRUDEOILM, synth_spec("CRUDEOILM"), DEFAULT_SCHEDULE, grid=grid,
                          plan=ResearchPlan(min_sessions=20, min_train_trades=3), calendar=SYNTH_CAL)
    assert result["status"] == "OK"
    assert result["evaluated_config_count"] == 4 == len(result["registry"])
    assert all("selection" in row and "oos" in row for row in result["registry"])
    assert len(result["walk_forward"]) == 4
    assert set(result["baselines"]) == {"no_trade", "ema_cross"}
    assert result["baselines"]["no_trade"]["oos"]["net_pnl"] == 0
    # A random walk must not come out as a credible edge.
    assert result["verdict"] == "NO_CREDIBLE_EDGE"
    assert result["failed_checks"]
    if result.get("selected"):
        assert result["selected"]["oos"]["trades"] < 30 or result["selected"]["oos"]["net_pnl"] <= 0 \
            or "OOS_T_STAT" in result["failed_checks"]


def test_natgas_replay_runs_with_its_own_config() -> None:
    bars = trend_bars(date(2026, 11, 24), start_price=310, slope=0.03, tick=0.1, minutes=600, wiggle=0.12)
    result = replay(bars, NATGASMINI, spec_for("NATGASMINI"), DEFAULT_SCHEDULE)
    assert result["config_id"].startswith("ACTS_V3_MCX_FUTURES:NATGASMINI")
    for trade in result["trades"]:
        assert trade["lot_size"] == 250
        assert trade["gross_pnl"] == pytest.approx(
            (trade["exit_price"] - trade["entry_price"]) * (1 if trade["direction"] == "LONG" else -1) * 250)
