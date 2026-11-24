"""Read-only ACTS_V3_MCX_FUTURES projections (PAPER only). GET routes only; no engine is instantiated here."""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.mcx_futures.models import INSTRUMENTS, McxFuturesCursor, McxFuturesEvent, McxFuturesTrade
from app.mcx_futures.runtime import split_components
from app.mcx_futures.session import SessionCalendar
from app.mcx_futures.settings import McxFuturesSettings

IST = ZoneInfo("Asia/Kolkata")
router = APIRouter(prefix="/api/mcx-futures", tags=["mcx-futures"])
FUTURE_TOLERANCE_SECONDS = 5


def _instrument(value: str | None) -> str | None:
    if value is not None and value not in INSTRUMENTS:
        raise HTTPException(status_code=422, detail="INSTRUMENT_MUST_BE_CRUDEOILM_OR_NATGASMINI")
    return value


def _age(at: str | None, now: datetime) -> float | None:
    if not at:
        return None
    try:
        return (now - datetime.fromisoformat(at)).total_seconds()  # negative = timestamp in the future
    except ValueError:
        return None


def derive_state(latest: dict[str, Any], enabled: bool, now: datetime, stale_after: int,
                 calendar: SessionCalendar | None = None) -> tuple[str, str | None]:
    """Server-side truth: never report RUNNING from a cached snapshot older than the stale threshold."""
    if not enabled:
        return "DISABLED", "MCX_FUTURES_PAPER_FLAG_OFF"
    if not latest:
        return "IDLE", "NO_OBSERVATION_RECORDED_YET"
    if latest.get("error"):
        return "ERROR", str(latest["error"])
    current_session = (calendar or SessionCalendar()).state(now)
    if current_session.status != "OPEN":
        return "MARKET_CLOSED", "OUTSIDE_MCX_SESSION_OR_HOLIDAY"
    session = (latest.get("session") or {}).get("status")
    age = _age((latest.get("feed") or {}).get("last_quote_at"), now)
    if session != "OPEN":
        # A closed-session snapshot is only trusted until the next scheduled open.
        nxt = (latest.get("session") or {}).get("next_open")
        if nxt and datetime.fromisoformat(nxt) <= now:
            return "DATA_STALE", "NO_OBSERVATION_SINCE_SCHEDULED_OPEN"
        return "MARKET_CLOSED", (latest.get("session") or {}).get("reason")
    if age is not None and age < -FUTURE_TOLERANCE_SECONDS:
        return "DATA_STALE", "FEED_TIMESTAMP_IN_FUTURE_CLOCK_SKEW"
    if age is None:
        return "DATA_UNAVAILABLE", "NO_SOURCE_QUOTE_TIMESTAMP"
    if age > stale_after:
        return "DATA_STALE", f"LAST_QUOTE_AGE_{int(age)}S"
    return "RUNNING", None


def _docs(session: Session, instrument: str | None) -> list[dict[str, Any]]:
    q = select(McxFuturesTrade)
    if instrument:
        q = q.where(McxFuturesTrade.instrument == instrument)
    return [json.loads(r.document) | {"state": r.state} for r in session.scalars(q).all()]


def _trade_row(d: dict[str, Any]) -> dict[str, Any]:
    keys = ("trade_id", "instrument", "contract", "direction", "version", "quantity_lots", "lot_size", "entry_at",
            "entry_price", "exit_at", "exit_price", "exit_reason", "gross_pnl", "fees", "slippage_cost", "net_pnl",
            "state", "journal_sequence", "rejection_reason")
    return {k: d.get(k) for k in keys} | {"mode": "PAPER"}


