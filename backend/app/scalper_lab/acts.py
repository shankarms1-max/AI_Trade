"""Frozen ACTS_V1 thesis over causal Phase 15 and persisted Phase 14.1 evidence."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.alpha.engine import joint_direction
from app.alpha.models import (ALPHA_VERSION, AlphaFeatureSnapshot,
                              AlphaHypothesis, AlphaValidity, CalculationMode,
                              JointAlphaDirection)

STRATEGY_ID = "ACTS_V1"
PARAMETERS: dict[str, int | float | str] = {
    "futures_5m_bps": 4, "futures_15m_bps": 8,
    "bull_range_position": .55, "bear_range_position": .45,
    "alpha_max_age_seconds": 180, "positioning_min_checks": 2,
    "opposite_positioning_veto_checks": 3,
    "pullback_min_seconds": 60, "resumption_points": 3,
    "structure_break_points": 10, "wall_unwind_fraction": .10,
    "trend_reversal_seconds": 60, "alpha_reversal_seconds": 60,
    "positioning_reversal_seconds": 180, "structure_break_seconds": 60,
    "max_confirmation_gap_seconds": 30,
    "profit_capture_pct": 50, "time_stop_minutes": 30,
    "max_entries_per_day": 3, "alpha_version": ALPHA_VERSION,
    "alpha_mode": CalculationMode.LIVE_ORIGINAL.value,
}
INVALIDATIONS = ["SPREAD_STOP", "HARD_MAX_LOSS", "FORCED_CLOSE",
                 "KILL_SWITCH", "MARKET_EVENT_EXIT", "SUSTAINED_TREND_REVERSAL",
                 "SUSTAINED_STRONG_ALPHA_REVERSAL", "SUSTAINED_OI_POSITIONING_REVERSAL",
                 "SUSTAINED_STRUCTURE_BREAK", "PROFIT_CAPTURE", "TRAILING_EXIT",
                 "TIME_STOP"]

ALPHA_STATES = {
    JointAlphaDirection.STRONG_BULLISH_CONFIRMATION: "STRONG_BULL",
    JointAlphaDirection.BULLISH_CONFIRMATION: "BULL",
    JointAlphaDirection.NEUTRAL: "NEUTRAL",
    JointAlphaDirection.CONFLICT: "NEUTRAL",
    JointAlphaDirection.BEARISH_CONFIRMATION: "BEAR",
    JointAlphaDirection.STRONG_BEARISH_CONFIRMATION: "STRONG_BEAR",
}


def _known_before(value: datetime | None, at: datetime) -> bool:
    return bool(value is not None and value.tzinfo is not None and value <= at)


def alpha_evidence(alpha: AlphaFeatureSnapshot | None, at: datetime,
                   future_id: str | None, future_expiry: Any,
                   option_expiry: Any) -> dict[str, Any]:
    """Use original persisted Alpha directions; never derive a proxy from scalper data."""
    result: dict[str, Any] = {"status": "ALPHA_UNAVAILABLE", "alpha_1": None,
                              "alpha_2": None, "classification": "ALPHA_UNAVAILABLE",
                              "signed_log_return": None,
                              "standardized_return": None,
                              "alpha_1_direction": None, "alpha_2_direction": None,
                              "joint_direction": None, "source_timestamp": None,
                              "reason": "ORIGINAL_CAUSAL_ALPHA_REQUIRED"}
    if alpha is None:
        return result
    if alpha.timestamp.tzinfo is None:
        result["reason"] = "ALPHA_TIMESTAMP_NOT_AWARE"
        return result
    result.update(alpha_1=alpha.alpha_1, alpha_2=alpha.alpha_2,
                  signed_log_return=alpha.signed_log_return,
                  standardized_return=alpha.standardized_return,
                  alpha_1_direction=alpha.alpha_1_direction.value,
                  alpha_2_direction=alpha.alpha_2_direction.value,
                  joint_direction=alpha.joint_alpha_direction.value,
                  source_timestamp=alpha.timestamp.isoformat())
    calculated = alpha.feature_calculated_at
    age = (at - alpha.timestamp).total_seconds()
    valid = (alpha.alpha_version == ALPHA_VERSION
             and alpha.calculation_mode == CalculationMode.LIVE_ORIGINAL
             and alpha.hypothesis_type == AlphaHypothesis.CONTINUATION
             and alpha.validity_state == AlphaValidity.VALID
             and alpha.price_source == "FUTURE"
             and alpha.alpha_1 is not None and alpha.alpha_2 is not None
             and alpha.reference_instrument_id == future_id
             and alpha.reference_expiry == future_expiry
             and alpha.expiry == option_expiry
             and 0 <= age <= int(PARAMETERS["alpha_max_age_seconds"])
             and _known_before(calculated, at)
             and (alpha.response_received_at is None
                  or _known_before(alpha.response_received_at, at))
             and (alpha.source_market_timestamp is None
                  or _known_before(alpha.source_market_timestamp, at))
             and (alpha.reference_source_timestamp is None
                  or _known_before(alpha.reference_source_timestamp, at))
             and (joint_direction(alpha.alpha_1_direction, alpha.alpha_2_direction)
                  == alpha.joint_alpha_direction))
    if not valid:
        result["reason"] = "ALPHA_IDENTITY_VALIDITY_OR_CAUSAL_TIME_FAILED"
        return result
    classification = ALPHA_STATES.get(alpha.joint_alpha_direction)
    if classification is None:
        result["reason"] = "ALPHA_JOINT_DIRECTION_UNAVAILABLE"
        return result
    result.update(status="AVAILABLE", classification=classification, reason=None)
    return result


def alpha_state(classification: str, direction: str) -> str:
    if classification == "ALPHA_UNAVAILABLE":
        return "UNAVAILABLE"
    own = "BULL" if direction == "BULL" else "BEAR"
    opposite = "BEAR" if direction == "BULL" else "BULL"
    if classification == f"STRONG_{opposite}":
        return "VETO"
    if classification == opposite:
        return "CONTRADICTORY"
    if classification == f"STRONG_{own}":
        return "STRONG_SUPPORTIVE"
    if classification == own:
        return "SUPPORTIVE"
    return "NEUTRAL"


def relative_alpha_state(alpha: dict[str, Any], direction: str) -> str:
    if alpha["status"] != "AVAILABLE":
        return "UNAVAILABLE"
    opposite = "BEARISH" if direction == "BULL" else "BULLISH"
    components = (alpha["alpha_1_direction"], alpha["alpha_2_direction"])
    if f"STRONG_{opposite}" in components:
        return "VETO"
    if opposite in components:
        return "CONTRADICTORY"
    return alpha_state(alpha["classification"], direction)


def regime(ctx: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    returns = ctx["futures_returns_bps"]
    structure = ctx["structure"]
    position = structure["range_position"]
    high, low = ctx["opening_high"], ctx["opening_low"]
    midpoint = (high + low) / 2 if high is not None and low is not None else None
    evidence = {"futures_5m_bps": returns.get("5m"),
                "futures_15m_bps": returns.get("15m"),
                "range_position": position, "opening_midpoint": midpoint,
                "opening_midpoint_relation": (
                    "ABOVE" if midpoint is not None and ctx["spot"] > midpoint else
                    "BELOW" if midpoint is not None and ctx["spot"] < midpoint else
                    "AT" if midpoint is not None else "UNAVAILABLE"),
                "spot": ctx["spot"], "vwap": ctx["vwap"]}
    if not structure["opening_range_complete"] or midpoint is None:
        return None, evidence
    for direction, sign, boundary in (("BULL", 1, .55), ("BEAR", -1, .45)):
        five, fifteen = returns.get("5m"), returns.get("15m")
        aligned = (five is not None and fifteen is not None and position is not None
                   and sign * five >= 4 and sign * fifteen >= 8
                   and (position >= boundary if sign > 0 else position <= boundary)
                   and sign * (ctx["spot"] - midpoint) > 0)
        vwap = ctx["vwap"]
        if vwap["status"] == "AVAILABLE":
            slope = vwap["slope_bps_per_minute"]
            aligned = (aligned and ctx["future"] is not None and slope is not None
                       and sign * (ctx["future"] - vwap["value"]) > 0
                       and sign * slope >= 0)
        if aligned:
            return direction, evidence
    return None, evidence


def _positive(value: Any) -> bool:
    return value is not None and value > 0


def _negative(value: Any) -> bool:
    return value is not None and value < 0


def positioning(ctx: dict[str, Any], direction: str) -> dict[str, Any]:
    options = ctx["options"]
    relevant = options["walls"]["put_support" if direction == "BULL" else "call_resistance"]
    opposing = options["walls"]["call_resistance" if direction == "BULL" else "put_support"]
    sign = 1 if direction == "BULL" else -1
    pcr = options["local_pcr"]
    slopes = {radius: pcr[radius]["oi_slope_per_minute"] for radius in ("3", "5")}
    checks = {
        "A": bool(relevant and any(_positive(relevant["cluster_delta_oi"][h])
                                   for h in ("3m", "5m"))),
        "B": bool(relevant and any(relevant["activity"][h] == "WRITING"
                                   for h in ("3m", "5m"))),
        "C": bool(opposing and any(_negative(opposing["cluster_delta_oi"][h])
                                   for h in ("3m", "5m"))),
        "D": any(value is not None and sign * value > 0
                 for radius in slopes.values() for h, value in radius.items()
                 if h in {"3m", "5m"}),
    }
    opposite = {
        "A": bool(relevant and any(_negative(relevant["cluster_delta_oi"][h])
                                   for h in ("3m", "5m"))),
        "B": bool(relevant and any(relevant["activity"][h] in
                                   {"LONG_BUILDUP", "SHORT_COVERING"}
                                   for h in ("3m", "5m"))),
        "C": bool(opposing and any(_positive(opposing["cluster_delta_oi"][h])
                                   for h in ("3m", "5m"))),
        "D": any(value is not None and sign * value < 0
                 for radius in slopes.values() for h, value in radius.items()
                 if h in {"3m", "5m"}),
    }
    return {"checks": checks, "passed": [name for name, passed in checks.items() if passed],
            "opposite_checks": opposite,
            "opposite_veto": sum(opposite.values()) >= 3,
            "qualified": sum(checks.values()) >= 2 and sum(opposite.values()) < 3,
            "relevant_wall": relevant, "opposing_wall": opposing,
            "local_pcr": pcr,
            "reference_timestamps": {h: options["reference_timestamps"][h]
                                     for h in ("3m", "5m")}}


def evidence(ctx: dict[str, Any], alpha: AlphaFeatureSnapshot | None) -> dict[str, Any]:
    at = datetime.fromisoformat(ctx["timestamp"])
    alpha_info = alpha_evidence(alpha, at, ctx.get("future_instrument_id"),
                                ctx.get("future_expiry"), ctx.get("expiry_date"))
    direction, regime_info = regime(ctx)
    return {"alpha": alpha_info, "direction": direction, "regime": regime_info,
            "positioning": positioning(ctx, direction) if direction else None}


def decide(ctx: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    data = ctx["acts"]
    direction = data["direction"]
    alpha = data["alpha"]
    position = data["positioning"]
    relation = relative_alpha_state(alpha, direction) if direction else "UNAVAILABLE"
    blockers: list[str] = []
    if direction is None:
        blockers.append("ACTS_DIRECTIONAL_REGIME_REQUIRED")
    if relation == "UNAVAILABLE":
        blockers.append("ALPHA_UNAVAILABLE")
    elif relation == "VETO":
        blockers.append("STRONG_OPPOSITE_ALPHA_VETO")
    if direction is not None and (position is None or not position["qualified"]):
        blockers.append("POSITIONING_TWO_OF_FOUR_REQUIRED")
        if position is not None and position["opposite_veto"]:
            blockers.append("STRONG_OPPOSITE_POSITIONING_VETO")
    at = datetime.fromisoformat(ctx["timestamp"])
    status, trigger = "NO_SETUP", None
    if blockers:
        state.clear()
    else:
        sign = 1 if direction == "BULL" else -1
        spot = ctx["spot"]
        previous_at = state.get("last_observed_at")
        if (previous_at is not None
                and (at - datetime.fromisoformat(previous_at)).total_seconds() > 30):
            state.clear()
        if state.get("direction") != direction:
            state.clear()
        if not state:
            previous = ctx["previous_spot"]
            if (previous is not None and sign * (spot - previous) < -.5
                    and ctx["fast_direction"] != direction):
                state.update(direction=direction, started_at=ctx["timestamp"],
                             last_countertrend_at=ctx["timestamp"],
                             last_observed_at=ctx["timestamp"],
                             anchor=spot, stage="PULLBACK")
                status = "WATCH"
            else:
                blockers.append("COUNTERTREND_PULLBACK_NOT_OBSERVED")
        else:
            state["last_observed_at"] = ctx["timestamp"]
            if sign * (spot - state["anchor"]) < 0:
                state["anchor"] = spot
            if ctx["fast_direction"] != direction and ctx["fast_direction"] != "NEUTRAL":
                state["last_countertrend_at"] = ctx["timestamp"]
            duration = (at - datetime.fromisoformat(state["started_at"])).total_seconds()
            persisted = (datetime.fromisoformat(state["last_countertrend_at"])
                         - datetime.fromisoformat(state["started_at"])).total_seconds()
            state["duration_seconds"] = duration
            state["pullback_persisted_seconds"] = persisted
            if (persisted >= 60 and ctx["fast_direction"] == direction
                    and sign * (spot - state["anchor"]) >= 3):
                status, trigger = "ENTER", "PULLBACK_60S_FAST_3_POINT_RESUMPTION"
            else:
                status = "WATCH"
    thesis = None
    if status == "ENTER":
        thesis = {"strategy_id": STRATEGY_ID, "strategy_version": STRATEGY_ID,
                  "direction": direction, "regime": data["regime"],
                  "vwap": ctx["vwap"], "alpha": alpha, "alpha_state": relation,
                  "positioning": position, "positioning_checks": position["checks"],
                  "local_pcr": position["local_pcr"],
                  "wall": position["relevant_wall"],
                  "opening_high": ctx["opening_high"],
                  "opening_low": ctx["opening_low"],
                  "pullback_start": state["started_at"],
                  "pullback_duration_seconds": state["duration_seconds"],
                  "pullback_persisted_seconds": state["pullback_persisted_seconds"],
                  "pullback_anchor": state["anchor"], "timing_trigger": trigger,
                  "invalidation_conditions": INVALIDATIONS,
                  "parameters": PARAMETERS.copy()}
        state.clear()
    return {"strategy_id": STRATEGY_ID, "timestamp": ctx["timestamp"],
            "status": status, "direction": direction, "trigger": trigger,
            "regime": direction or "RANGE", "vwap": ctx["vwap"],
            "alpha": alpha, "alpha_state": relation,
            "positioning_checks": position["checks"] if position else {},
            "positioning": position, "watch_state": dict(state),
            "blockers": blockers, "thesis": thesis}


def entry_valid(ctx: dict[str, Any], direction: str) -> str | None:
    data = ctx["acts"]
    if data["direction"] != direction:
        return "PENDING_ACTS_REGIME_REVERSED"
    if relative_alpha_state(data["alpha"], direction) in {"UNAVAILABLE", "VETO"}:
        return "PENDING_ACTS_ALPHA_LOST_OR_VETOED"
    if data["positioning"] is None or not data["positioning"]["qualified"]:
        return "PENDING_ACTS_POSITIONING_LOST"
    return None


def thesis_exit(trade: Any, ctx: dict[str, Any]) -> str | None:
    direction = trade.decision["direction"]
    sign = 1 if direction == "BULL" else -1
    at = datetime.fromisoformat(ctx["timestamp"])
    returns = ctx["futures_returns_bps"]
    five, fifteen = returns.get("5m"), returns.get("15m")
    alpha = ctx["acts"]["alpha"]
    held = trade.decision["thesis"].get("wall")
    current = next((row for row in ctx["options"]["contracts"]
                    if held and row["identity"] == held["identity"]), None)
    opposing = ctx["options"]["walls"]["call_resistance" if sign > 0 else "put_support"]
    unwind = False
    if current:
        for horizon in ("3m", "5m"):
            delta = current["delta_oi"][horizon]
            previous = current["open_interest"] - delta if delta is not None else None
            if previous and delta < 0 and -delta / previous >= .10:
                unwind = True
    adverse_activity = bool(current and all(
        current["activity"][horizon] in {"LONG_BUILDUP", "SHORT_COVERING"}
        for horizon in ("3m", "5m")))
    opposing_strength = bool(opposing and all(
        opposing["cluster_delta_oi"][horizon] is not None
        and opposing["cluster_delta_oi"][horizon] > 0
        for horizon in ("3m", "5m")))
    break_price = bool(held and ctx["future"] is not None
                       and sign * (ctx["spot"] - held["strike"]) < -10
                       and sign * (ctx["future"] - held["strike"]) < -10)
    conditions = (
        ("SUSTAINED_TREND_REVERSAL", five is not None and fifteen is not None
         and sign * five <= -4 and sign * fifteen <= -8, 60),
        ("SUSTAINED_STRONG_ALPHA_REVERSAL",
         relative_alpha_state(alpha, direction) == "VETO", 60),
        ("SUSTAINED_OI_POSITIONING_REVERSAL",
         sum((unwind, adverse_activity, opposing_strength)) >= 2, 180),
        ("SUSTAINED_STRUCTURE_BREAK", break_price, 60),
    )
    for reason, adverse, seconds in conditions:
        if not adverse:
            trade.acts_adverse_since.pop(reason, None)
            trade.acts_last_adverse_at.pop(reason, None)
            continue
        last = trade.acts_last_adverse_at.get(reason)
        if last is not None and (at - last).total_seconds() > 30:
            trade.acts_adverse_since.pop(reason, None)
        trade.acts_last_adverse_at[reason] = at
        since = trade.acts_adverse_since.setdefault(reason, at)
        if (at - since).total_seconds() >= seconds:
            return reason
    return None
