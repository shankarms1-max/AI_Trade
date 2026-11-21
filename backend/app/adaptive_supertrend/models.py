"""AST-owned projections and immutable optimizer, session and journal evidence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OptimizerRun(Base):
    __tablename__ = "ast_optimizer_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document: Mapped[str] = mapped_column(Text, nullable=False)


class SessionParameters(Base):
    __tablename__ = "ast_session_parameters"
    trading_date: Mapped[Any] = mapped_column(Date, primary_key=True)
    optimizer_run_id: Mapped[str] = mapped_column(ForeignKey("ast_optimizer_runs.id"), nullable=False)
    document: Mapped[str] = mapped_column(Text, nullable=False)


# Retired option tables remain mapped to preserve migration metadata and evidence.
class LegacyAstCursor(Base):
    __tablename__ = "ast_paper_cursor"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[str | None] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="GENESIS")
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    latest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    __table_args__ = (CheckConstraint("id = 1", name="ck_ast_single_cursor"),)


class LegacyAstTrade(Base):
    __tablename__ = "ast_paper_trades"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("scalper_market_snapshots.id"), unique=True, nullable=False
    )
    trading_date: Mapped[Any] = mapped_column(Date, index=True, nullable=False)
    closed_date: Mapped[Any | None] = mapped_column(Date, index=True)
    state: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    document: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING_PAPER_ENTRY','OPEN','CLOSED'," "'ENTRY_REJECTED','UNRESOLVED_EXPOSURE')",
            name="ck_ast_trade_state",
        ),
    )


class LegacyAstEvent(Base):
    __tablename__ = "ast_paper_events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    event_key: Mapped[str] = mapped_column(String(220), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("scalper_market_snapshots.id"), nullable=False)
    trade_id: Mapped[str | None] = mapped_column(ForeignKey("ast_paper_trades.id"))
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_ast_event_trade_sequence", "trade_id", "sequence"),
        Index("ix_ast_event_type_sequence", "event_type", "sequence"),
    )


class AstCursor(Base):
    __tablename__ = "ast_futures_paper_cursor"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[str | None] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="GENESIS")
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    latest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    __table_args__ = (CheckConstraint("id = 1", name="ck_ast_futures_single_cursor"),)


class AstTrade(Base):
    __tablename__ = "ast_futures_paper_trades"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("scalper_market_snapshots.id"), unique=True, nullable=False
    )
    trading_date: Mapped[Any] = mapped_column(Date, index=True, nullable=False)
    closed_date: Mapped[Any | None] = mapped_column(Date, index=True)
    state: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    document: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING_PAPER_ENTRY','OPEN','PENDING_PAPER_EXIT','CLOSED',"
            "'ENTRY_REJECTED','UNRESOLVED_EXPOSURE')",
            name="ck_ast_futures_trade_state",
        ),
    )


class AstEvent(Base):
    __tablename__ = "ast_futures_paper_events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    event_key: Mapped[str] = mapped_column(String(220), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("scalper_market_snapshots.id"), nullable=False)
    trade_id: Mapped[str | None] = mapped_column(ForeignKey("ast_futures_paper_trades.id"))
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_ast_futures_event_trade_sequence", "trade_id", "sequence"),
        Index("ix_ast_futures_event_type_sequence", "event_type", "sequence"),
    )


for model in (LegacyAstEvent, AstEvent, SessionParameters, OptimizerRun):

    @event.listens_for(model, "before_update")
    @event.listens_for(model, "before_delete")
    def immutable(*_: Any) -> None:
        raise ValueError("AST_EVIDENCE_IS_IMMUTABLE")