def perf(docs: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [d for d in docs if d["state"] == "CLOSED" and d.get("net_pnl") is not None]
    nets = [float(d["net_pnl"]) for d in closed]
    wins, losses = [n for n in nets if n > 0], [n for n in nets if n <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for d in sorted(closed, key=lambda d: d.get("exit_at") or ""):
        eq += float(d["net_pnl"])
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    unreal = sum(float((d.get("last_mark") or {}).get("unrealized_pnl") or 0) for d in docs if d["state"] == "OPEN")
    return {"trades": len(closed), "win_rate": len(wins) / len(nets) if nets else None,
            "expectancy": sum(nets) / len(nets) if nets else None,
            "avg_win": sum(wins) / len(wins) if wins else None, "avg_loss": sum(losses) / len(losses) if losses else None,
            "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
            "max_drawdown": dd if nets else None,
            "gross": sum(float(d.get("gross_pnl") or 0) for d in closed),
            "fees": sum(float(d.get("fees") or 0) for d in closed), "net": sum(nets),
            "realized": sum(nets), "unrealized": unreal if any(d["state"] == "OPEN" for d in docs) else None}


def build_status(sessions: sessionmaker[Session], now: datetime | None = None,
                 settings: McxFuturesSettings | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    settings = settings or McxFuturesSettings.from_env()
    today = now.astimezone(IST).date()
    calendar = SessionCalendar(holidays=frozenset(date.fromisoformat(day) for day in settings.holidays))
    out = []
    with sessions() as session:
        cursors = {c.instrument: c for c in session.scalars(select(McxFuturesCursor)).all()}
        for inst in INSTRUMENTS:
            c = cursors.get(inst)
            latest = json.loads(c.latest_json) if c else {}
            # The API process may have different env flags from the standalone worker.
            # Once the worker has persisted a mode, its cursor is the authority.
            legacy_worker = bool(c and c.observed_at and "paper_entries_enabled" not in latest)
            collector_enabled = latest.get("collector_enabled", legacy_worker or settings.enabled(inst))
            paper_entries_enabled = latest.get("paper_entries_enabled")
            # Earlier workers used capture time as a fallback in last_quote_at. Its
            # provenance cannot be recovered from the cursor, so never label it
            # as an exchange quote timestamp in the new API.
            state_latest = ({**latest, "feed": {**(latest.get("feed") or {}), "last_quote_at": None}}
                            if legacy_worker else latest)
            state, reason = derive_state(state_latest, collector_enabled, now, settings.stale_after_seconds, calendar)
            execution_state = ("PAPER_DISABLED" if not collector_enabled else
                               "LEGACY_MODE_UNVERIFIED" if legacy_worker else
                               "PAPER_ENABLED" if paper_entries_enabled else "COLLECT_ONLY")
            execution_reason = ("MCX_FUTURES_PAPER_OR_INSTRUMENT_FLAG_OFF" if not collector_enabled else
                                "WORKER_MODE_NOT_PERSISTED" if legacy_worker else
                                None if paper_entries_enabled else "MCX_FUTURES_TRADING_ENABLED_FALSE")
            all_docs = _docs(session, inst)
            docs = [d for d in all_docs if (d.get("entry_at") or d.get("at") or "")[:10] == str(today)]
            entered = [d for d in docs if d["state"] in ("OPEN", "CLOSED", "UNRESOLVED_EXPOSURE")
                       and d.get("entry_at")]
            closed = [d for d in all_docs if d["state"] == "CLOSED"
                      and (d.get("exit_at") or "")[:10] == str(today)]
            open_ = next((d for d in all_docs if d["state"] == "OPEN"), None)
            unresolved = [d for d in all_docs if d["state"] == "UNRESOLVED_EXPOSURE"]
            rej = [d for d in docs if d["state"] == "ENTRY_REJECTED"]
            persisted_feed = latest.get("feed") or {}
            feed = {"last_quote_at": None, "observed_at": latest.get("observed_at"),
                    "source_timestamp_available": None,
                    "bid": None, "ask": None, "bid_qty": None, "ask_qty": None,
                    "ltp": None, "spread_ticks": None, **persisted_feed,
                    "age_seconds": _age(persisted_feed.get("last_quote_at"), now),
                    "capture_latency_seconds": persisted_feed.get("age_seconds"),
                    "stale": state in ("DATA_STALE", "DATA_UNAVAILABLE") or not persisted_feed}
            if legacy_worker:
                feed["last_quote_at"] = None
            out.append({"instrument": inst, "state": state, "state_reason": reason,
                        "execution_state": execution_state, "execution_reason": execution_reason,
                        "open_position_count": sum(d["state"] == "OPEN" for d in all_docs),
                        "unresolved_exposure_count": len(unresolved),
                        "contract": json.loads(c.contract_json) or None if c else None,
                        "session": latest.get("session") or {"status": "CLOSED", "window": None, "next_open": None,
                                                              "last_entry_time": None, "forced_exit_time": None},
                        # feed.stale follows the server-derived state (age, clock skew), not only quote age.
                        "feed": feed,
                        "last_decision": latest.get("last_decision"), "warmup": latest.get("warmup"),
                        "position": (latest.get("position") or {
                            "direction": open_.get("direction"), "entry_at": open_.get("entry_at"),
                            "entry_price": open_.get("entry_price"), "quantity_lots": open_.get("quantity_lots"),
                            "mark_price": (open_.get("last_mark") or {}).get("mark_price"),
                            "mark_at": (open_.get("last_mark") or {}).get("at"),
                            "unrealized_pnl": (open_.get("last_mark") or {}).get("unrealized_pnl"),
                            "stop_price": open_.get("stop_price")}) if open_ else None,
                        "trades_today": len(entered),
                        "realized_pnl_today": sum(float(d.get("net_pnl") or 0) for d in closed),
                        "entries_rejected_today": len(rej),
                        "last_rejection": ({"at": rej[-1].get("at"), "reason": rej[-1].get("rejection_reason")}
                                           if rej else None),
                        "risk": latest.get("risk") or {"daily_loss_limit": None, "daily_realized": None,
                                                       "max_risk_per_trade": None, "entries_today": None,
                                                       "max_entries": None}})
        versions = {c.engine_version for c in cursors.values() if c.engine_version}
    return {"generated_at": now.isoformat(), "engine_version": ",".join(sorted(versions)) or "ACTS_V3_MCX_FUTURES",
            "mode": "PAPER", "calibration_status": "PRE_REGISTERED_UNVALIDATED", "instruments": out}


@router.get("/status")
def status(sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    return build_status(sessions)


@router.get("/trades")
def trades(instrument: str | None = Query(None), limit: int = Query(50, ge=1, le=200), before: str | None = None,
           sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    _instrument(instrument)
    with sessions() as session:
        docs = sorted(_docs(session, instrument), key=lambda d: d.get("entry_at") or d.get("at") or "", reverse=True)
    if before:
        docs = [d for d in docs if (d.get("entry_at") or d.get("at") or "") < before]
    page = docs[:limit]
    nxt = (page[-1].get("entry_at") or page[-1].get("at")) if len(docs) > limit else None
    return {"items": [_trade_row(d) for d in page], "next_before": nxt}


@router.get("/decisions")
def decisions(instrument: str | None = Query(None), limit: int = Query(100, ge=1, le=500),
              sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    _instrument(instrument)
    with sessions() as session:
        q = select(McxFuturesEvent).where(McxFuturesEvent.event_type.in_(("DECISION", "CYCLE", "RISK_BLOCK",
                                                                       "ENTRY_REJECTED")))
        if instrument:
            q = q.where(McxFuturesEvent.instrument == instrument)
        rows = session.scalars(q.order_by(McxFuturesEvent.id.desc()).limit(limit)).all()
    items = []
    for r in rows:
        p = json.loads(r.payload).get("payload") or {}  # stored row = full engine event; fields live in payload
        cyc = p.get("cycle") or {}
        points, maxima, missing = split_components(p.get("components"))
        status = p.get("status") or (p.get("event") if r.event_type == "CYCLE" else
                                     "RISK_BLOCK" if r.event_type == "RISK_BLOCK" else
                                     "ENTRY_REJECTED" if r.event_type == "ENTRY_REJECTED" else None)
        vetoes = list(p.get("vetoes") or []) + list(p.get("risk_blockers") or p.get("blockers") or [])
        if r.event_type == "ENTRY_REJECTED" and p.get("reason"):
            vetoes.append(p["reason"])
        items.append({"at": r.at, "instrument": r.instrument, "type": r.event_type, "status": status,
                      "direction": p.get("direction"), "score": p.get("score"), "components": points,
                      "component_max": maxima, "unavailable_inputs": missing, "vetoes": vetoes,
                      "cycle_id": cyc.get("cycle_id") or p.get("cycle_id"),
                      "cycle_state": cyc.get("cycle_state") or cyc.get("state"),
                      "transition": p.get("event") if r.event_type == "CYCLE" else None,
                      "evidence_at": p.get("bar_start") or r.at, "journal_sequence": r.sequence})
    return {"items": items}


@router.get("/performance")
def performance(instrument: str | None = Query(None),
                sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    _instrument(instrument)
    with sessions() as session:
        docs = _docs(session, instrument)
    by = {i: perf([d for d in docs if d.get("instrument") == i]) for i in INSTRUMENTS if instrument in (None, i)}
    return {"by_instrument": by, "overall": perf(docs), "mode": "PAPER"}
