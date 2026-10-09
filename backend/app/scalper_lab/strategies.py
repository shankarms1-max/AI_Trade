"""Four fixed research theses; fast prices only time a known setup."""
from __future__ import annotations

from typing import Any

from app.scalper_lab import STRATEGY_IDS
from app.scalper_lab.acts import decide as decide_acts


def _sign(direction: str) -> int:
    return 1 if direction == "BULL" else -1


def _side(direction: str) -> str:
    return "put_support" if direction == "BULL" else "call_resistance"


def _watch(state: dict[str, Any], direction: str, reason: str,
           anchor: float, index: int, stage: str = "PULLBACK") -> None:
    started = (state.get("started_index", index)
               if state.get("direction") == direction and state.get("stage") == stage
               else index)
    state.update(direction=direction, reason=reason, anchor=anchor,
                 started_index=started, stage=stage)


def _clear(state: dict[str, Any]) -> None:
    state.clear()


def _result(strategy: str, status: str, ctx: dict, *, direction: str | None = None,
            trigger: str | None = None, blockers: list[str] | None = None,
            state: dict | None = None) -> dict:
    options = ctx["options"]
    wall = options["walls"].get(_side(direction)) if direction else None
    thesis = {"strategy_id": strategy, "direction": direction,
              "regime": ctx["regime"], "timing_trigger": trigger,
              "vwap": ctx["vwap"], "wall": wall,
              "local_pcr": options["local_pcr"],
              "positioning_classification": options["classification_by_direction"].get(direction),
              "short_leg_reason": "OI_WALL_ACTIVITY_PREMIUM_DISTANCE_LIQUIDITY_BOOK_QUALITY",
              "hedge_reason": "FAR_EXECUTABLE_HEDGE_WITH_DEFINED_RISK",
              "invalidation_conditions": INVALIDATIONS[strategy]}
    return {"strategy_id": strategy, "timestamp": ctx["timestamp"],
            "status": status, "direction": direction, "trigger": trigger,
            "regime": ctx["regime"], "vwap": ctx["vwap"],
            "walls": options["walls"], "local_pcr": options["local_pcr"],
            "positioning": options["classification_by_direction"],
            "delta_oi_reference": options["reference_timestamps"],
            "blockers": blockers or [], "watch_state": dict(state or {}),
            "thesis": thesis if status == "ENTER" else None}


INVALIDATIONS = {
    "VWAP_OI_REJECTION": ["SPREAD_STOP", "HARD_MAX_LOSS", "STRUCTURE_FAILURE",
                          "SUSTAINED_VWAP_RECLAIM", "OI_WALL_UNWIND",
                          "FORCED_CLOSE", "KILL_SWITCH"],
    "ORB_RETEST": ["SPREAD_STOP", "HARD_MAX_LOSS", "ORB_STRUCTURE_FAILURE",
                   "SUSTAINED_ORB_REENTRY", "FORCED_CLOSE", "KILL_SWITCH"],
    "OI_WALL": ["SPREAD_STOP", "HARD_MAX_LOSS", "WALL_STRUCTURE_FAILURE",
                "WALL_BREAK_REENTRY",
                "OI_WALL_UNWIND", "PREMIUM_OI_FLIP", "FORCED_CLOSE", "KILL_SWITCH"],
    "TREND_PULLBACK": ["SPREAD_STOP", "HARD_MAX_LOSS", "PULLBACK_STRUCTURE_FAILURE",
                       "TREND_REVERSAL", "FORCED_CLOSE", "KILL_SWITCH"],
}


