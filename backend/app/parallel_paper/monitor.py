"""Compact, read-only Phase 15 paper monitor projection."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timedelta
import json
from math import isfinite
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.parallel_paper.config import STRATEGIES
from app.paper import expiry
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperEvent, ParallelPaperTrade
from app.parallel_paper.orb_evidence import ORB_MINUTES, orb_features
from app.scalper.models import ScalperMarketSnapshotRecord
from app.scalper.repository import IST, _aware


NAMES = {"ACTS_V2": "ACTS V2", "ORB_15": "ORB 15", "ORB_30": "ORB 30",
         "ORB_60": "ORB 60", "EMA_PULLBACK": "EMA Pullback",
         "SUPERTREND_RSI_VOLUME": "Supertrend + RSI + Volume"}
ORDER = ("ACTS_V2", *STRATEGIES)
ACTIVE_EXPOSURE = {"OPEN", "UNRESOLVED_EXPOSURE"}


def _on_day(value: str | None, day) -> bool:
    return bool(value and datetime.fromisoformat(value).astimezone(IST).date() == day)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _decision_features(latest: dict) -> dict:
    """Allowlisted persisted diagnostics; no strategy evaluation or inferred states."""
    raw_features = latest.get("features")
    features: dict = raw_features if isinstance(raw_features, dict) else {}
    names = ("ema_fast", "ema_slow", "pullback_anchor", "pullback_depth_bps",
             "resumption_strength_bps", "supertrend", "supertrend_direction", "rsi",
             "volume_ratio", "volume_source", "bar_end")

    def scalar(value: Any) -> Any:
        if value is None or isinstance(value, (str, bool)):
            return value
        return value if isinstance(value, (int, float)) and isfinite(value) else None

    result = {key: scalar(features.get(key)) for key in names}
    for key in ("opportunity_score", "score_band", "market_state", "alpha_state"):
        result[key] = scalar(latest.get(key))
    return result


def _trade(row: Any, doc: dict, mark_available: bool) -> dict:
    outcome = doc.get("final_outcome") or {}
    candidate = doc.get("candidate") or {}
    short = candidate.get("short_leg") or {}
    long = candidate.get("long_leg") or {}
    entry_at, exit_at = doc.get("entry_timestamp"), doc.get("exit_timestamp")
    marked_open = row.state == "OPEN" and mark_available and _number(doc.get("gross_mark_rupees")) is not None
    holding = _number(doc.get("holding_seconds"))
    if holding is None and entry_at and exit_at:
        holding = (datetime.fromisoformat(exit_at) - datetime.fromisoformat(entry_at)).total_seconds()
    return {
        "trade_id": row.id, "state": row.state,
        "expiry_terminalization": expiry.is_unpriced(doc),
        "exit_pricing_status": doc.get("exit_pricing_status"),
        "terminalized_at": doc.get("terminalized_at"),
        "terminal_status": expiry.LABEL if expiry.is_unpriced(doc) else None, "direction": doc.get("direction"),
        "decision_timestamp": doc.get("decision_timestamp"), "entry_timestamp": entry_at,
        "exit_timestamp": exit_at, "short_strike": doc.get("short_strike", short.get("strike")),
        "hedge_strike": doc.get("hedge_strike", long.get("strike")),
        "entry_credit": _number(doc.get("entry_credit")),
        "current_debit": _number(doc.get("current_debit")) if marked_open else None,
        "mark_quality_warning": doc.get("mark_quality_warning") if marked_open else None,
        "exit_debit": _number(doc.get("exit_debit")),
        "realized_gross_pnl": _number(outcome.get("gross_rupees")) if row.state == "CLOSED" else None,
        "current_unrealized_gross_pnl": _number(doc.get("gross_mark_rupees")) if marked_open else None,
        "mfe": _number(doc.get("mfe")), "mae": _number(doc.get("mae")),
        "holding_seconds": holding, "entry_reason": doc.get("entry_reason"),
        "exit_reason": doc.get("exit_reason"),
        "exit_policy_version": doc.get("exit_policy_version"),
        "best_gross_points": _number(doc.get("best_gross_points")),
        "mirror_diagnostics": doc.get("mirror_diagnostics"),
        "false_breakout": bool(doc["false_breakout_first_at"]) if "false_breakout_first_at" in doc else None,
        "strategy_features": orb_features(getattr(row, "strategy_id", ""), doc.get("strategy_features"),
                                          doc.get("decision_timestamp")),
    }


def _unavailable(strategy: str) -> dict:
    return {"strategy_id": strategy, "display_name": NAMES[strategy], "trades_today": None,
            "wins_today": None, "losses_today": None, "entry_rejections_today": None,
            "latest_rejection_reason": None, "realized_gross_pnl_today": None,
            "unrealized_gross_pnl": None, "open_position_count": None,
            "unresolved_exposure_count": None, "expired_unpriced_count": None,
            "latest_status": None, "latest_blocker": None, "latest_timestamp": None,
            "accounting_status": "GROSS_ONLY", "today_trades": [], "orb_range": None}


def _project(strategy: str, cursor: Any, rows: Sequence[Any], events: Sequence[Any], at: datetime,
             future_price: float | None = None) -> dict:
    if cursor is None or cursor.observed_at is None:
        return _unavailable(strategy)
    day = at.astimezone(IST).date()
    latest = json.loads(cursor.latest_json)
    if (not isinstance(latest, dict) or not latest
            or (strategy == "ACTS_V2" and latest.get("strategy_version") != "ACTS_V2")):
        return _unavailable(strategy)
    rejections: list[str | None] = []
    failed_marks: dict[str, bool] = {}
    blocked_signals: set[tuple] = set()
    rejected_trades: set[str] = set()
    for event in events:
        payload = json.loads(event.payload)
        if strategy == "ACTS_V2":
            if payload.get("strategy_version") != "ACTS_V2":
                continue
            if event.event_type == "ACTS_ENTRY_REJECTED":
                if event.trade_id is None or event.trade_id not in rejected_trades:
                    rejections.append(payload.get("reason"))
                if event.trade_id is not None:
                    rejected_trades.add(event.trade_id)
            elif event.event_type == "ACTS_MARK" and event.trade_id:
                failed_marks[event.trade_id] = payload.get("available") is False
        else:
            if event.event_type == "OBSERVATION":
                decision = payload.get("decision") or {}
                if decision.get("status") == "ENTRY_REJECTED":
                    breakout = (decision.get("features") or {}).get("breakout_timestamp")
                    identity = (strategy, day.isoformat(), decision.get("direction"), breakout)
                    if not strategy.startswith("ORB_") or not breakout or identity not in blocked_signals:
                        rejections.append(next(iter(decision.get("blockers") or []), None))
                    if strategy.startswith("ORB_") and breakout:
                        blocked_signals.add(identity)
            elif event.event_type == "ENTRY_REJECTED":
                if event.trade_id is None or event.trade_id not in rejected_trades:
                    rejections.append(payload.get("reason"))
                if event.trade_id is not None:
                    rejected_trades.add(event.trade_id)
            elif event.event_type == "MARK" and event.trade_id:
                failed_marks[event.trade_id] = payload.get("available") is False

    entered: list[dict] = []
    closed_today: list[dict] = []
    active: list[dict] = []
    unresolved_count = expired_count = 0
    for row in rows:
        doc = json.loads(row.document)
        if strategy == "ACTS_V2" and doc.get("strategy_version") != "ACTS_V2":
            continue
        mark_available = not failed_marks.get(row.id, False)
        unresolved_count += row.state == "UNRESOLVED_EXPOSURE"
        expired_count += row.state == "CLOSED" and expiry.is_unpriced(doc)
        if row.state in ACTIVE_EXPOSURE:
            active.append(_trade(row, doc, mark_available))
        if row.state == "CLOSED" and not expiry.is_unpriced(doc) and _on_day(doc.get("exit_timestamp"), day):
            closed_today.append(_trade(row, doc, mark_available))
        if doc.get("entry_timestamp") and _on_day(doc["entry_timestamp"], day):
            entered.append(_trade(row, doc, mark_available))

    realized_values = [trade["realized_gross_pnl"] for trade in closed_today]
    mark_values = [trade["current_unrealized_gross_pnl"] for trade in active]
    realized = sum(realized_values) if all(value is not None for value in realized_values) else None
    unrealized = sum(mark_values) if all(value is not None for value in mark_values) else None
    latest_blocker = (latest.get("primary_blocker") if strategy == "ACTS_V2" else
                      next(iter(latest.get("blockers") or []), None))
    orb_range = orb_features(strategy, latest.get("features"), cursor.observed_at)
    if orb_range is not None:
        orb_range.update(current_future_price=future_price, status=latest.get("status"),
                         blocker=latest_blocker, decision_reason=latest.get("entry_reason"))
    return {"strategy_id": strategy, "display_name": NAMES[strategy],
            "trades_today": len(entered),
            "wins_today": sum(value > 0 for value in realized_values) if realized is not None else None,
            "losses_today": sum(value < 0 for value in realized_values) if realized is not None else None,
            "entry_rejections_today": len(rejections),
            "latest_rejection_reason": rejections[-1] if rejections else None,
            "realized_gross_pnl_today": realized, "unrealized_gross_pnl": unrealized,
            "open_position_count": sum(trade["state"] == "OPEN" for trade in active),
            "unresolved_exposure_count": unresolved_count, "expired_unpriced_count": expired_count,
            "latest_status": latest.get("status"),
            "direction": latest.get("direction"), "decision_features": _decision_features(latest),
            "latest_blocker": latest_blocker,
            "latest_timestamp": latest.get("timestamp") or cursor.observed_at,
            "orb_range": orb_range,
            "accounting_status": "GROSS_ONLY",
            "today_trades": sorted(entered, key=lambda trade: (trade["entry_timestamp"], trade["trade_id"]))}


def summary(sessions: sessionmaker[Session], at: datetime) -> dict:
    """Read ORM projections and immutable events, without running a paper engine."""
    at = at.astimezone(IST)
    start = datetime.combine(at.date(), time.min, IST)
    end = start + timedelta(days=1)
    with sessions() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        parallel_cursors = {row.strategy_id: row for row in session.scalars(select(ParallelPaperCursor))}
        snapshot_ids = {cursor.snapshot_id for strategy, cursor in parallel_cursors.items()
                        if strategy in ORB_MINUTES and cursor.snapshot_id is not None}
        futures = {row.id: row for row in session.execute(select(
            ScalperMarketSnapshotRecord.id, ScalperMarketSnapshotRecord.captured_at,
            ScalperMarketSnapshotRecord.nifty_future).where(ScalperMarketSnapshotRecord.id.in_(snapshot_ids)))}
        parallel_rows = session.scalars(select(ParallelPaperTrade)).all()
        parallel_events = session.scalars(select(ParallelPaperEvent).join(
            ScalperMarketSnapshotRecord, ParallelPaperEvent.snapshot_id == ScalperMarketSnapshotRecord.id
        ).where(ScalperMarketSnapshotRecord.captured_at >= start,
                ScalperMarketSnapshotRecord.captured_at < end,
                ParallelPaperEvent.event_type.in_(("OBSERVATION", "ENTRY_REJECTED", "MARK"))).order_by(
                    ParallelPaperEvent.strategy_id, ParallelPaperEvent.sequence)).all()
        acts_cursor = session.get(ActsPaperCursor, 1)
        acts_rows = session.scalars(select(ActsPaperTrade)).all()
        acts_events = session.scalars(select(ActsPaperEvent).join(
            ScalperMarketSnapshotRecord, ActsPaperEvent.snapshot_id == ScalperMarketSnapshotRecord.id
        ).where(ScalperMarketSnapshotRecord.captured_at >= start,
                ScalperMarketSnapshotRecord.captured_at < end,
                ActsPaperEvent.event_type.in_(("ACTS_ENTRY_REJECTED", "ACTS_MARK"))).order_by(
                    ActsPaperEvent.sequence)).all()

        strategies = []
        for strategy in ORDER:
            cursor = acts_cursor if strategy == "ACTS_V2" else parallel_cursors.get(strategy)
            rows = acts_rows if strategy == "ACTS_V2" else [
                row for row in parallel_rows if row.strategy_id == strategy]
            events = acts_events if strategy == "ACTS_V2" else [
                event for event in parallel_events if event.strategy_id == strategy]
            try:
                snapshot = futures.get(cursor.snapshot_id) if cursor and strategy in ORB_MINUTES else None
                future_price = (float(snapshot.nifty_future) if cursor is not None and cursor.observed_at
                                and snapshot is not None
                                and snapshot.nifty_future is not None
                                and _aware(snapshot.captured_at) == datetime.fromisoformat(cursor.observed_at)
                                else None)
                strategies.append(_project(strategy, cursor, rows, events, at, future_price))
            except (KeyError, TypeError, ValueError):
                strategies.append(_unavailable(strategy))

    realized = [row["realized_gross_pnl_today"] for row in strategies]
    unrealized = [row["unrealized_gross_pnl"] for row in strategies]
    open_counts = [row["open_position_count"] for row in strategies]
    trade_counts = [row["trades_today"] for row in strategies]
    unresolved_counts = [row["unresolved_exposure_count"] for row in strategies]
    expired_counts = [row["expired_unpriced_count"] for row in strategies]
    realized_total = sum(realized) if all(value is not None for value in realized) else None
    unrealized_total = sum(unrealized) if all(value is not None for value in unrealized) else None
    return {"mode": "PAPER", "as_of": at.isoformat(), "accounting_status": "GROSS_ONLY",
            "totals": {"realized_gross_pnl_today": realized_total,
                       "unrealized_gross_pnl": unrealized_total,
                       "combined_gross_pnl": (
                           realized_total + unrealized_total
                           if realized_total is not None and unrealized_total is not None else None),
                       "open_positions": sum(open_counts) if all(value is not None for value in open_counts) else None,
                       "unresolved_exposures": (sum(unresolved_counts)
                                                if all(v is not None for v in unresolved_counts) else None),
                       "expired_unpriced_count": (sum(expired_counts)
                                                  if all(v is not None for v in expired_counts) else None),
                       "trades_today": (sum(trade_counts)
                                        if all(value is not None for value in trade_counts) else None)},
            "strategies": strategies}
