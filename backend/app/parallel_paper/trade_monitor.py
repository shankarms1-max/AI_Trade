"""Read-only, bounded views over the existing Phase 15 trade projections/events.

No paper engine or market-data client is imported here. Date filters refer to
the decision observation's IST date, not the date of the eventual entry/exit.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
from math import isfinite
from typing import Any

from sqlalchemy import JSON, cast, func, literal, select, text, type_coerce, union_all
from sqlalchemy.orm import Session, sessionmaker

from app.acts_paper.models import ActsPaperEvent, ActsPaperTrade
from app.parallel_paper.models import ParallelPaperEvent, ParallelPaperTrade
from app.parallel_paper.monitor import NAMES, ORDER
from app.paper import expiry
from app.scalper.models import ScalperMarketSnapshotRecord
from app.scalper.repository import IST, _aware

ACTIVE = ("PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE")
STATES = (*ACTIVE, "CLOSED", "ENTRY_REJECTED")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _fields(value: Any, names: tuple[str, ...]) -> dict:
    """Project named scalar evidence only; never return an entire stored object."""
    return {key: item for key, item in _dict(value).items() if key in names and
            (item is None or isinstance(item, (str, bool)) or _number(item) is not None)}


def _quotes(value: Any) -> dict:
    # Executable-pair evidence stores the original quote inside `quote`.
    return {leg: _fields({**_dict(_dict(evidence).get("quote")),
                         **_dict(_dict(evidence).get("validation")), **_dict(evidence)}, (
        "exchange", "instrument_token", "trading_symbol", "expiry", "strike", "option_type",
        "side", "fill_price", "bid", "ask", "bid_quantity", "ask_quantity", "depth_unit",
        "source_market_timestamp", "tick_size", "spread_pct", "wide_spread", "quality_warning"))
        for leg, evidence in _dict(value).items() if leg in ("short", "long")}


def _diagnostics(value: Any) -> list[dict]:
    diagnostics = _dict(value)
    evidence = _dict(diagnostics.get("evidence"))
    result = []
    for name, timestamp in diagnostics.items():
        if not isinstance(timestamp, str):
            continue
        try:
            datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        detail = _dict(evidence.get(name))
        signal = _dict(detail.get("signal"))
        result.append({"name": name, "observed_at": timestamp,
                       **_fields(detail, ("mark_available", "gross_points", "gross_pnl",
                                          "mfe", "mae", "best_gross_points")),
                       **_fields(signal, ("opportunity_score", "score_band", "status", "direction"))})
    return sorted(result, key=lambda item: (item["observed_at"], item["name"]))


def _marks(session: Session, rows: list[Any]) -> dict[tuple[str, str], dict]:
    """Latest mark attempt per returned trade, across all dates (bounded result)."""
    result = {}
    model: Any
    for model, acts in ((ActsPaperEvent, True), (ParallelPaperEvent, False)):
        ids = [row.trade_id for row in rows if (row.strategy_id == "ACTS_V2") == acts]
        if not ids:
            continue
        query = select(
            literal("ACTS_V2").label("strategy_id") if acts else model.strategy_id,
            model.trade_id, model.payload, model.snapshot_id,
            func.row_number().over(partition_by=(model.trade_id,) if acts else
                                   (model.strategy_id, model.trade_id),
                                   order_by=model.sequence.desc()).label("rank"),
        ).where(model.trade_id.in_(ids), model.event_type == ("ACTS_MARK" if acts else "MARK")).subquery()
        for mark in session.execute(select(query, ScalperMarketSnapshotRecord.captured_at).join(
                ScalperMarketSnapshotRecord, ScalperMarketSnapshotRecord.id == query.c.snapshot_id
        ).where(query.c.rank == 1)):
            payload = _dict(json.loads(mark.payload))
            result[(mark.strategy_id, mark.trade_id)] = {
                "available": payload.get("available") is True,
                "timestamp": _aware(mark.captured_at), "snapshot_id": mark.snapshot_id,
                "reason": payload.get("reason"), "quotes": _quotes(payload.get("quotes")),
                "quality_warning": payload.get("mark_quality_warning")}
    return result


def _project(row: Any, mark: dict | None, at: datetime) -> dict:
    doc = _dict(json.loads(row.document))
    candidate = _dict(doc.get("candidate"))
    short, long = _dict(candidate.get("short_leg")), _dict(candidate.get("long_leg"))
    policy = _dict(doc.get("policy"))
    execution = _dict(policy.get("execution") or policy.get("config"))
    mark = mark or {}
    mark_at = mark.get("timestamp")
    available = bool(row.state == "OPEN" and mark.get("available") and mark_at is not None
                     and mark_at <= at
                     and mark_at.astimezone(IST).date() == at.date()
                     and mark.get("snapshot_id") == doc.get("last_snapshot_id")
                     and _number(doc.get("gross_mark_rupees")) is not None
                     and _number(doc.get("current_debit")) is not None)
    short_strike = _number(doc.get("short_strike", short.get("strike")))
    hedge_strike = _number(doc.get("hedge_strike", long.get("strike")))
    width = _number(candidate.get("spread_width", doc.get("spread_width")))
    if width is None and short_strike is not None and hedge_strike is not None:
        width = abs(short_strike - hedge_strike)
    holding = _number(doc.get("holding_seconds"))
    entry, exit_at = doc.get("entry_timestamp"), doc.get("exit_timestamp")
    if entry and (row.state in ("OPEN", "UNRESOLVED_EXPOSURE") or
                  row.state == "CLOSED" and exit_at is not None and holding is None):
        holding = max(0, ((datetime.fromisoformat(exit_at) if exit_at else at) -
                          datetime.fromisoformat(entry)).total_seconds())
    decision = _dict(doc.get("decision"))
    rejected = _dict(doc.get("candidate_rejections") or decision.get("candidate_rejections"))
    return {
        "trade_id": row.trade_id, "strategy_id": row.strategy_id,
        "expiry_terminalization": expiry.is_unpriced(doc),
        "exit_pricing_status": doc.get("exit_pricing_status"),
        "terminalized_at": doc.get("terminalized_at"),
        "previous_unresolved_reason": doc.get("previous_unresolved_reason"),
        "terminal_status": expiry.LABEL if expiry.is_unpriced(doc) else None,
        "display_name": NAMES[row.strategy_id], "state": row.state, "direction": doc.get("direction"),
        "decision_timestamp": doc.get("decision_timestamp"), "entry_timestamp": entry,
        "exit_timestamp": exit_at, "short_strike": short_strike, "hedge_strike": hedge_strike,
        "spread_width": width, "lot_size": _number(doc.get("lot_size")),
        "lots": _number(doc.get("lots", policy.get("lots", execution.get("lots")))),
        "entry_credit": _number(doc.get("entry_credit")),
        "current_debit": _number(doc.get("current_debit")) if available else None,
        "exit_debit": _number(doc.get("exit_debit")) if row.state == "CLOSED" else None,
        "realized_gross_pnl": (_number(_dict(doc.get("final_outcome")).get("gross_rupees"))
                               if row.state == "CLOSED" else None),
        "current_unrealized_gross_pnl": _number(doc.get("gross_mark_rupees")) if available else None,
        "mfe": _number(doc.get("mfe")), "mae": _number(doc.get("mae")),
        "best_gross_points": _number(doc.get("best_gross_points")), "holding_seconds": holding,
        "entry_reason": doc.get("entry_reason"), "exit_reason": doc.get("exit_reason"),
        "exit_policy_version": doc.get("exit_policy_version"), "policy_hash": doc.get("policy_hash"),
        "policy_allowed_widths": [value for value in execution.get("allowed_widths", [])
                                  if _number(value) is not None],
        "rejection_reason": doc.get("rejection_reason"),
        "unresolved_reason": doc.get("unresolved_reason") if row.state == "UNRESOLVED_EXPOSURE" else None,
        "latest_mark_timestamp": mark_at.isoformat() if mark_at else None,
        "mark_available": available, "mark_unavailable_reason": None if available else (
            "CONTRACT_EXPIRED_UNPRICED" if expiry.is_unpriced(doc) else
            mark.get("reason") or doc.get("unresolved_reason") or "NO_CURRENT_EXACT_MARK"),
        "mark_quality_warning": mark.get("quality_warning") if available else None,
        "accounting_status": "GROSS_ONLY",
        "opportunity_score": _number(doc.get("opportunity_score", decision.get("opportunity_score"))),
        "score_band": doc.get("score_band", decision.get("score_band")),
        "candidate_rejections": {key: value for key, value in rejected.items() if _number(value) is not None},
        "mirror_diagnostics": _diagnostics(doc.get("mirror_diagnostics")),
        "entry_quotes": _quotes(doc.get("entry_quotes")), "exit_quotes": _quotes(doc.get("exit_quotes")),
        "latest_mark_quotes": mark.get("quotes", {}) if available else {},
        "false_breakout_first_at": doc.get("false_breakout_first_at"),
    }


def trades(sessions: sessionmaker[Session], at: datetime, *, positions: bool = False,
           day: date | None = None, strategy: str | None = None, state: str | None = None,
           limit: int = 100, offset: int = 0) -> dict:
    """One bounded page, no writes, recomputation of market evidence, or engines."""
    if (strategy is not None and strategy not in ORDER) or (state is not None and state not in STATES):
        raise ValueError("INVALID_PHASE15_TRADE_FILTER")
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ValueError("INVALID_PHASE15_TRADE_PAGE")
    at = at.astimezone(IST)
    with sessions() as session, session.no_autoflush:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        queries = []
        model: Any
        for model, acts in ((ActsPaperTrade, True), (ParallelPaperTrade, False)):
            source_strategy = literal("ACTS_V2") if acts else model.strategy_id
            query = select(model.id.label("trade_id"), source_strategy.label("strategy_id"),
                           model.state, model.document, ScalperMarketSnapshotRecord.captured_at.label("decision_at")
                           ).join(ScalperMarketSnapshotRecord,
                                  ScalperMarketSnapshotRecord.id == model.decision_snapshot_id)
            if acts:
                document = (cast(model.document, JSON) if session.get_bind().dialect.name == "postgresql"
                            else type_coerce(model.document, JSON))
                query = query.where(document["strategy_version"].as_string() == "ACTS_V2")
            else:
                query = query.where(model.strategy_id.in_(ORDER[1:]))
            if strategy is not None:
                query = query.where(source_strategy == strategy)
            if positions:
                query = query.where(model.state.in_(ACTIVE))
            elif state is not None:
                query = query.where(model.state == state)
            if day is not None and not positions:
                start = datetime.combine(day, time.min, IST)
                query = query.where(ScalperMarketSnapshotRecord.captured_at >= start,
                                    ScalperMarketSnapshotRecord.captured_at < start + timedelta(days=1))
            queries.append(query)
        combined = union_all(*queries).subquery()
        rows = list(session.execute(select(combined).order_by(
            combined.c.decision_at.desc(), combined.c.strategy_id, combined.c.trade_id
        ).limit(limit + 1).offset(offset)))
        has_more = len(rows) > limit
        rows = rows[:limit]
        marks = _marks(session, rows)
        return {"mode": "PAPER", "as_of": at.isoformat(), "accounting_status": "GROSS_ONLY",
                "date": day.isoformat() if day is not None and not positions else None,
                "date_basis": "DECISION_OBSERVATION_IST", "limit": limit, "offset": offset,
                "has_more": has_more,
                "items": [_project(row, marks.get((row.strategy_id, row.trade_id)), at) for row in rows]}
