"""Read-only AST status. No collector construction, initialization or journal writes."""

from datetime import datetime
import json

from sqlalchemy import or_, select

from app.adaptive_supertrend import VERSION
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.journal import ACTIVE
from app.adaptive_supertrend.models import AstCursor, AstTrade, OptimizerRun, SessionParameters
from app.scalper.repository import IST


def report(sessions, at: datetime, policy: AstPolicy | None = None) -> dict:
    at = at.astimezone(IST)
    with sessions() as session:
        cursor = session.get(AstCursor, 1)
        if cursor is None:
            return {"strategy_id": VERSION, "strategy_version": VERSION, "status": "AST_MIGRATION_REQUIRED"}
        parameters = session.get(SessionParameters, at.date())
        document = json.loads(parameters.document) if parameters else None
        if document is not None and document.get("strategy_version") != VERSION:
            parameters = document = None
        optimizer = session.get(OptimizerRun, parameters.optimizer_run_id) if parameters else None
        optimizer_doc = json.loads(optimizer.document) if optimizer else None
        rows = list(
            session.scalars(
                select(AstTrade).where(
                    or_(
                        AstTrade.state.in_(ACTIVE),
                        AstTrade.trading_date == at.date(),
                        AstTrade.closed_date == at.date(),
                    )
                )
            )
        )
        docs = [json.loads(row.document) for row in rows]
        realized = sum(
            doc.get("gross_pnl", 0)
            for doc in docs
            if doc.get("exit_timestamp") and datetime.fromisoformat(doc["exit_timestamp"]).date() == at.date()
        )
        closed = [doc for row, doc in zip(rows, docs) if row.closed_date == at.date()]
        marks = [
            doc.get("gross_mark_rupees")
            for row, doc in zip(rows, docs)
            if row.state in ("OPEN", "PENDING_PAPER_EXIT", "UNRESOLVED_EXPOSURE")
        ]
        return {
            "strategy_id": VERSION,
            "strategy_version": VERSION,
            "mode": "PAPER",
            "status": (
                "AST_POLICY_UNCONFIGURED"
                if policy is not None and policy.missing
                else "AST_PARAMETERS_UNAVAILABLE" if document is None else "PAPER_RESEARCH"
            ),
            "missing_policy": policy.missing if policy else None,
            "latest": json.loads(cursor.latest_json),
            "last_observed_at": cursor.observed_at,
            "parameters": document,
            "optimizer_summary": (
                {
                    key: optimizer_doc[key]
                    for key in ("grid_count", "selected", "training_data_identity", "walk_forward_status")
                }
                if optimizer_doc
                else None
            ),
            "trades_today": sum(
                bool(doc.get("entry_timestamp")) and datetime.fromisoformat(doc["entry_timestamp"]).date() == at.date()
                for doc in docs
            ),
            "open_trades": [doc for row, doc in zip(rows, docs) if row.state in ("OPEN", "PENDING_PAPER_EXIT")],
            "unresolved_exposure": sum(row.state == "UNRESOLVED_EXPOSURE" for row in rows),
            "realized_gross_pnl": realized,
            "unrealized_gross_pnl": sum(marks) if all(v is not None for v in marks) else None,
            "realized_net_pnl": (
                sum(doc["net_pnl"] for doc in closed)
                if closed and all(doc.get("net_pnl") is not None for doc in closed)
                else None
            ),
            "journal_head": cursor.head_hash,
        }
