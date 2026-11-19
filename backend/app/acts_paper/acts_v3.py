"""ACTS V3 entry-only opportunity cycles over the unchanged V2 signal engine."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.acts_paper import acts_v2
from app.paper.mirror_exit import ACTS_REASON
from app.research.manifest import digest
from app.scalper.repository import IST

STRATEGY_ID = "ACTS_V3"
POLICY_VERSION = "ACTS_V3_OPPORTUNITY_CYCLE_V1"
PARAMETERS: dict[str, Any] = {
    "version": POLICY_VERSION,
    "entry_score_floor": 60,
    "exceptional_reversal_score_floor": 85,
    "reversal_confirmation_seconds": 60,
    "max_confirmation_gap_seconds": 22.5,
    "observation_interval_seconds": 15,
    "underlying_signal_version": acts_v2.STRATEGY_ID,
    "exit_signal": "UNCHANGED_ACTS_V2_FULL_SIGNAL",
    "unfilled_entry": "RELEASE_RESERVATION_WITHOUT_CONSUMING",
}
AUDIT_FIELDS = (
    "cycle_id",
    "cycle_state",
    "rearm_reason",
    "previous_direction",
    "previous_exit_reason",
    "previous_exit_timestamp",
    "reversal_confirmation_seconds",
    "opportunity_cycle_policy_version",
)


def state(watch: dict, at: datetime, identity: tuple | None = None) -> dict:
    if at.tzinfo is None:
        raise ValueError("ACTS_V3_AWARE_OBSERVATION_REQUIRED")
    day = at.astimezone(IST).date().isoformat()
    cycle = watch.get("opportunity_cycle")
    if cycle and cycle.get("opportunity_cycle_policy_version") != POLICY_VERSION:
        raise ValueError("ACTS_V3_CYCLE_POLICY_MISMATCH")
    if not cycle or cycle["session_id"] != day:
        watch.clear()
        cycle = {
            "opportunity_cycle_policy_version": POLICY_VERSION,
            "session_id": day,
            "cycle_id": None,
            "cycle_sequence": 0,
            "cycle_state": "ARMED",
            "rearm_reason": None,
            "armed_direction": None,
            "consumed_directions": [],
            "previous_direction": None,
            "previous_exit_reason": None,
            "previous_exit_timestamp": None,
            "quarantined_direction": None,
            "pullback_direction": None,
            "reversal_confirmation_seconds": 0,
        }
        watch.update(strategy_version=STRATEGY_ID, opportunity_cycle=cycle, signal_watch={})
    if identity is not None and cycle.get("reference_identity") != list(identity):
        cycle["reference_identity"] = list(identity)
        reset_confirmation(cycle)
        cycle["pullback_direction"] = None
        watch["signal_watch"] = {}
        if cycle["cycle_state"] != "ENTERED":
            cycle.update(armed_direction=None, cycle_state="CONSUMED" if cycle["consumed_directions"] else "ARMED")
    return cycle


def reset_confirmation(cycle: dict) -> None:
    for name in ("confirmation_started_at", "confirmation_last_at", "confirmation_direction"):
        cycle.pop(name, None)
    cycle["reversal_confirmation_seconds"] = 0


def audit(cycle: dict) -> dict:
    return {key: deepcopy(cycle.get(key)) for key in AUDIT_FIELDS}


def _arm(cycle: dict, direction: str, reason: str, at: datetime) -> None:
    if cycle.get("armed_direction") == direction and cycle["cycle_state"] in ("ARMED", "REARMED"):
        return
    cycle["cycle_sequence"] += 1
    cycle.update(
        cycle_id=digest(
            {
                "policy": POLICY_VERSION,
                "session": cycle["session_id"],
                "sequence": cycle["cycle_sequence"],
                "direction": direction,
                "reset_at": at.isoformat(),
            }
        ),
        cycle_state="ARMED" if reason == "FIRST_OPPORTUNITY" else "REARMED",
        armed_direction=direction,
        rearm_reason=reason,
    )


def filled(watch: dict, at: datetime, direction: str) -> dict:
    cycle = state(watch, at)
    cycle["consumed_directions"] = sorted(set(cycle["consumed_directions"]) | {direction})
    cycle.update(cycle_state="ENTERED", armed_direction=None, quarantined_direction=None)
    return audit(cycle)


def closed(watch: dict, at: datetime, direction: str, reason: str) -> dict:
    cycle = state(watch, at)
    cycle["consumed_directions"] = sorted(set(cycle["consumed_directions"]) | {direction})
    cycle.update(
        cycle_state="CONSUMED",
        armed_direction=None,
        pullback_direction=None,
        previous_direction=direction,
        previous_exit_reason=reason,
        previous_exit_timestamp=at.isoformat(),
        quarantined_direction=("BEAR" if direction == "BULL" else "BULL") if reason == ACTS_REASON else None,
    )
    reset_confirmation(cycle)
    watch["signal_watch"] = {}
    return audit(cycle)


def rejected(watch: dict, at: datetime) -> dict:
    cycle = state(watch, at)
    cycle.update(
        cycle_state="ARMED" if cycle["rearm_reason"] == "FIRST_OPPORTUNITY" else "REARMED",
        armed_direction=cycle.get("entry_direction"),
    )
    return audit(cycle)


def decide(
    ctx: dict,
    watch: dict,
    candidate: Any,
    candidate_rejections: dict,
    risk_blockers: list[str],
    config: acts_v2.ActsV2Config,
) -> dict:
    at = datetime.fromisoformat(ctx["timestamp"])
    cycle = state(
        watch, at, (ctx.get("future_instrument_id"), str(ctx.get("future_expiry")), str(ctx.get("expiry_date")))
    )
    last = cycle.get("last_observed_at")
    if last and at <= datetime.fromisoformat(last):
        raise ValueError("ACTS_V3_NONCAUSAL_OBSERVATION")
    previous_signal_watch = deepcopy(watch.setdefault("signal_watch", {}))
    result = acts_v2.decide(ctx, watch["signal_watch"], candidate, candidate_rejections, risk_blockers, config)
    direction, market, score = result["direction"], result["market_state"], result["opportunity_score"]
    intrinsic = [reason for reason in result["hard_blockers"] if reason not in risk_blockers]
    full = bool(
        direction
        and candidate is not None
        and not intrinsic
        and not market.endswith("_PULLBACK")
        and score >= config.tradeable_score
    )
    entry_signal = full and score >= PARAMETERS["entry_score_floor"]
    flat = cycle["cycle_state"] != "ENTERED"
    if cycle.get("armed_direction") and direction != cycle["armed_direction"] and flat:
        cycle.update(armed_direction=None, cycle_state="CONSUMED")
    if flat and direction and market.endswith("_PULLBACK") and ctx.get("future") is not None:
        cycle.update(cycle_state="RESET_OBSERVED", pullback_direction=direction)
    exceptional = bool(
        flat
        and direction == cycle.get("quarantined_direction")
        and direction is not None
        and full
        and score >= PARAMETERS["exceptional_reversal_score_floor"]
    )
    if exceptional:
        previous = cycle.get("confirmation_last_at")
        gap = (at - datetime.fromisoformat(previous)).total_seconds() if previous else None
        if (
            previous is None
            or gap is None
            or gap <= 0
            or gap > PARAMETERS["max_confirmation_gap_seconds"]
            or cycle.get("confirmation_direction") != direction
        ):
            # A previously confirmed permission waiting for a hard cooldown
            # cannot survive a new gap or contract identity.
            if cycle.get("rearm_reason") == "EXCEPTIONAL_REVERSAL_CONFIRMATION":
                cycle.update(armed_direction=None, cycle_state="CONSUMED")
            cycle.update(confirmation_started_at=at.isoformat(), confirmation_direction=direction)
        cycle["confirmation_last_at"] = at.isoformat()
        cycle["reversal_confirmation_seconds"] = (
            at - datetime.fromisoformat(cycle["confirmation_started_at"])
        ).total_seconds()
    else:
        reset_confirmation(cycle)
        if flat and cycle.get("rearm_reason") == "EXCEPTIONAL_REVERSAL_CONFIRMATION":
            cycle.update(armed_direction=None, cycle_state="CONSUMED")
    if flat and entry_signal:
        if result["trigger"] == "PULLBACK_RESUMPTION" and cycle.get("pullback_direction") == direction:
            reason = (
                "OPPOSITE_PULLBACK_RESUMPTION"
                if cycle["previous_direction"] and direction != cycle["previous_direction"]
                else "PULLBACK_RESUMPTION"
            )
            _arm(cycle, direction, reason, at)
            cycle["pullback_direction"] = None
        elif cycle["previous_exit_reason"] == ACTS_REASON and direction == cycle["previous_direction"]:
            _arm(cycle, direction, "RETURN_AFTER_FALSE_REVERSAL", at)
        elif exceptional and cycle["reversal_confirmation_seconds"] >= PARAMETERS["reversal_confirmation_seconds"]:
            _arm(cycle, direction, "EXCEPTIONAL_REVERSAL_CONFIRMATION", at)
        elif direction not in cycle["consumed_directions"] and direction != cycle.get("quarantined_direction"):
            _arm(cycle, direction, "FIRST_OPPORTUNITY", at)
    blockers = list(result["hard_blockers"])
    if direction and score < PARAMETERS["entry_score_floor"]:
        blockers.append("V3_SCORE_FLOOR")
    permission = flat and cycle.get("armed_direction") == direction and cycle["cycle_state"] in ("ARMED", "REARMED")
    if entry_signal and not permission:
        if direction == cycle.get("quarantined_direction") and direction is not None:
            blockers.append("REVERSAL_QUARANTINE")
            if exceptional:
                blockers.append("REVERSAL_CONFIRMATION_PENDING")
        else:
            blockers.append("CONSUMED_CYCLE")
    enter = bool(entry_signal and permission and not blockers)
    if enter:
        cycle.update(cycle_state="ENTERED", entry_direction=direction)
        watch["signal_watch"] = {}
    cycle["last_observed_at"] = at.isoformat()
    if result["status"] == "ENTER" and not enter and previous_signal_watch.get("started_at"):
        watch["signal_watch"] = previous_signal_watch
    evidence = audit(cycle)
    result.update(
        strategy_id=STRATEGY_ID,
        status="ENTER" if enter else "WATCH" if not blockers and market.endswith("_PULLBACK") else "NO_SETUP",
        blockers=list(dict.fromkeys(blockers)),
        hard_blockers=list(dict.fromkeys(blockers)),
        underlying_signal_version=acts_v2.STRATEGY_ID,
        opportunity_cycle=evidence,
        **evidence,
    )
    if enter:
        result["entry_reason"] = (
            cycle["rearm_reason"] if cycle["rearm_reason"] != "FIRST_OPPORTUNITY" else result["entry_reason"]
        )
        result["thesis"] = {
            **(result["thesis"] or {}),
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_ID,
            "opportunity_cycle": evidence,
            "entry_reason": result["entry_reason"],
            "opportunity_cycle_policy": PARAMETERS,
        }
    else:
        result.update(thesis=None, entry_reason=None)
    result["watch_state"] = deepcopy(watch)
    return result
