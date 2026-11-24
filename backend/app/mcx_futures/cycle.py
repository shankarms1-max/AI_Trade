"""V3 opportunity-cycle semantics for one MCX futures instrument and session.

Ported intent of NIFTY ACTS V3 (not its options assumptions):
- FIRST_OPPORTUNITY arms a direction once per session;
- a direction is CONSUMED only when a paper entry FILLS; an unfilled or
  rejected entry releases the reservation without consuming;
- PULLBACK_RESUMPTION re-arms a consumed direction after a pullback (opposite
  k-bar move >= pullback_atr x ATR while the EMA trend is intact) followed by a
  confirmed resumption within ``setup_lifetime_minutes``;
- RETURN_AFTER_FALSE_REVERSAL re-arms the previous direction after an
  opposite-signal exit when that direction returns;
- the opposite direction of an opposite-signal exit is QUARANTINED; it may only
  be entered after an EXCEPTIONAL_REVERSAL_CONFIRMATION (score >= exceptional
  floor sustained for ``reversal_confirmation_seconds`` over decisions whose
  spacing never exceeds one bar plus 1.5 observation intervals);
- a new session (IST date) resets everything.

State is a JSON-safe dict; every function is deterministic and pure apart from
mutating the passed state.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Any

POLICY_VERSION = "ACTS_V3_MCX_FUTURES_CYCLE_V1"
ARMED, REARMED, RESERVED, ENTERED, CONSUMED = "ARMED", "REARMED", "RESERVED", "ENTERED", "CONSUMED"
OPPOSITE_SIGNAL = "OPPOSITE_SIGNAL"
FIRST_OPPORTUNITY = "FIRST_OPPORTUNITY"
EXCEPTIONAL = "EXCEPTIONAL_REVERSAL_CONFIRMATION"
AUDIT_FIELDS = (
    "session_id", "cycle_id", "cycle_sequence", "cycle_state", "rearm_reason", "armed_direction", "entry_direction",
    "consumed_directions", "previous_direction", "previous_exit_reason", "previous_exit_at", "quarantined_direction",
    "pullback", "reversal_confirmation_seconds", "signal_streak", "policy",
)


def new_state(session_id: str) -> dict[str, Any]:
    return {
        "policy": POLICY_VERSION,
        "session_id": session_id,
        "cycle_id": None,
        "cycle_sequence": 0,
        "cycle_state": ARMED,
        "rearm_reason": None,
        "armed_direction": None,
        "entry_direction": None,
        "consumed_directions": [],
        "previous_direction": None,
        "previous_exit_reason": None,
        "previous_exit_at": None,
        "quarantined_direction": None,
        "pullback": None,
        "confirmation": None,
        "reversal_confirmation_seconds": 0.0,
        "signal_streak": None,
        "last_decision_at": None,
    }


def ensure_session(state: dict[str, Any] | None, session_id: str) -> dict[str, Any]:
    if state is not None and state.get("policy") != POLICY_VERSION:
        raise ValueError("CYCLE_POLICY_MISMATCH")
    if state is None or state["session_id"] != session_id:
        return new_state(session_id)
    return state


def audit(state: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps({key: state.get(key) for key in AUDIT_FIELDS}))


def max_confirmation_gap_seconds(cfg: Any) -> float:
    """Consecutive decisions are one bar apart; tolerate 1.5 observation intervals of jitter."""
    return float(cfg.bar_seconds + 1.5 * cfg.observation_interval_seconds)


def _arm(state: dict[str, Any], direction: str, reason: str, at: datetime) -> None:
    if state.get("armed_direction") == direction and state["cycle_state"] in (ARMED, REARMED):
        return
    state["cycle_sequence"] += 1
    blob = json.dumps({"policy": POLICY_VERSION, "session": state["session_id"], "sequence": state["cycle_sequence"],
                       "direction": direction, "at": at.isoformat()}, sort_keys=True)
    state.update(
        cycle_id=hashlib.sha256(blob.encode()).hexdigest()[:16],
        cycle_state=ARMED if reason == FIRST_OPPORTUNITY else REARMED,
        armed_direction=direction,
        rearm_reason=reason,
    )


def _reset_confirmation(state: dict[str, Any]) -> None:
    state["confirmation"] = None
    state["reversal_confirmation_seconds"] = 0.0


def _flat(state: dict[str, Any]) -> bool:
    return state["cycle_state"] not in (RESERVED, ENTERED)


def update_streak(state: dict[str, Any], at: datetime, direction: str | None, qualifies: bool, cfg: Any) -> int:
    streak = state.get("signal_streak")
    if not qualifies or direction is None:
        state["signal_streak"] = None
        return 0
    contiguous = bool(
        streak
        and streak["direction"] == direction
        and 0 < (at - datetime.fromisoformat(streak["last_at"])).total_seconds() <= max_confirmation_gap_seconds(cfg)
    )
    count = streak["count"] + 1 if contiguous and streak else 1
    state["signal_streak"] = {"direction": direction, "count": count, "last_at": at.isoformat()}
    return count


def decide(state: dict[str, Any], at: datetime, evaluation: dict[str, Any], blockers: list[str],
           cfg: Any) -> dict[str, Any]:
    """Apply one completed-bar decision.  ``blockers`` = hard vetoes + risk blockers."""
    if at.tzinfo is None:
        raise ValueError("CYCLE_AWARE_TIMESTAMP_REQUIRED")
    last = state.get("last_decision_at")
    if last and at <= datetime.fromisoformat(last):
        raise ValueError("CYCLE_NONCAUSAL_DECISION")
    direction = evaluation["direction"]
    market = evaluation["market_state"]
    score = float(evaluation["score"])
    pullback_now = market.endswith("_PULLBACK")
    qualifies = bool(direction and evaluation.get("warm") and not pullback_now and score >= cfg.entry_score_floor)
    count = update_streak(state, at, direction, qualifies, cfg)
    entry_signal = qualifies and count >= cfg.confirmation_bars
    flat = _flat(state)
    if flat and state.get("armed_direction") and direction != state["armed_direction"]:
        state.update(armed_direction=None, cycle_state=CONSUMED)
    pullback = state.get("pullback")
    if pullback and (at - datetime.fromisoformat(pullback["at"])) > timedelta(minutes=cfg.setup_lifetime_minutes):
        state["pullback"] = pullback = None
    if flat and direction and pullback_now:
        if not pullback or pullback["direction"] != direction:
            state["pullback"] = pullback = {"direction": direction, "at": at.isoformat()}
    exceptional = bool(flat and direction is not None and direction == state.get("quarantined_direction")
                       and qualifies and score >= cfg.exceptional_reversal_score_floor)
    if exceptional:
        conf = state.get("confirmation")
        gap = (at - datetime.fromisoformat(conf["last_at"])).total_seconds() if conf else None
        if conf is None or gap is None or gap <= 0 or gap > max_confirmation_gap_seconds(cfg) \
                or conf["direction"] != direction:
            if state.get("rearm_reason") == EXCEPTIONAL:
                state.update(armed_direction=None, cycle_state=CONSUMED)
            conf = {"direction": direction, "started_at": at.isoformat()}
        conf["last_at"] = at.isoformat()
        state["confirmation"] = conf
        state["reversal_confirmation_seconds"] = (at - datetime.fromisoformat(conf["started_at"])).total_seconds()
    else:
        _reset_confirmation(state)
        if flat and state.get("rearm_reason") == EXCEPTIONAL:
            state.update(armed_direction=None, cycle_state=CONSUMED)
    if flat and entry_signal and direction:
        if pullback and pullback["direction"] == direction and not pullback_now:
            reason = ("OPPOSITE_PULLBACK_RESUMPTION"
                      if state["previous_direction"] and direction != state["previous_direction"]
                      else "PULLBACK_RESUMPTION")
            _arm(state, direction, reason, at)
            state["pullback"] = None
        elif state["previous_exit_reason"] == OPPOSITE_SIGNAL and direction == state["previous_direction"]:
            _arm(state, direction, "RETURN_AFTER_FALSE_REVERSAL", at)
        elif exceptional and state["reversal_confirmation_seconds"] >= cfg.reversal_confirmation_seconds:
            _arm(state, direction, EXCEPTIONAL, at)
        elif direction not in state["consumed_directions"] and direction != state.get("quarantined_direction"):
            _arm(state, direction, FIRST_OPPORTUNITY, at)
    out = list(blockers)
    if direction and score < cfg.entry_score_floor:
        out.append("SCORE_BELOW_FLOOR")
    if pullback_now:
        out.append("PULLBACK_IN_PROGRESS")
    if qualifies and not entry_signal:
        out.append("AWAITING_CONFIRMATION")
    if not flat:
        out.append("POSITION_OR_RESERVATION_ACTIVE")
    permission = flat and state.get("armed_direction") == direction and state["cycle_state"] in (ARMED, REARMED)
    if entry_signal and flat and not permission:
        if direction == state.get("quarantined_direction"):
            out.append("REVERSAL_QUARANTINE")
            if exceptional:
                out.append("REVERSAL_CONFIRMATION_PENDING")
        else:
            out.append("CONSUMED_CYCLE")
    out = list(dict.fromkeys(out))
    enter = bool(entry_signal and permission and not out)
    if enter:
        state.update(cycle_state=RESERVED, entry_direction=direction)
    state["last_decision_at"] = at.isoformat()
    return {
        "enter": enter,
        "direction": direction,
        "entry_signal": entry_signal,
        "blockers": out,
        "entry_reason": state["rearm_reason"] if enter else None,
        "cycle": audit(state),
    }


def filled(state: dict[str, Any], at: datetime, direction: str) -> dict[str, Any]:
    """Consumption happens here, on FILL only."""
    state["consumed_directions"] = sorted(set(state["consumed_directions"]) | {direction})
    state.update(cycle_state=ENTERED, armed_direction=None, quarantined_direction=None, entry_direction=direction)
    return audit(state)


def released(state: dict[str, Any], at: datetime) -> dict[str, Any]:
    """Unfilled/rejected entry: release the reservation WITHOUT consuming the direction."""
    if state["cycle_state"] == RESERVED:
        state.update(
            cycle_state=ARMED if state["rearm_reason"] == FIRST_OPPORTUNITY else REARMED,
            armed_direction=state.get("entry_direction"),
            entry_direction=None,
        )
    return audit(state)


def closed(state: dict[str, Any], at: datetime, direction: str, reason: str) -> dict[str, Any]:
    state["consumed_directions"] = sorted(set(state["consumed_directions"]) | {direction})
    state.update(
        cycle_state=CONSUMED,
        armed_direction=None,
        entry_direction=None,
        pullback=None,
        signal_streak=None,
        previous_direction=direction,
        previous_exit_reason=reason,
        previous_exit_at=at.isoformat(),
        quarantined_direction=("SHORT" if direction == "LONG" else "LONG") if reason == OPPOSITE_SIGNAL else None,
    )
    _reset_confirmation(state)
    return audit(state)
