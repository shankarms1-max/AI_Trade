"""Pure deterministic AST_FUTURES_V1_DIRECT state machine, shared by the PAPER worker and the regression.

Signals use completed 5-minute bars only. A signal or AST flip completed at bar end T executes at the first
executable observation at or after T, which belongs to the following bar. The signal bar is never its own fill.
Persistence, clocks and market access belong to the caller.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.adaptive_supertrend.bars import opening_range
from app.adaptive_supertrend.direct import (
    ATR_PERIOD,
    BAR_SECONDS,
    FILL_BASIS,
    FILL_BASIS_NOTE,
    MULTIPLIER_TENTHS,
    SESSION_CLOSE_TIME,
    SESSION_EXIT_TIME,
    VERSION,
)
from app.adaptive_supertrend.direct.policy import DirectPolicy
from app.adaptive_supertrend.indicators import supertrend
from app.parallel_paper.bars import Bar
from app.research.manifest import digest
from app.scalper.repository import IST

ACTIVE = ("PENDING_PAPER_ENTRY", "OPEN", "PENDING_PAPER_EXIT", "UNRESOLVED_EXPOSURE")
EXIT_EVENTS = {"AST_FLIP": "AST_FLIP_EXIT", "FORCED_EXIT": "FORCED_EXIT", "SAFETY_EXIT_KILL_SWITCH": "SAFETY_EXIT"}
EXIT_CATEGORY = {"AST_FLIP": "STRATEGY", "FORCED_EXIT": "SESSION", "SAFETY_EXIT_KILL_SWITCH": "SAFETY"}


@dataclass(frozen=True)
class BarEval:
    bar: Bar
    direction: str | None
    value: float | None
    atr: float | None
    transition: str | None


@dataclass(frozen=True)
class Observation:
    snapshot_id: int
    at: datetime
    price: Decimal | None
    future_id: str | None
    future_expiry: date | None
    lot_size: int | None
    timing_ok: bool


@dataclass
class Step:
    decision: dict
    active: dict | None
    created: dict | None
    events: list[tuple[str, dict]] = field(default_factory=list)


def bar_identity(bar: Bar) -> tuple:
    return getattr(bar, "future_id", None), getattr(bar, "future_expiry", None)


def evaluate(bars: list[Bar]) -> list[BarEval]:
    """Fixed 22/3.9 AST over contiguous valid same-contract runs. A gap or roll restarts causal warm-up."""
    result: list[BarEval] = []
    run: list[Bar] = []

    def flush() -> None:
        if run:
            for bar, row in zip(run, supertrend(run, ATR_PERIOD, MULTIPLIER_TENTHS)):
                result.append(BarEval(bar, row["direction"], row["value"], row["atr"], row["transition"]))
            run.clear()

    for bar in bars:
        if run and bar_identity(run[-1]) != bar_identity(bar):
            flush()
        if not bar.valid:
            flush()
            result.append(BarEval(bar, None, None, None, None))
        else:
            run.append(bar)
    flush()
    if any(left.bar.end > right.bar.start for left, right in zip(result, result[1:])):
        raise ValueError("AST_DIRECT_CHRONOLOGICAL_BARS_REQUIRED")
    return result


def _bar_doc(bar: Bar) -> dict:
    expiry = getattr(bar, "future_expiry", None)
    return {
        "start": bar.start.isoformat(),
        "end": bar.end.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "valid": bar.valid,
        "reason": bar.reason,
        "future_instrument_id": getattr(bar, "future_id", None),
        "future_expiry": str(expiry) if expiry is not None else None,
    }


def _obs_identity(obs: Observation) -> list:
    return [obs.future_id, str(obs.future_expiry) if obs.future_expiry is not None else None]


def price_blocker(policy: DirectPolicy, obs: Observation) -> str | None:
    """Fail-closed executability of one observation for a paper fill on the LTP proxy basis."""
    if not obs.timing_ok:
        return "OBSERVATION_TIMING_INVALID"
    if obs.price is None or not obs.price.is_finite() or obs.price <= 0:
        return "FUTURES_PRICE_UNAVAILABLE"
    if not (obs.future_id or "").strip() or obs.future_expiry is None or obs.future_expiry < obs.at.date():
        return "FUTURES_IDENTITY_UNAVAILABLE_OR_EXPIRED"
    return None


def lot_blocker(policy: DirectPolicy, obs: Observation) -> str | None:
    return None if type(obs.lot_size) is int and obs.lot_size == policy.lot_size else "FUTURES_LOT_SIZE_MISMATCH"


def _forced_deadline(policy: DirectPolicy, at: datetime) -> datetime:
    return datetime.combine(at.date(), SESSION_EXIT_TIME, IST) + timedelta(seconds=policy.forced_exit_grace_seconds)


def _unresolved(trade: dict, reason: str, obs: Observation, events: list) -> None:
    trade.update(state="UNRESOLVED_EXPOSURE", unresolved_reason=reason, unresolved_at=obs.at.isoformat())
    events.append(("UNRESOLVED_EXPOSURE", copy.deepcopy(trade)))


def _reject(trade: dict, reason: str, obs: Observation, events: list) -> None:
    trade.update(state="ENTRY_REJECTED", rejection_reason=reason, rejected_at=obs.at.isoformat())
    events.append(("ENTRY_REJECTED", copy.deepcopy(trade)))


def _try_entry(policy, state, trade, obs, kill_switch, events) -> str | None:
    """Fill a pending entry, reject it, or return the reason it waits for a later observation."""
    at = obs.at
    delay = (at - datetime.fromisoformat(trade["signal_timestamp"])).total_seconds()
    identity = _obs_identity(obs)
    if kill_switch:
        _reject(trade, "KILL_SWITCH", obs, events)
    elif at.time().replace(tzinfo=None) >= SESSION_EXIT_TIME:
        _reject(trade, "CANCELLED_AT_SESSION_EXIT", obs, events)
    elif delay > policy.entry_fill_window_seconds:
        last = state.get("pending_fill_blocker") or "NO_OBSERVATION"
        _reject(trade, f"ENTRY_FILL_WINDOW_EXPIRED:{last}", obs, events)
    elif obs.future_id and identity != [trade["future_instrument_id"], trade["future_expiry"]]:
        _reject(trade, "FUTURES_CONTRACT_CHANGED", obs, events)
    else:
        blocker = price_blocker(policy, obs) or lot_blocker(policy, obs)
        if blocker:
            return blocker
        assert obs.price is not None
        trade.update(
            state="OPEN",
            entry_timestamp=at.isoformat(),
            entry_snapshot_id=obs.snapshot_id,
            entry_price=float(obs.price),
            entry_delay_seconds=delay,
            entry_observed_lot_size=obs.lot_size,
        )
        state["consumed"].append(trade["opportunity_id"])
        state["mark"] = {"trade_id": trade["trade_id"], "at": at.isoformat(), "price": float(obs.price),
                         "points": 0.0, "mfe_points": 0.0, "mae_points": 0.0}
        events.append(("ENTRY", copy.deepcopy(trade)))
    return None


def _points(trade: dict, price: Decimal) -> Decimal:
    return (price - Decimal(str(trade["entry_price"]))) * (1 if trade["side"] == "LONG" else -1)


def _mark(state, trade, obs) -> None:
    assert obs.price is not None
    points = float(_points(trade, obs.price))
    mark = state.get("mark") or {"mfe_points": 0.0, "mae_points": 0.0}
    state["mark"] = {
        "trade_id": trade["trade_id"],
        "at": obs.at.isoformat(),
        "price": float(obs.price),
        "points": points,
        "mfe_points": max(mark["mfe_points"], points),
        "mae_points": min(mark["mae_points"], points),
    }


def _try_exit(policy, state, trade, obs, events) -> str | None:
    at = obs.at
    held = [trade["future_instrument_id"], trade["future_expiry"]]
    identity = _obs_identity(obs)
    if obs.future_id and identity != held:
        # The held contract can no longer be priced. Never substitute another contract's price.
        _unresolved(trade, "HELD_CONTRACT_UNAVAILABLE", obs, events)
        return None
    blocker = price_blocker(policy, obs)
    if blocker is None:
        assert obs.price is not None
        _mark(state, trade, obs)
        points = _points(trade, obs.price)
        reason = trade["pending_exit_reason"]
        friction = policy.friction_points_per_round_trip
        mark = state["mark"]
        trade.update(
            state="CLOSED",
            exit_timestamp=at.isoformat(),
            exit_snapshot_id=obs.snapshot_id,
            exit_price=float(obs.price),
            exit_reason=reason,
            exit_category=EXIT_CATEGORY[reason],
            gross_points=float(points),
            gross_inr=float(points * trade["lot_size"]),
            friction_points=friction,
            net_research_points=None if friction is None else float(points - Decimal(str(friction))),
            mfe_points=mark["mfe_points"],
            mae_points=mark["mae_points"],
            closed_date=at.date().isoformat(),
        )
        state["mark"] = None
        events.append((EXIT_EVENTS[reason], copy.deepcopy(trade)))
        return None
    local = at.time().replace(tzinfo=None)
    if local >= SESSION_CLOSE_TIME:
        _unresolved(trade, f"SESSION_CLOSED_WITHOUT_EXIT:{blocker}", obs, events)
    elif local >= SESSION_EXIT_TIME and at > _forced_deadline(policy, at):
        _unresolved(trade, f"FORCED_EXIT_UNEXECUTABLE:{blocker}", obs, events)
    return blocker


def _manage(policy, state, trade, obs, new, kill_switch, events) -> str | None:
    """Advance the trade that was active before this observation. Returns a fill blocker, if any."""
    day = obs.at.date().isoformat()
    if trade["state"] == "UNRESOLVED_EXPOSURE":
        return None
    if trade["trading_date"] != day:
        if trade["state"] == "PENDING_PAPER_ENTRY":
            _reject(trade, "SESSION_OVER", obs, events)
        else:
            _unresolved(trade, "MISSED_SESSION_EXIT", obs, events)
        return None
    if trade["state"] == "PENDING_PAPER_ENTRY":
        return _try_entry(policy, state, trade, obs, kill_switch, events)
    if trade["state"] == "OPEN" and obs.future_id and _obs_identity(obs) != [
        trade["future_instrument_id"],
        trade["future_expiry"],
    ]:
        # AST and marks can no longer be evaluated on the held contract. Never mutate it into another one.
        _unresolved(trade, "HELD_CONTRACT_UNAVAILABLE", obs, events)
        return None
    if trade["state"] == "OPEN":
        side = "BULL" if trade["side"] == "LONG" else "BEAR"
        entered = datetime.fromisoformat(trade["entry_timestamp"])
        flip = next((e for e in new if e.bar.end > entered and e.direction not in (None, side)), None)
        reason = (
            "SAFETY_EXIT_KILL_SWITCH"
            if kill_switch
            else "AST_FLIP" if flip else "FORCED_EXIT" if obs.at.time().replace(tzinfo=None) >= SESSION_EXIT_TIME
            else None
        )
        if reason is None:
            if price_blocker(policy, obs) is None and _obs_identity(obs) == [
                trade["future_instrument_id"],
                trade["future_expiry"],
            ]:
                _mark(state, trade, obs)
            return None
        trade.update(
            state="PENDING_PAPER_EXIT",
            pending_exit_reason=reason,
            exit_decision_timestamp=obs.at.isoformat(),
            exit_decision_snapshot_id=obs.snapshot_id,
            flip_bar=_bar_doc(flip.bar) if flip else None,
            flip_direction=flip.direction if flip else None,
            flip_transition=flip.transition if flip else None,
        )
        events.append(("PENDING_PAPER_EXIT", copy.deepcopy(trade)))
    if trade["state"] == "PENDING_PAPER_EXIT":
        return _try_exit(policy, state, trade, obs, events)
    return None


def opportunity(day: str, direction: str, orb: dict, transition: str | None, identity: list) -> tuple[str, dict]:
    value = {
        "strategy_id": VERSION,
        "trading_date": day,
        "direction": direction,
        "opening_range": {key: orb[key] for key in ("high", "low", "range_start", "range_end")},
        "atr_period": ATR_PERIOD,
        "multiplier_tenths": MULTIPLIER_TENTHS,
        "ast_transition": transition,
        "future_identity": identity,
    }
    return digest(value), value


def step(
    policy: DirectPolicy,
    state: dict,
    active: dict | None,
    obs: Observation,
    completed: list[BarEval],
    *,
    kill_switch: bool = False,
) -> Step:
    if obs.at.tzinfo is None:
        raise ValueError("AST_DIRECT_AWARE_CLOCK_REQUIRED")
    obs = Observation(**{**obs.__dict__, "at": obs.at.astimezone(IST)})
    at = obs.at
    if any(e.bar.end > at for e in completed):
        raise ValueError("AST_DIRECT_FUTURE_BAR")
    day = at.date().isoformat()
    if state.get("date") != day:
        state.clear()
        state.update(date=day, opening_range=None, last_bar_end=None, consumed=[], mark=None, pending_fill_blocker=None)
    session = [e for e in completed if e.bar.start.astimezone(IST).date() == at.date()]
    if state["opening_range"] is None:
        candidate = opening_range([e.bar for e in session], at)
        window = datetime.fromisoformat(candidate["range_start"]), datetime.fromisoformat(candidate["range_end"])
        source = [e.bar for e in session if window[0] <= e.bar.start < window[1]]
        if candidate["complete"] and len({bar_identity(bar) for bar in source}) == 1:
            state["opening_range"] = {
                "high": candidate["opening_high"],
                "low": candidate["opening_low"],
                "range_start": candidate["range_start"],
                "range_end": candidate["range_end"],
                "source_bars": [_bar_doc(bar) for bar in source],
                "frozen_at": at.isoformat(),
            }
    previous_end = datetime.fromisoformat(state["last_bar_end"]) if state["last_bar_end"] else None
    new = [e for e in session if previous_end is None or e.bar.end > previous_end]
    if session:
        state["last_bar_end"] = session[-1].bar.end.isoformat()
    latest = session[-1] if session else None
    current = latest is not None and latest.bar.valid and (at - latest.bar.end).total_seconds() < BAR_SECONDS
    orb = state["opening_range"]

    events: list[tuple[str, dict]] = []
    trade = copy.deepcopy(active) if active is not None else None
    held = trade is not None and trade["state"] in ACTIVE
    fill_blocker = _manage(policy, state, trade, obs, new, kill_switch, events) if held else None

    blockers: list[str] = []
    if kill_switch:
        blockers.append("KILL_SWITCH_ACTIVE")
    if orb is None:
        blockers.append("AST_OPENING_RANGE_UNAVAILABLE")
    if not current:
        blockers.append("AST_CURRENT_COMPLETED_BAR_UNAVAILABLE")
    elif latest is not None and latest.direction is None:
        blockers.append("AST_WARMUP_REQUIRED")
    breakout = opportunity_id = None
    created = None
    signal_blockers: list[str] = []
    evaluated = latest if latest is not None and new and new[-1] is latest and current else None
    if evaluated is not None and orb is not None and evaluated.direction is not None:
        close = evaluated.bar.close
        breakout = "BULL" if close > orb["high"] else "BEAR" if close < orb["low"] else None
        if breakout is None:
            blockers.append("ORB_NOT_BROKEN")
        elif breakout != evaluated.direction:
            blockers.append("AST_ORB_DIRECTION_CONFLICT")
        else:
            identity = [str(value) if value is not None else None for value in bar_identity(evaluated.bar)]
            opportunity_id, identity_doc = opportunity(day, breakout, orb, evaluated.transition, identity)
            if kill_switch:
                signal_blockers.append("KILL_SWITCH_ACTIVE")
            if (
                evaluated.bar.end.astimezone(IST).time().replace(tzinfo=None) >= SESSION_EXIT_TIME
                or at.time().replace(tzinfo=None) >= SESSION_EXIT_TIME
            ):
                signal_blockers.append("SESSION_EXIT_BOUNDARY")
            if opportunity_id in state["consumed"]:
                signal_blockers.append("AST_OPPORTUNITY_CONSUMED")
            if held:
                signal_blockers.append("MAX_OPEN_POSITIONS_REACHED")
            if None in identity:
                signal_blockers.append("SIGNAL_BAR_FUTURES_IDENTITY_UNAVAILABLE")
            if not signal_blockers:
                trade_id = digest({"strategy": VERSION, "snapshot_id": obs.snapshot_id, "opportunity": opportunity_id})
                created = {
                    "trade_id": trade_id,
                    "strategy_id": VERSION,
                    "strategy_version": VERSION,
                    "mode": "PAPER",
                    "state": "PENDING_PAPER_ENTRY",
                    "trading_date": day,
                    "side": "LONG" if breakout == "BULL" else "SHORT",
                    "direction": breakout,
                    "opportunity_id": opportunity_id,
                    "opportunity": identity_doc,
                    "signal_bar": _bar_doc(evaluated.bar),
                    "signal_price": close,
                    "signal_timestamp": evaluated.bar.end.isoformat(),
                    "decision_timestamp": at.isoformat(),
                    "decision_snapshot_id": obs.snapshot_id,
                    "opening_range": orb,
                    "atr_period": ATR_PERIOD,
                    "multiplier": MULTIPLIER_TENTHS / 10,
                    "ast": {
                        "direction": evaluated.direction,
                        "value": evaluated.value,
                        "atr": evaluated.atr,
                        "transition": evaluated.transition,
                    },
                    "future_instrument_id": identity[0],
                    "future_expiry": identity[1],
                    "lots": policy.lots,
                    "lot_size": policy.lot_size,
                    "quantity": policy.lot_size * policy.lots,
                    "fill_basis": FILL_BASIS,
                    "fill_basis_note": FILL_BASIS_NOTE,
                    "policy_hash": policy.policy_hash,
                    "policy": policy.model_dump(mode="json"),
                }
                events.append(("PENDING_PAPER_ENTRY", copy.deepcopy(created)))
                fill_blocker = _try_entry(policy, state, created, obs, kill_switch, events)
    position = created if created is not None else trade
    # Fill blockers are observation evidence, kept in strategy state so trade documents change only with events.
    pending = position is not None and position["state"] == "PENDING_PAPER_ENTRY"
    state["pending_fill_blocker"] = fill_blocker if pending else None
    if position is not None and position["state"] in ACTIVE:
        status = position["state"]
    elif events and events[-1][0] in (*EXIT_EVENTS.values(), "ENTRY_REJECTED"):
        status = "CLOSED" if events[-1][0] != "ENTRY_REJECTED" else "ENTRY_REJECTED"
    elif signal_blockers:
        status = "SIGNAL_BLOCKED"
    elif any(b != "ORB_NOT_BROKEN" and b != "AST_ORB_DIRECTION_CONFLICT" for b in blockers):
        status = "NO_SETUP"
    else:
        status = "WATCH"
    decision = {
        "strategy_id": VERSION,
        "strategy_version": VERSION,
        "mode": "PAPER",
        "timestamp": at.isoformat(),
        "snapshot_id": obs.snapshot_id,
        "trading_date": day,
        "status": status,
        "blockers": list(dict.fromkeys(blockers + signal_blockers)),
        "fill_blocker": fill_blocker,
        "kill_switch": kill_switch,
        "futures": {
            "instrument_id": obs.future_id,
            "expiry": str(obs.future_expiry) if obs.future_expiry is not None else None,
            "observed_lot_size": obs.lot_size,
            "ltp": float(obs.price) if obs.price is not None and obs.price.is_finite() else None,
            "timing_ok": obs.timing_ok,
            "observation_blocker": price_blocker(policy, obs) or lot_blocker(policy, obs),
        },
        "opening_range": {k: v for k, v in orb.items() if k != "source_bars"} if orb else None,
        "last_completed_bar": _bar_doc(latest.bar) if latest is not None else None,
        "ast": {
            "atr_period": ATR_PERIOD,
            "multiplier": MULTIPLIER_TENTHS / 10,
            "direction": latest.direction if latest is not None else None,
            "value": latest.value if latest is not None else None,
            "atr": latest.atr if latest is not None else None,
            "transition": latest.transition if latest is not None else None,
        },
        "evaluated_bar_end": evaluated.bar.end.isoformat() if evaluated is not None else None,
        "breakout_direction": breakout,
        "opportunity_id": opportunity_id,
        "position": (
            {
                key: position.get(key)
                for key in ("trade_id", "state", "side", "entry_price", "entry_timestamp", "pending_exit_reason")
            }
            if position is not None and position["state"] in ACTIVE
            else None
        ),
        "mark": state.get("mark"),
    }
    return Step(decision, trade, created, events)
