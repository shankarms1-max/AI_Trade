"""Phase 15 MIRROR_EXIT_V1: causal signals and diagnostics, never order execution."""
from __future__ import annotations

from datetime import datetime, time
from math import isfinite
from typing import Any

from app.acts_paper import acts_v2
from app.parallel_paper.bars import Bar, MarketPoint, contiguous_bars, session_start
from app.parallel_paper.config import ExperimentConfig
from app.parallel_paper.decisions import decide
from app.scalper.config import ScalperConfig
from app.scalper.repository import IST
from app.scalper_lab import acts

VERSION = "MIRROR_EXIT_V1"
ACTS_REASON = "SUSTAINED_OPPOSITE_ACTS_TRADEABLE_THESIS"


def policy(config: ScalperConfig) -> dict[str, Any]:
    if config.forced_exit_time != time(15, 20):
        raise ValueError("MIRROR_EXIT_REQUIRES_1520_IST_FORCED_CLOSE")
    return {"version": VERSION, "forced_exit_time_ist": "15:20:00",
            "precedence": ["FORCED_CLOSE", "EMERGENCY", "STOP_LOSS", "MIRROR_SIGNAL", "HOLD"],
            "legacy_profit_trailing_time_exits": "DIAGNOSTICS_ONLY",
            "acts_confirmation_seconds": 60, "acts_max_gap_seconds": 30,
            "acts_execution_score": "NO_CANDIDATE_ZERO_POINTS_NO_RESCALING",
            "signal_book_unavailable": "PERSIST_EXIT_REQUEST_UNTIL_EXECUTABLE_OR_UNRESOLVED",
            "exit_price": "EXACT_SHORT_ASK_MINUS_HEDGE_BID"}


def safety_reason(doc: dict, at: datetime, debit: float, config: ScalperConfig,
                  *, event_blocked: bool) -> str | None:
    """Retain the legacy hard exits and their ordering, with no normal exits."""
    if at.astimezone(IST).time().replace(tzinfo=None) >= config.forced_exit_time:
        return "FORCED_CLOSE"
    if config.kill_switch or event_blocked:
        return "KILL_SWITCH" if config.kill_switch else "MARKET_EVENT_EXIT"
    if debit >= doc["entry_credit"] * config.stop_credit_multiple:
        return "SPREAD_STOP"
    if (debit - doc["entry_credit"]) * doc["lot_size"] * config.lots >= config.max_loss_per_trade:
        return "HARD_MAX_LOSS"
    return None


def first(doc: dict, name: str, at: datetime, *, available: bool, **evidence: Any) -> None:
    diagnostics = doc.setdefault("mirror_diagnostics", {})
    if name in diagnostics:
        return
    diagnostics[name] = at.isoformat()
    diagnostics.setdefault("evidence", {})[name] = {
        "observed_at": at.isoformat(), "mark_available": available,
        "gross_points": doc.get("gross_mark_points") if available else None,
        "gross_pnl": doc.get("gross_mark_rupees") if available else None,
        "mfe": doc.get("mfe"), "mae": doc.get("mae"),
        "best_gross_points": doc.get("best_gross_points"), **evidence}


def legacy_diagnostics(doc: dict, at: datetime, debit: float, config: ScalperConfig,
                       *, acts_strategy: bool = False) -> None:
    credit, best = doc["entry_credit"], doc["best_gross_points"]
    profit_pct = float(acts.PARAMETERS["profit_capture_pct"]) if acts_strategy else config.profit_capture_pct
    minutes = int(acts.PARAMETERS["time_stop_minutes"]) if acts_strategy else config.time_stop_minutes
    if debit <= credit * (1 - profit_pct / 100):
        first(doc, "old_profit_capture_first_at", at, available=True)
    if (best >= credit * config.trailing_activation_pct / 100
            and credit - debit <= best - credit * config.trailing_giveback_pct / 100):
        first(doc, "old_trailing_exit_first_at", at, available=True)
    if (at - datetime.fromisoformat(doc["entry_timestamp"])).total_seconds() >= minutes * 60:
        first(doc, "old_time_stop_at", at, available=True)


def _requested(doc: dict, reason: str | None, at: datetime, signal: dict) -> str | None:
    if reason is not None:
        doc.setdefault("mirror_exit_request", {"reason": reason, "observed_at": at.isoformat(),
                                               "signal": signal})
    return (doc.get("mirror_exit_request") or {}).get("reason")


