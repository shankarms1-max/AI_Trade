"""Five deliberately separate entry experiments over shared causal market evidence."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.parallel_paper.bars import (
    Bar, MarketPoint, contiguous_bars, opening_range, session_start,
)
from app.parallel_paper.config import ExperimentConfig, VERSIONS
from app.parallel_paper.indicators import ema, rsi, supertrend


def _result(strategy: str, at: datetime, status: str, features: dict, *,
            direction: str | None = None, reason: str | None = None,
            blockers: list[str] | None = None) -> dict[str, Any]:
    return {"strategy_id": strategy, "strategy_version": VERSIONS[strategy],
            "timestamp": at.isoformat(), "status": status,
            "direction": direction, "entry_reason": reason,
            "features": features, "blockers": blockers or []}


def decide_orb(strategy: str, points: list[MarketPoint], at: datetime,
               state: dict, config: ExperimentConfig) -> dict:
    features = opening_range(points, at, int(strategy.split("_")[1]), config)
    if not features["complete"]:
        return _result(strategy, at, "NO_SETUP", features, blockers=[features["reason"]])
    price = points[-1].price
    if price is None:
        return _result(strategy, at, "NO_SETUP", features, blockers=["FUTURES_UNAVAILABLE"])
    direction = ("BULL" if price > features["opening_high"] else
                 "BEAR" if price < features["opening_low"] else None)
    previous = state.get("breakout")
    if direction is None:
        if previous:
            features["false_breakout"] = {**previous, "returned_inside_at": at.isoformat()}
        state.pop("breakout", None)
        return _result(strategy, at, "NO_SETUP", features, blockers=["PRICE_INSIDE_RANGE"])
    if (not previous or previous["direction"] != direction or
            (at - datetime.fromisoformat(previous["last_at"])).total_seconds()
            > config.max_sequence_gap_seconds):
        previous = {"direction": direction, "started_at": at.isoformat()}
    previous["last_at"] = at.isoformat()
    state["breakout"] = previous
    boundary = features["opening_high"] if direction == "BULL" else features["opening_low"]
    features.update(breakout_direction=direction, breakout_timestamp=previous["started_at"],
                    breakout_distance_bps=abs(price / boundary - 1) * 10000,
                    confirmation_seconds=(at - datetime.fromisoformat(
                        previous["started_at"])).total_seconds())
    used = state.get("used_directions", {})
    if (direction in used and (
            not config.orb_allow_reentry or used[direction] == previous["started_at"])):
        return _result(strategy, at, "NO_SETUP", features, direction=direction,
                       blockers=["ORB_REENTRY_DISABLED_OR_SAME_BREAKOUT"])
    if features["confirmation_seconds"] < config.orb_confirmation_seconds:
        return _result(strategy, at, "WATCH", features, direction=direction)
    return _result(strategy, at, "ENTER", features, direction=direction,
                   reason="CONFIRMED_OPENING_RANGE_BREAKOUT")


def decide_ema(bars: list[Bar], at: datetime, state: dict,
               config: ExperimentConfig) -> dict:
    strategy = "EMA_PULLBACK"
    closes = [bar.close for bar in bars]
    if len(bars) < config.ema_slow:
        return _result(strategy, at, "NO_SETUP", {"completed_bars": len(bars)},
                       blockers=["EMA_WARMUP_REQUIRED"])
    bar = bars[-1]
    fast, slow = ema(closes, config.ema_fast)[-1], ema(closes, config.ema_slow)[-1]
    assert fast is not None and slow is not None
    direction = "BULL" if fast > slow else "BEAR" if fast < slow else None
    features = {"ema_fast_period": config.ema_fast, "ema_slow_period": config.ema_slow,
                "ema_fast": fast, "ema_slow": slow, "ema_spread": fast - slow,
                "bar_end": bar.end.isoformat(), "close": bar.close}
    if state.get("last_bar_end") == bar.end.isoformat():
        return _result(strategy, at, "NO_SETUP", features, direction=direction,
                       blockers=["BAR_ALREADY_EVALUATED"])
    state["last_bar_end"] = bar.end.isoformat()
    setup = state.get("setup")
    if setup and (setup["direction"] != direction or
                  (bar.end - datetime.fromisoformat(setup["started_at"])).total_seconds()
                  > config.ema_setup_lifetime_bars * config.bar_minutes * 60):
        state.pop("setup", None)
        setup = None
    if direction is None:
        return _result(strategy, at, "NO_SETUP", features, blockers=["EMA_TREND_NEUTRAL"])
    sign = 1 if direction == "BULL" else -1
    strength = sign * (bar.close / closes[-2] - 1) * 10000
    if setup:
        setup["anchor"] = min(setup["anchor"], bar.low) if sign > 0 else max(
            setup["anchor"], bar.high)
        features.update(pullback_depth_bps=abs(fast / setup["anchor"] - 1) * 10000,
                        pullback_anchor=setup["anchor"],
                        pullback_duration_seconds=(bar.end - datetime.fromisoformat(
                            setup["started_at"])).total_seconds(),
                        resumption_strength_bps=strength)
        if (strength >= config.ema_resumption_bps and sign * (bar.close - fast) > 0
                and sign * (bar.close - bar.open) > 0):
            state.pop("setup", None)
            return _result(strategy, at, "ENTER", features, direction=direction,
                           reason="EMA_ZONE_PULLBACK_RESUMPTION")
    tolerance = config.ema_zone_tolerance_bps / 10000
    zone_high, zone_low = max(fast, slow) * (1 + tolerance), min(fast, slow) * (1 - tolerance)
    touching = bar.low <= zone_high and bar.high >= zone_low
    if touching and sign * (bar.close - closes[-2]) < 0:
        if not setup:
            state["setup"] = {"direction": direction, "started_at": bar.end.isoformat(),
                              "anchor": bar.low if sign > 0 else bar.high}
        return _result(strategy, at, "WATCH", features, direction=direction)
    return _result(strategy, at, "WATCH" if setup else "NO_SETUP", features,
                   direction=direction, blockers=[] if setup else ["EMA_PULLBACK_NOT_OBSERVED"])


def decide_technical(bars: list[Bar], at: datetime, state: dict,
                     config: ExperimentConfig) -> dict:
    strategy = "SUPERTREND_RSI_VOLUME"
    features = supertrend(bars, config.atr_period, config.supertrend_multiplier)
    features.update(rsi=rsi([bar.close for bar in bars], config.rsi_period),
                    volume_source="FIXED_SESSION_ATM_CE_PE_CUMULATIVE_DELTA_V1",
                    completed_bars=len(bars))
    blockers = []
    if not bars or features["supertrend_direction"] is None or features["rsi"] is None:
        blockers.append("TECHNICAL_WARMUP_REQUIRED")
    volume_window = bars[-config.volume_baseline_bars - 1:-1]
    volume = bars[-1].volume if bars else None
    baseline = (sum(bar.volume for bar in volume_window if bar.volume is not None) / len(volume_window)
                if len(volume_window) == config.volume_baseline_bars
                and all(bar.volume is not None for bar in volume_window) else None)
    ratio = volume / baseline if volume is not None and baseline is not None and baseline > 0 else None
    features.update(volume=volume, volume_baseline=baseline, volume_ratio=ratio,
                    volume_rejection_reasons=sorted({bar.volume_reason for bar in
                                                    [*volume_window, *bars[-1:]]
                                                    if bar.volume_reason}))
    if ratio is None:
        blockers.append("VALID_OPTION_VOLUME_SERIES_REQUIRED")
    elif ratio < config.volume_min_ratio:
        blockers.append("VOLUME_CONFIRMATION_REQUIRED")
    direction = features["supertrend_direction"]
    value = features["rsi"]
    if value is not None and direction is not None:
        if config.rsi_bear < value < config.rsi_bull:
            blockers.append("RSI_NEUTRAL")
        elif not (value >= config.rsi_bull if direction == "BULL" else value <= config.rsi_bear):
            blockers.append("RSI_DIRECTION_NOT_CONFIRMED")
    if bars:
        features["bar_end"] = bars[-1].end.isoformat()
        if state.get("consumed_bar") == bars[-1].end.isoformat():
            blockers.append("BAR_ALREADY_TRADED")
    return _result(strategy, at, "NO_SETUP" if blockers else "ENTER", features,
                   direction=direction, reason=None if blockers else "SUPERTREND_RSI_VOLUME_CONFIRMED",
                   blockers=blockers)


def decide(strategy: str, points: list[MarketPoint], bars: list[Bar], at: datetime,
           state: dict, config: ExperimentConfig) -> dict:
    if any(point.at > at for point in points) or any(bar.end > at for bar in bars):
        raise ValueError("PARALLEL_FUTURE_OBSERVATION")
    session = session_start(at).date().isoformat()
    if state.get("session") != session:
        state.clear()
        state["session"] = session
    if strategy.startswith("ORB_"):
        return decide_orb(strategy, points, at, state, config)
    usable = contiguous_bars(bars)
    if strategy == "EMA_PULLBACK":
        return decide_ema(usable, at, state, config)
    return decide_technical(usable, at, state, config)


def entry_committed(strategy: str, decision: dict, state: dict) -> None:
    if strategy.startswith("ORB_"):
        state.setdefault("used_directions", {})[decision["direction"]] = (
            decision["features"]["breakout_timestamp"])
    elif strategy == "SUPERTREND_RSI_VOLUME":
        state["consumed_bar"] = decision["features"]["bar_end"]
