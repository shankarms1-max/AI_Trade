"""Read-only AST_FUTURES_V1_DIRECT status. No broker construction, initialization or journal writes."""

from __future__ import annotations

from datetime import datetime, time
import json
from pathlib import Path

from sqlalchemy import or_, select

from app.adaptive_supertrend.direct import SESSION_EXIT_TIME, VERSION
from app.adaptive_supertrend.direct.engine import ACTIVE
from app.adaptive_supertrend.direct.models import DirectCursor, DirectTrade
from app.adaptive_supertrend.direct.policy import DirectPolicy
from app.scalper.repository import IST

HEARTBEAT_STALE_SECONDS = 120


def read_heartbeat(path: Path | None, at: datetime) -> dict:
    if path is None or not path.exists():
        return {"worker": "STOPPED", "heartbeat": None}
    try:
        beat = json.loads(path.read_text())
        age = (at - datetime.fromisoformat(beat["at"])).total_seconds()
    except (OSError, ValueError, KeyError, TypeError):
        return {"worker": "UNKNOWN", "heartbeat": None}
    return {"worker": "RUNNING" if 0 <= age <= HEARTBEAT_STALE_SECONDS else "STOPPED", "heartbeat": beat,
            "heartbeat_age_seconds": age}


def _summary(doc: dict) -> dict:
    keys = (
        "trade_id", "state", "side", "signal_timestamp", "entry_timestamp", "entry_price", "exit_timestamp",
        "exit_price", "exit_reason", "exit_category", "gross_points", "net_research_points", "rejection_reason",
        "unresolved_reason", "future_instrument_id", "future_expiry", "lot_size",
    )
    return {key: doc.get(key) for key in keys}


def report(
    sessions,
    at: datetime,
    policy: DirectPolicy | None,
    *,
    heartbeat: Path | None = None,
    kill_switch: bool = False,
    enabled: bool | None = None,
) -> dict:
    at = at.astimezone(IST)
    with sessions() as session:
        cursor = session.get(DirectCursor, 1)
        if cursor is None:
            return {"strategy_id": VERSION, "status": "AST_MIGRATION_REQUIRED"}
        rows = list(
            session.scalars(
                select(DirectTrade).where(
                    or_(
                        DirectTrade.state.in_(ACTIVE),
                        DirectTrade.trading_date == at.date(),
                        DirectTrade.closed_date == at.date(),
                    )
                )
            )
        )
        closed = [json.loads(doc) for doc in session.scalars(
            select(DirectTrade.document).where(DirectTrade.state == "CLOSED")
        )]
    docs = [json.loads(row.document) for row in rows]
    latest = json.loads(cursor.latest_json)
    today = [doc for doc in docs if doc["trading_date"] == at.date().isoformat()]
    closed_today = [doc for doc in closed if doc.get("closed_date") == at.date().isoformat()]
    active = [doc for doc in docs if doc["state"] in ACTIVE]
    if active and active[0]["state"] == "UNRESOLVED_EXPOSURE":
        forced = "UNRESOLVED_EXPOSURE"
    elif at.time().replace(tzinfo=None) < SESSION_EXIT_TIME:
        forced = "SCHEDULED_15:20_IST"
    elif active:
        forced = "PENDING"
    else:
        forced = "COMPLETE_FLAT"
    friction = [doc.get("net_research_points") for doc in closed]
    return {
        "strategy_id": VERSION,
        "strategy_version": VERSION,
        "mode": "PAPER",
        "live_trading": False,
        **read_heartbeat(heartbeat, at),
        "worker_enabled": enabled,
        "kill_switch": kill_switch,
        "policy_hash": policy.policy_hash if policy else None,
        "fixed_parameters": {"atr_period": 22, "multiplier": 3.9, "regime": "NONE", "optimizer": "DISABLED"},
        "trading_date": at.date().isoformat(),
        "in_session": at.weekday() < 5 and time(9, 15) <= at.time().replace(tzinfo=None) < time(15, 30),
        "futures_instrument": latest.get("futures"),
        "last_observation_at": cursor.observed_at,
        "last_completed_bar": latest.get("last_completed_bar"),
        "opening_range": latest.get("opening_range"),
        "ast": latest.get("ast"),
        "status": latest.get("status"),
        "blockers": latest.get("blockers"),
        "fill_blocker": latest.get("fill_blocker"),
        "position": _summary(active[0]) if active else None,
        "mark": latest.get("mark"),
        "trades_today": [_summary(doc) for doc in today],
        "today_gross_points": sum(doc["gross_points"] for doc in closed_today),
        "cumulative_closed_trades": len(closed),
        "cumulative_gross_points": sum(doc["gross_points"] for doc in closed),
        "cumulative_net_research_points": (
            sum(friction) if closed and all(value is not None for value in friction) else None
        ),
        "forced_exit_status": forced,
        "journal_sequence": cursor.sequence,
        "journal_head": cursor.head_hash,
    }
