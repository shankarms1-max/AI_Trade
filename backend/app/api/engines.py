"""GET /api/engines/status — one truthful status row per trading/research engine visible to THIS deployment.

States are derived server-side from journal timestamps and the exchange session, never from cached "OK" flags:
RUNNING (in session, fresh observations), MARKET_CLOSED, DATA_STALE (in session, last observation older than the
threshold), IDLE (enabled, nothing recorded yet), ERROR, DISABLED, NOT_CONFIGURED (engine lives in another stack).
Read-only: no engine is constructed and no broker client is touched.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import os
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.acts_paper.models import ActsPaperCursor, ActsPaperTrade
from app.api.mcx_futures import build_status as mcx_status
from app.db.session import get_session_factory
from app.parallel_paper.models import ParallelPaperCursor
from app.scalper.models import ScalperMarketSnapshotRecord

IST = ZoneInfo("Asia/Kolkata")
router = APIRouter(prefix="/api/engines", tags=["engines"])
NIFTY_OPEN, NIFTY_CLOSE = time(9, 15), time(15, 30)
STALE_SECONDS = 60


def _holidays() -> set[date]:
    out = set()
    for part in os.environ.get("COLLECTOR_HOLIDAYS", "").split(","):
        try:
            out.add(date.fromisoformat(part.strip()))
        except ValueError:
            continue
    return out


def nifty_session_open(now: datetime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < 5 and local.date() not in _holidays() and NIFTY_OPEN <= local.time() <= NIFTY_CLOSE


def freshness_state(observed_at: str | None, now: datetime, in_session: bool) -> tuple[str, str | None]:
    if not in_session:
        return "MARKET_CLOSED", None
    if not observed_at:
        return "DATA_STALE", "NO_OBSERVATION_RECORDED"
    age = (now - datetime.fromisoformat(observed_at)).total_seconds()
    if age > STALE_SECONDS:
        return "DATA_STALE", f"LAST_OBSERVATION_{int(age)}S_AGO"
    return "RUNNING", None


def _acts(session: Session, now: datetime) -> list[dict[str, Any]]:
    cursor = session.get(ActsPaperCursor, 1)
    if cursor is None:
        return []
    latest = json.loads(cursor.latest_json or "{}")
    version = latest.get("strategy_version") or "ACTS"
    state, reason = freshness_state(cursor.observed_at, now, nifty_session_open(now))
    today = str(now.astimezone(IST).date())
    closed_today, open_pos, trades_today = 0.0, None, 0
    for row in session.scalars(select(ActsPaperTrade).where(ActsPaperTrade.state.in_(("CLOSED", "OPEN")))).all():
        doc = json.loads(row.document)
        if doc.get("strategy_version") != version:
            continue
        if row.state == "CLOSED" and (doc.get("exit_timestamp") or "")[:10] == today and doc.get("final_outcome"):
            closed_today += float(doc["final_outcome"].get("gross_rupees") or 0)
            trades_today += 1
        if row.state == "OPEN":
            cand = doc.get("candidate") or {}
            open_pos = {"direction": doc.get("direction"),
                        "contract": f"{(cand.get('short_leg') or {}).get('trading_symbol')} / "
                                    f"{(cand.get('long_leg') or {}).get('trading_symbol')}",
                        "quantity": doc.get("lots"), "entry_price": doc.get("entry_credit"),
                        "mark_price": doc.get("current_debit"), "mark_at": cursor.observed_at,
                        "unrealized_pnl": doc.get("gross_mark_rupees")}
    return [{"id": f"nifty-{version.lower()}", "label": f"NIFTY {version.replace('_', ' ')}", "family": "ACTS",
             "version": version, "instrument": "NIFTY", "mode": "PAPER", "state": state, "state_reason": reason,
             "last_update": cursor.observed_at,
             "last_decision": {"at": latest.get("timestamp"), "status": latest.get("status"),
                               "direction": latest.get("direction"), "score": latest.get("opportunity_score"),
                               "primary_blocker": latest.get("primary_blocker")},
             "active_position": open_pos, "trades_today": trades_today, "realized_pnl_today": closed_today,
             "unrealized_pnl": open_pos["unrealized_pnl"] if open_pos else None,
             "pnl_basis": "GROSS_ENGINE_RECORD", "detail_href": "/nifty"}]


def _parallel(session: Session, now: datetime) -> list[dict[str, Any]]:
    out = []
    for c in session.scalars(select(ParallelPaperCursor)).all():
        state, reason = freshness_state(c.observed_at, now, nifty_session_open(now))
        if state == "DATA_STALE" and c.observed_at and (now - datetime.fromisoformat(c.observed_at)).days >= 1:
            state, reason = "DISABLED", "NO_OBSERVATION_FOR_OVER_A_DAY_RETIRED_OR_STOPPED"
        out.append({"id": f"parallel-{c.strategy_id.lower()}", "label": c.strategy_id.replace("_", " "),
                    "family": "PARALLEL", "version": c.strategy_id, "instrument": "NIFTY", "mode": "PAPER",
                    "state": state, "state_reason": reason, "last_update": c.observed_at, "last_decision": None,
                    "active_position": None, "trades_today": None, "realized_pnl_today": None,
                    "unrealized_pnl": None, "detail_href": None})
    return out


def _research(session: Session, now: datetime) -> list[dict[str, Any]]:
    last = session.scalar(select(func.max(ScalperMarketSnapshotRecord.captured_at)))
    at = last.astimezone(timezone.utc).isoformat() if last else None
    state, reason = freshness_state(at, now, nifty_session_open(now))
    return [{"id": "research-nifty-capture", "label": "NIFTY market capture", "family": "RESEARCH", "version": None,
             "instrument": "NIFTY", "mode": "RESEARCH", "state": state, "state_reason": reason, "last_update": at,
             "last_decision": None, "active_position": None, "trades_today": None, "realized_pnl_today": None,
             "unrealized_pnl": None, "detail_href": "/system"}]


def _mcx(sessions: sessionmaker[Session], now: datetime, settings=None) -> list[dict[str, Any]]:
    out = []
    for row in mcx_status(sessions, now, settings)["instruments"]:
        d = row.get("last_decision") or {}
        pos = row.get("position")
        out.append({"id": f"mcx-{row['instrument'].lower()}", "label": f"ACTS V3 {row['instrument']}",
                    "family": "MCX_FUTURES", "version": "ACTS_V3_MCX_FUTURES", "instrument": row["instrument"],
                    "mode": "PAPER", "state": row["state"], "state_reason": row["state_reason"],
                    "last_update": row["feed"].get("last_quote_at"),
                    "last_decision": {"at": d.get("at"), "status": d.get("status"), "direction": d.get("direction"),
                                      "score": d.get("score"),
                                      "primary_blocker": (d.get("vetoes") or [None])[0] if d else None} if d else None,
                    "active_position": None if not pos else {
                        "direction": pos.get("direction"), "contract": (row.get("contract") or {}).get("trading_symbol"),
                        "quantity": pos.get("quantity_lots"), "entry_price": pos.get("entry_price"),
                        "mark_price": pos.get("mark_price"), "mark_at": pos.get("mark_at"),
                        "unrealized_pnl": pos.get("unrealized_pnl")},
                    "trades_today": row["trades_today"], "realized_pnl_today": row["realized_pnl_today"],
                    "unrealized_pnl": pos.get("unrealized_pnl") if pos else None,
                    "detail_href": f"/mcx-futures/{row['instrument']}"})
    return out


def _ast() -> dict[str, Any]:
    return {"id": "ast-futures-v1-direct", "label": "AST Futures (NIFTY)", "family": "AST",
            "version": "AST_FUTURES_V1_DIRECT", "instrument": "NIFTY", "mode": "PAPER", "state": "NOT_CONFIGURED",
            "state_reason": "RUNS_IN_ISOLATED_AST_STACK_NOT_VISIBLE_FROM_THIS_DEPLOYMENT", "last_update": None,
            "last_decision": None, "active_position": None, "trades_today": None, "realized_pnl_today": None,
            "unrealized_pnl": None, "detail_href": "/ast-futures"}


def build(sessions: sessionmaker[Session], now: datetime | None = None, mcx_settings=None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    engines: list[dict[str, Any]] = []
    errors = []
    for name, fn in (("acts", lambda s: _acts(s, now)), ("parallel", lambda s: _parallel(s, now)),
                     ("research", lambda s: _research(s, now))):
        try:
            with sessions() as session:
                engines += fn(session)
        except SQLAlchemyError:
            errors.append(name)
            engines.append({"id": f"{name}-unavailable", "label": name.upper(), "family": "RESEARCH"
                            if name == "research" else "PARALLEL" if name == "parallel" else "ACTS",
                            "version": None, "instrument": "NIFTY", "mode": None, "state": "ERROR",
                            "state_reason": "PROJECTION_QUERY_FAILED", "last_update": None, "last_decision": None,
                            "active_position": None, "trades_today": None, "realized_pnl_today": None,
                            "unrealized_pnl": None, "detail_href": None})
    try:
        engines += _mcx(sessions, now, mcx_settings)
    except SQLAlchemyError:
        errors.append("mcx")
    engines.append(_ast())
    return {"generated_at": now.isoformat(), "timezone": "Asia/Kolkata", "engines": engines,
            "projection_errors": errors}


@router.get("/status")
def status(sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    return build(sessions)