def parallel_signal(doc: dict, points: list[MarketPoint], bars: list[Bar], at: datetime,
                    config: ExperimentConfig, *, available: bool) -> str | None:
    if any(point.at > at for point in points) or any(bar.end > at for bar in bars):
        raise ValueError("PARALLEL_FUTURE_OBSERVATION")
    strategy, direction = doc["strategy_id"], doc["direction"]
    state = doc.setdefault("mirror_exit_state", {})
    current = points[-1] if points else None
    same_future = bool(current and current.at == at and current.price is not None
                       and isfinite(current.price) and current.price > 0
                       and (current.future_id, str(current.future_expiry)) ==
                       (doc["future_instrument_id"], doc["future_expiry"]))
    same_session = session_start(at) == session_start(datetime.fromisoformat(doc["decision_timestamp"]))
    if not same_future or not same_session:
        state.clear()  # Missing/changed evidence cannot sustain a confirmation timer.
        doc["mirror_exit_latest"] = {"timestamp": at.isoformat(), "status": "UNAVAILABLE",
                                     "reason": "ORIGINAL_SESSION_FUTURES_REQUIRED"}
        return _requested(doc, None, at, {})
    usable = contiguous_bars(bars)
    if not strategy.startswith("ORB_") and (
            not usable or usable[-1].end <= datetime.fromisoformat(doc["entry_timestamp"])):
        return _requested(doc, None, at, {})
    decision = decide(strategy, points, bars, at, state, config,
                      frozen_range=doc["strategy_features"] if strategy.startswith("ORB_") else None)
    doc["mirror_exit_latest"] = decision
    features, opposite = decision["features"], "BEAR" if direction == "BULL" else "BULL"
    sign = 1 if direction == "BULL" else -1
    reason = None
    full = decision["status"] == "ENTER" and decision["direction"] == opposite
    if strategy.startswith("ORB_"):
        original = doc["strategy_features"]
        high, low = original["opening_high"], original["opening_low"]
        assert current is not None and current.price is not None
        for name, crossed in (
            ("same_boundary_reentry_first_at", sign * (current.price - (high if sign > 0 else low)) <= 0),
            ("opening_midpoint_cross_first_at", sign * (current.price - (high + low) / 2) <= 0),
            ("opposite_breakout_first_at", current.price < low if sign > 0 else current.price > high),
        ):
            if crossed:
                first(doc, name, at, available=available, future=current.price,
                      opening_high=high, opening_low=low)
        if full:
            first(doc, "opposite_breakout_confirmed_at", at, available=available, features=features)
            reason = "OPPOSITE_ORB_BREAKOUT"
    elif strategy == "EMA_PULLBACK":
        fast, slow = features.get("ema_fast"), features.get("ema_slow")
        if fast is not None and slow is not None and sign * (fast - slow) < 0:
            first(doc, "adverse_ema_cross_first_at", at, available=available, features=features)
        anchor = doc["strategy_features"].get("pullback_anchor")
        if anchor is not None and sign * (usable[-1].close - anchor) < 0:
            first(doc, "pullback_anchor_break_first_at", at, available=available,
                  original_anchor=anchor, bar_end=usable[-1].end.isoformat(), close=usable[-1].close)
        if decision["direction"] == opposite and decision["status"] == "WATCH":
            first(doc, "opposite_setup_watch_first_at", at, available=available, features=features)
        if full:
            first(doc, "opposite_entry_confirmed_at", at, available=available, features=features)
            reason = "OPPOSITE_EMA_PULLBACK_RESUMPTION"
    else:
        if features.get("supertrend_direction") == opposite:
            first(doc, "opposite_supertrend_first_at", at, available=available, features=features)
            value = features.get("rsi")
            if value is not None and (value <= config.rsi_bear if sign > 0 else value >= config.rsi_bull):
                first(doc, "opposite_supertrend_rsi_first_at", at, available=available, features=features)
        if full:
            first(doc, "opposite_full_signal_first_at", at, available=available, features=features)
            reason = "OPPOSITE_SUPERTREND_RSI_VOLUME_SIGNAL"
    return _requested(doc, reason, at, decision)


def acts_signal(doc: dict, trade: Any, ctx: dict, config: acts_v2.ActsV2Config,
                *, available: bool) -> str | None:
    at = datetime.fromisoformat(ctx["timestamp"])
    # Evaluate every legacy component separately as evidence only. The unchanged
    # legacy engine still returns its first matured invalidation normally.
    for name, adverse, seconds in acts.adverse_conditions(trade, ctx):
        if acts.sustained_condition(trade, name, adverse, at, seconds):
            first(doc, name, at, available=available,
                  futures_returns_bps=ctx["futures_returns_bps"], alpha=ctx["acts"]["alpha"],
                  structure=ctx["structure"], positioning=ctx["acts"]["positioning"])
    state = doc.setdefault("mirror_exit_state", {})
    thesis = acts_v2.directional_thesis(ctx, state.setdefault("watch", {}), config)
    doc["mirror_exit_latest"] = thesis
    same_future = (ctx.get("future") is not None and
                   (ctx.get("future_instrument_id"), str(ctx.get("future_expiry"))) ==
                   (doc["future_instrument_id"], doc["future_expiry"]))
    same_session = session_start(at) == session_start(datetime.fromisoformat(doc["entry_timestamp"]))
    opposite = thesis["direction"] != doc["direction"] and thesis["direction"] is not None
    confirmed = acts.sustained_condition(
        trade, ACTS_REASON, bool(same_future and same_session and opposite and thesis["tradeable"]), at, 60)
    if confirmed:
        first(doc, "opposite_acts_thesis_confirmed_at", at, available=available, thesis=thesis)
    return _requested(doc, ACTS_REASON if confirmed else None, at, thesis)