def decide(strategy: str, ctx: dict, state: dict[str, Any], index: int) -> dict:
    """Update only this agent's watch state using the current causal context."""
    if strategy not in STRATEGY_IDS:
        raise ValueError("LAB_UNKNOWN_STRATEGY")
    if strategy == "ACTS_V1":
        return decide_acts(ctx, state)
    if state and index - state.get("started_index", index) > 8:
        _clear(state)
    spot = ctx["spot"]
    future = ctx["future"]
    vwap = ctx["vwap"]
    walls = ctx["options"]["walls"]
    fast = ctx["fast_direction"]
    structure = ctx["structure"]

    if strategy == "VWAP_OI_REJECTION":
        if vwap["status"] != "AVAILABLE" or vwap["slope_bps_per_minute"] is None:
            _clear(state)
            return _result(strategy, "NO_SETUP", ctx, blockers=[vwap["reason"] or
                           "VWAP_SLOPE_UNAVAILABLE"])
        direction = {"BULLISH": "BULL", "BEARISH": "BEAR"}.get(ctx["regime"])
        if direction is None:
            _clear(state)
            return _result(strategy, "NO_SETUP", ctx, blockers=["FUTURES_REGIME_RANGE"])
        wall = walls[_side(direction)]
        if (wall is None or wall["activity"]["1m"] != "WRITING"
                or wall["cluster_delta_oi"]["1m"] is None
                or wall["cluster_delta_oi"]["1m"] <= 0):
            _clear(state)
            return _result(strategy, "NO_SETUP", ctx, direction=direction,
                           blockers=["REINFORCING_OI_WALL_REQUIRED"])
        distance = _sign(direction) * (future - vwap["value"])
        if distance <= 0:
            _clear(state)
            return _result(strategy, "NO_SETUP", ctx, direction=direction,
                           blockers=["FUTURES_VWAP_INVALIDATED"])
        if state.get("direction") == direction and fast == direction and distance >= state["anchor"] + 2:
            anchor = state["anchor"]
            _clear(state)
            result = _result(strategy, "ENTER", ctx, direction=direction,
                             trigger="VWAP_PULLBACK_REJECTION_FAST_RESUMPTION")
            result["setup_anchor"] = anchor
            return result
        if distance <= max(5, future * .0005):
            _watch(state, direction, "FUTURES_VWAP_PULLBACK", distance, index)
            return _result(strategy, "WATCH", ctx, direction=direction, state=state)
        return _result(strategy, "NO_SETUP", ctx, direction=direction,
                       blockers=["VWAP_PULLBACK_NOT_OBSERVED"])

    if strategy == "ORB_RETEST":
        if vwap["status"] != "AVAILABLE" or vwap["slope_bps_per_minute"] is None:
            _clear(state)
            return _result(strategy, "NO_SETUP", ctx, blockers=[vwap["reason"] or
                           "VWAP_SLOPE_UNAVAILABLE"])
        if not structure["opening_range_complete"]:
            _clear(state)
            return _result(strategy, "NO_SETUP", ctx, blockers=["OPENING_RANGE_INCOMPLETE"])
        opening_high = ctx["opening_high"]
        opening_low = ctx["opening_low"]
        if opening_high is None or opening_low is None:
            _clear(state)
            return _result(strategy, "NO_SETUP", ctx, blockers=["OPENING_RANGE_MISSING"])
        direction = ("BULL" if spot >= opening_high + 5 else
                     "BEAR" if spot <= opening_low - 5 else state.get("direction"))
        if direction not in {"BULL", "BEAR"}:
            return _result(strategy, "NO_SETUP", ctx, blockers=["OPENING_BREAKOUT_MISSING"])
        sign = _sign(direction)
        line = opening_high if sign > 0 else opening_low
        aligned = (future is not None and sign * (future - vwap["value"]) > 0
                   and sign * vwap["slope_bps_per_minute"] > 0)
        if not aligned or sign * (spot - line) < -2:
            _clear(state)
            return _result(strategy, "NO_SETUP", ctx, direction=direction,
                           blockers=["ORB_VWAP_ALIGNMENT_OR_STRUCTURE_FAILED"])
        stage = state.get("stage") if state.get("direction") == direction else None
        if stage == "RETEST" and sign * (spot - line) >= 5 and fast == direction:
            _clear(state)
            return _result(strategy, "ENTER", ctx, direction=direction,
                           trigger="ORB_BREAKOUT_RETEST_FAST_RESUMPTION")
        if stage == "BREAKOUT" and -2 <= sign * (spot - line) <= 5:
            _watch(state, direction, "ORB_RETEST", line, index, "RETEST")
        elif stage is None and sign * (spot - line) >= 5:
            _watch(state, direction, "ORB_BREAKOUT", line, index, "BREAKOUT")
        if state:
            return _result(strategy, "WATCH", ctx, direction=direction, state=state)
        return _result(strategy, "NO_SETUP", ctx, direction=direction,
                       blockers=["ORB_BREAKOUT_NOT_OBSERVED"])

    if strategy == "OI_WALL":
        for direction in ("BEAR", "BULL"):
            wall = walls[_side(direction)]
            if wall is None or wall["activity"]["1m"] != "WRITING":
                continue
            sign = _sign(direction)
            distance = sign * (spot - wall["strike"])
            if not 0 <= distance <= 10:
                continue
            if (state.get("direction") == direction and state.get("stage") == "REJECTION"
                    and fast == direction and distance >= state["anchor"] + 2):
                anchor = state["anchor"]
                _clear(state)
                result = _result(strategy, "ENTER", ctx, direction=direction,
                                 trigger="OI_WALL_REJECTION_FAST_RESUMPTION")
                result["setup_anchor"] = anchor
                return result
            _watch(state, direction, "OI_WALL_TOUCH", distance, index, "REJECTION")
            return _result(strategy, "WATCH", ctx, direction=direction, state=state)
        # A weakening wall may become a continuation setup after price breaks it.
        for weak_side, direction in (("call_resistance", "BULL"),
                                     ("put_support", "BEAR")):
            wall = walls[weak_side]
            if (wall is not None and wall["wall_state"]["1m"] == "WEAKENING"
                    and 0 <= -_sign(direction) * (spot - wall["strike"]) <= 10):
                _watch(state, direction, "OI_WALL_WEAKENING", wall["strike"],
                       index, "BREAK")
                state["broken_wall"] = wall["strike"]
                return _result(strategy, "WATCH", ctx, direction=direction, state=state)
        # Breakout continuation needs failure of one wall and writing on the opposite side.
        for broken_side, direction in (("call_resistance", "BULL"),
                                       ("put_support", "BEAR")):
            broken = state.get("broken_wall")
            if broken is None or state.get("direction") != direction:
                continue
            supporting = walls[_side(direction)]
            if (supporting and supporting["activity"]["1m"] == "WRITING"
                    and _sign(direction) * (spot - broken) >= 5 and fast == direction):
                _clear(state)
                result = _result(strategy, "ENTER", ctx, direction=direction,
                                 trigger="OI_WALL_BREAK_AND_UNWIND_FAST_CONTINUATION")
                result["failed_wall"] = broken
                return result
        previous = ctx["previous_spot"]
        for broken_side, direction in (("call_resistance", "BULL"),
                                       ("put_support", "BEAR")):
            wall = walls[broken_side]
            supporting = walls[_side(direction)]
            if (wall is not None and supporting is not None and previous is not None
                    and supporting["activity"]["1m"] == "WRITING"
                    and wall["wall_state"]["1m"] == "WEAKENING"
                    and (previous - wall["strike"]) * (spot - wall["strike"]) <= 0
                    and _sign(direction) * (spot - wall["strike"]) >= 5):
                _watch(state, direction, "OI_WALL_BREAK_AND_UNWIND",
                       wall["strike"], index, "BREAK")
                state["broken_wall"] = wall["strike"]
                return _result(strategy, "WATCH", ctx, direction=direction, state=state)
        _clear(state)
        return _result(strategy, "NO_SETUP", ctx, blockers=["OI_WALL_SETUP_ABSENT"])

    # TREND_PULLBACK does not require VWAP, but still requires actual futures trend.
    five, fifteen = (ctx["futures_returns_bps"].get(key) for key in ("5m", "15m"))
    direction = None
    position = structure["range_position"]
    if five is not None and fifteen is not None and position is not None:
        if five >= 4 and fifteen >= 8 and position >= .55:
            direction = "BULL"
        elif five <= -4 and fifteen <= -8 and position <= .45:
            direction = "BEAR"
    if direction is None:
        _clear(state)
        return _result(strategy, "NO_SETUP", ctx, blockers=["FUTURES_5M_15M_TREND_REQUIRED"])
    one = structure["returns_bps"].get("1m")
    if state.get("direction") == direction and fast == direction:
        anchor = state["anchor"]
        if _sign(direction) * (spot - anchor) >= 3:
            _clear(state)
            result = _result(strategy, "ENTER", ctx, direction=direction,
                             trigger="TREND_PULLBACK_FAST_RESUMPTION")
            result["setup_anchor"] = anchor
            return result
    if one is not None and _sign(direction) * one <= -2:
        _watch(state, direction, "COUNTERTREND_1M_PULLBACK", spot, index)
        return _result(strategy, "WATCH", ctx, direction=direction, state=state)
    return _result(strategy, "WATCH" if state else "NO_SETUP", ctx,
                   direction=direction, state=state,
                   blockers=[] if state else ["COUNTERTREND_PULLBACK_NOT_OBSERVED"])
