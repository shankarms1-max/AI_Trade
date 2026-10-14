"""Read-only strategy-separated paper performance, including unchanged ACTS V2 evidence."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from statistics import mean

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.acts_paper.models import ActsPaperCursor, ActsPaperTrade
from app.parallel_paper.config import STRATEGIES, VERSIONS
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperTrade
from app.research.manifest import json_value
from app.scalper.repository import IST


def statistics(rows: list[tuple[str, dict]], at: datetime) -> dict:
    entries = [(state, doc) for state, doc in rows if doc.get("entry_timestamp")]
    closed = sorted([doc for state, doc in entries if state == "CLOSED"],
                    key=lambda doc: doc["exit_timestamp"])
    pnl = [(doc.get("final_outcome") or {})["gross_rupees"] for doc in closed]
    wins, losses = [value for value in pnl if value > 0], [value for value in pnl if value < 0]
    equity = peak = drawdown = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    day = at.astimezone(IST).date()
    today = [(state, doc) for state, doc in entries
             if datetime.fromisoformat(doc["entry_timestamp"]).astimezone(IST).date() == day]
    active = [doc | {"state": state} for state, doc in rows
              if state in {"OPEN", "PENDING_PAPER_ENTRY", "UNRESOLVED_EXPOSURE"}]
    realized = sum(doc["final_outcome"]["gross_rupees"] for doc in closed
                   if datetime.fromisoformat(doc["exit_timestamp"]).astimezone(IST).date() == day)
    marks = [doc.get("gross_mark_rupees") for doc in active if doc["state"] == "OPEN"]
    unknown = any(doc["state"] == "UNRESOLVED_EXPOSURE" for doc in active) or any(
        value is None for value in marks)
    return {"trades_today": len(today), "realized_pnl_today": realized,
            "unrealized_pnl": None if unknown else sum(marks),
            "open_position": active, "trades": len(entries), "closed_trades": len(closed),
            "wins": len(wins), "losses": len(losses), "breakeven": sum(value == 0 for value in pnl),
            "win_rate": len(wins) / len(pnl) if pnl else None,
            "gross_pnl": sum(pnl), "average_pnl": mean(pnl) if pnl else None,
            "expectancy": mean(pnl) if pnl else None,
            "average_win": mean(wins) if wins else None,
            "average_loss": mean(losses) if losses else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "profit_factor_status": "DEFINED" if losses else "NO_LOSING_TRADES",
            "max_drawdown": drawdown,
            "mfe_average_points": mean(doc.get("mfe", 0) for doc in closed) if closed else None,
            "mae_average_points": mean(doc.get("mae", 0) for doc in closed) if closed else None,
            "average_holding_seconds": mean(
                (datetime.fromisoformat(doc["exit_timestamp"])
                 - datetime.fromisoformat(doc["entry_timestamp"])).total_seconds()
                for doc in closed) if closed else None,
            "false_breakout_trades": sum(bool(doc.get("false_breakout_first_at")) for doc in closed),
            "false_breakout_rate": sum(bool(doc.get("false_breakout_first_at"))
                                       for doc in closed) / len(closed) if closed else None,
            "entry_reason_counts": dict(Counter(doc.get("entry_reason") for _, doc in entries)),
            "exit_reason_counts": dict(Counter(doc.get("exit_reason") for doc in closed)),
            "net_pnl": sum(doc["final_outcome"]["net_rupees"] for doc in closed)
            if closed and all(doc["final_outcome"].get("net_rupees") is not None for doc in closed) else None,
            "accounting_status": "NET_COMPLETE" if closed and all(
                doc["final_outcome"].get("net_rupees") is not None for doc in closed) else "GROSS_ONLY",
            "completed_trade_evidence": closed}


def report(sessions: sessionmaker[Session], at: datetime) -> dict:
    with sessions() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        result = {}
        for strategy in STRATEGIES:
            rows = session.scalars(select(ParallelPaperTrade).where(
                ParallelPaperTrade.strategy_id == strategy).order_by(
                    ParallelPaperTrade.decision_snapshot_id)).all()
            cursor = session.get(ParallelPaperCursor, strategy)
            result[strategy] = {"strategy_id": strategy, "strategy_version": VERSIONS[strategy],
                                "latest": json.loads(cursor.latest_json) if cursor else {},
                                "journal_head": cursor.head_hash if cursor else None,
                                **statistics([(row.state, json.loads(row.document)) for row in rows], at)}
        # Read the original ACTS projection without instantiating or invoking its service.
        acts_rows = session.scalars(select(ActsPaperTrade).order_by(ActsPaperTrade.decision_snapshot_id)).all()
        acts_docs = [(row.state, json.loads(row.document)) for row in acts_rows
                     if json.loads(row.document).get("strategy_version") == "ACTS_V2"]
        acts_cursor = session.get(ActsPaperCursor, 1)
        result["ACTS_V2"] = {
            "strategy_id": "ACTS_V2", "strategy_version": "ACTS_V2",
            "latest": json.loads(acts_cursor.latest_json) if acts_cursor else {},
            "journal_head": acts_cursor.head_hash if acts_cursor else None,
            **statistics(acts_docs, at)}
        return json_value({"mode": "PAPER", "as_of": at, "strategies": result})
