"""Strict per-instrument, per-session paper risk rules for MCX outright futures."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.mcx_futures.spec import ContractSpec, round_to_tick


def new_state(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "realised_net": 0.0,
        "consecutive_losses": 0,
        "entries": 0,
        "last_exit_at": None,
        "last_loss_at": None,
        "gap_block_until": None,
        "gap_evidence": None,
    }


def ensure_session(state: dict[str, Any] | None, session_id: str) -> dict[str, Any]:
    """Daily counters reset per session; consecutive losses also reset (one session = one risk budget)."""
    if state is None or state["session_id"] != session_id:
        return new_state(session_id)
    return state


def stop_distance(atr: float, cfg: Any, spec: ContractSpec) -> float:
    """max(k x ATR, min ticks), rounded UP to the tick grid (never tighter than intended)."""
    raw = max(cfg.stop_atr_multiple * atr, cfg.min_stop_ticks * spec.tick_size)
    return round_to_tick(raw, spec.tick_size, "UP")


def risk_rupees(stop_points: float, cfg: Any, spec: ContractSpec) -> float:
    return stop_points * spec.lot_size * cfg.lots


def entry_blockers(state: dict[str, Any], at: datetime, *, atr: float | None, cfg: Any, spec: ContractSpec,
                   session: dict[str, Any] | None, position_open: bool, quote_age: float | None) -> list[str]:
    blockers: list[str] = []
    if session is None:
        return ["SESSION_CLOSED"]
    if position_open:
        blockers.append("POSITION_ALREADY_OPEN")
    if at >= datetime.fromisoformat(session["last_entry_at"]):
        blockers.append("AFTER_LAST_ENTRY_TIME")
    days = spec.days_to_expiry(datetime.fromisoformat(session["open_at"]).date())
    if days < cfg.expiry_window_days:
        blockers.append("EXPIRY_WINDOW")
    if quote_age is None or quote_age > cfg.stale_quote_seconds:
        blockers.append("STALE_FEED")
    if atr is None or atr <= 0:
        blockers.append("ATR_UNAVAILABLE")
        risk = None
    else:
        risk = risk_rupees(stop_distance(atr, cfg, spec), cfg, spec)
        if risk > cfg.max_risk_per_trade:
            blockers.append("STOP_RISK_EXCEEDS_MAX_RISK_PER_TRADE")
    loss = -state["realised_net"]
    if loss >= cfg.daily_loss_limit:
        blockers.append("DAILY_LOSS_LIMIT")
    elif risk is not None and loss + risk > cfg.daily_loss_limit:
        blockers.append("DAILY_LOSS_HEADROOM")
    if state["consecutive_losses"] >= cfg.max_consecutive_losses:
        blockers.append("MAX_CONSECUTIVE_LOSSES")
    if state["entries"] >= cfg.max_entries_per_session:
        blockers.append("MAX_ENTRIES_PER_SESSION")
    if state["last_exit_at"] and at < datetime.fromisoformat(state["last_exit_at"]) + timedelta(
            minutes=cfg.cooldown_after_exit_minutes):
        blockers.append("COOLDOWN_AFTER_EXIT")
    if state["last_loss_at"] and at < datetime.fromisoformat(state["last_loss_at"]) + timedelta(
            minutes=cfg.cooldown_after_loss_minutes):
        blockers.append("COOLDOWN_AFTER_LOSS")
    if state["gap_block_until"] and at < datetime.fromisoformat(state["gap_block_until"]):
        blockers.append("GAP_GUARD")
    return blockers


def check_gap(state: dict[str, Any], at: datetime, price: float, reference: float | None, atr: float | None,
              cfg: Any) -> dict[str, Any] | None:
    """Open/after-outage gap vs last known price > gap_guard_atr x ATR blocks entries for M minutes."""
    if reference is None:
        return None
    if atr is None or atr <= 0:
        until = at + timedelta(minutes=cfg.gap_guard_minutes)
        evidence = {"reason": "GAP_UNMEASURABLE_ATR_UNAVAILABLE", "price": price, "reference": reference}
    else:
        gap_atr = abs(price - reference) / atr
        if gap_atr <= cfg.gap_guard_atr:
            return None
        until = at + timedelta(minutes=cfg.gap_guard_minutes)
        evidence = {"reason": "GAP_EXCEEDS_ATR_LIMIT", "price": price, "reference": reference,
                    "gap_atr": round(gap_atr, 4)}
    evidence["until"] = until.isoformat()
    state["gap_block_until"] = until.isoformat()
    state["gap_evidence"] = evidence
    return evidence


def record_entry(state: dict[str, Any]) -> None:
    state["entries"] += 1


def record_exit(state: dict[str, Any], at: datetime, net: float) -> None:
    state["realised_net"] = round(state["realised_net"] + net, 6)
    state["last_exit_at"] = at.isoformat()
    if net < 0:
        state["consecutive_losses"] += 1
        state["last_loss_at"] = at.isoformat()
    else:
        state["consecutive_losses"] = 0
