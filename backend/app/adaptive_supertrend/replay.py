"""Offline execution research in an ephemeral database, sharing the exact paper service."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, select

from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.journal import verify
from app.adaptive_supertrend.models import AstCursor, AstEvent, AstTrade, OptimizerRun, SessionParameters
from app.adaptive_supertrend.service import AstPaperService, freeze
from app.core.config import Settings
from app.db.base import Base
from app.db.session import build_session_factory
from app.scalper.models import ScalperMarketSnapshot, ScalperMarketSnapshotRecord, ScalperOptionQuoteRecord
from app.scalper.research_capture import AST_PAPER_MODE, ResearchSnapshotRepository
from app.scalper.repository import IST


def replay(bars, observations: Path, policy: AstPolicy) -> dict:
    """JSONL contains actual persisted raw snapshots, never signals or synthetic quote reconstruction.

    The caller may compare explicit safety policies by invoking this function separately.
    Input and output cannot mutate source snapshots, ACTS tables or production databases.
    """
    count = 0
    previous = None
    freezes = {}
    with TemporaryDirectory(prefix="ast-execution-research-") as directory:
        engine = create_engine(f"sqlite+pysqlite:///{directory}/replay.db")
        tables = [
            Base.metadata.tables[model.__tablename__]
            for model in (
                ScalperMarketSnapshotRecord,
                ScalperOptionQuoteRecord,
                OptimizerRun,
                SessionParameters,
                AstCursor,
                AstTrade,
                AstEvent,
            )
        ]
        Base.metadata.create_all(engine, tables=tables)
        sessions = build_session_factory(engine)
        with sessions.begin() as session:
            session.add(AstCursor(id=1))
        settings = Settings(_env_file=None, ast_config_json=policy.model_dump_json())  # type: ignore[call-arg]
        service = AstPaperService(sessions, settings)
        repository = ResearchSnapshotRepository(engine, mode=AST_PAPER_MODE)
        try:
            with observations.open() as stream:
                for line in stream:
                    raw = ScalperMarketSnapshot.model_validate_json(line)
                    at = raw.captured_at.astimezone(IST)
                    if previous is not None and at <= previous:
                        raise ValueError("AST_REPLAY_STRICTLY_CHRONOLOGICAL_OBSERVATIONS_REQUIRED")
                    previous = at
                    if at.date() not in freezes:
                        training = [bar for bar in bars if bar.start.astimezone(IST).date() < at.date()]
                        try:
                            freeze(
                                sessions,
                                training,
                                at.date(),
                                at.replace(hour=9, minute=0, second=0, microsecond=0),
                                policy,
                            )
                            freezes[at.date()] = "FROZEN"
                        except (ValueError, RuntimeError) as exc:
                            freezes[at.date()] = str(exc)
                    captured = repository.save(raw)
                    service.process_snapshot(captured.snapshot_id)
                    count += 1
            verify(sessions, full=True)
            with sessions() as session:
                rows = list(session.scalars(select(AstTrade).order_by(AstTrade.decision_snapshot_id)))
                documents = [json.loads(row.document) for row in rows]
                closed = [doc for row, doc in zip(rows, documents) if row.state == "CLOSED"]
                return {
                    "result_layer": "EXECUTABLE_OPTION_REPLAY",
                    "observations": count,
                    "policy_hash": policy.policy_hash,
                    "policy": policy.model_dump(mode="json"),
                    "safety_policy": policy.execution.safety.model_dump(mode="json") if policy.execution else None,
                    "parameter_freezes": {str(key): value for key, value in freezes.items()},
                    "journal_valid": True,
                    "trades": documents,
                    "gross_option_pnl": sum(doc["gross_pnl"] for doc in closed),
                    "net_option_pnl": (
                        sum(doc["net_pnl"] for doc in closed)
                        if closed and all(doc.get("net_pnl") is not None for doc in closed)
                        else None
                    ),
                    "unresolved_exposure": sum(row.state == "UNRESOLVED_EXPOSURE" for row in rows),
                    "option_fills_inferred": False,
                }
        finally:
            engine.dispose()
