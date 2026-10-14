"""Strategy-partitioned cursors, projections, and append-only hash chains."""
from __future__ import annotations

from typing import Any

from sqlalchemy import (CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer,
                        String, Text, UniqueConstraint, event)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ParallelPaperCursor(Base):
    __tablename__ = "parallel_paper_cursors"
    strategy_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[str | None] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    head_hash: Mapped[str] = mapped_column(String(64), default="GENESIS", nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    latest_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)


class ParallelPaperTrade(Base):
    __tablename__ = "parallel_paper_trades"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(
        ForeignKey("parallel_paper_cursors.strategy_id"), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("scalper_market_snapshots.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    document: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        Index("ix_parallel_strategy_state", "strategy_id", "state"),
        UniqueConstraint("strategy_id", "decision_snapshot_id", name="uq_parallel_decision"),
        UniqueConstraint("strategy_id", "id", name="uq_parallel_trade_owner"),
        CheckConstraint("state IN ('PENDING_PAPER_ENTRY','OPEN','CLOSED','ENTRY_REJECTED',"
                        "'UNRESOLVED_EXPOSURE')", name="ck_parallel_trade_state"),
    )


class ParallelPaperEvent(Base):
    __tablename__ = "parallel_paper_events"
    strategy_id: Mapped[str] = mapped_column(
        ForeignKey("parallel_paper_cursors.strategy_id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    event_key: Mapped[str] = mapped_column(String(220), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("scalper_market_snapshots.id"), nullable=False)
    trade_id: Mapped[str | None] = mapped_column(String(64))
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["strategy_id", "trade_id"],
                             ["parallel_paper_trades.strategy_id", "parallel_paper_trades.id"]),
        UniqueConstraint("strategy_id", "event_key", name="uq_parallel_event"),
    )


@event.listens_for(ParallelPaperEvent, "before_update")
@event.listens_for(ParallelPaperEvent, "before_delete")
def forbid_event_mutation(*_: Any) -> None:
    raise ValueError("PARALLEL_PAPER_EVIDENCE_IS_IMMUTABLE")
