"""AST_FUTURES_V1_DIRECT projections and immutable hash-chained journal. Separate from earlier AST evidence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

TRADE_STATES = (
    "PENDING_PAPER_ENTRY",
    "OPEN",
    "PENDING_PAPER_EXIT",
    "CLOSED",
    "ENTRY_REJECTED",
    "UNRESOLVED_EXPOSURE",
    "UNRESOLVED_ACKNOWLEDGED",
)


class DirectCursor(Base):
    __tablename__ = "ast_direct_paper_cursor"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[str | None] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="GENESIS")
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    latest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    __table_args__ = (CheckConstraint("id = 1", name="ck_ast_direct_single_cursor"),)


class DirectTrade(Base):
    __tablename__ = "ast_direct_paper_trades"
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
            "state IN (" + ",".join(f"'{state}'" for state in TRADE_STATES) + ")",
            name="ck_ast_direct_trade_state",
        ),
    )


class DirectEvent(Base):
    __tablename__ = "ast_direct_paper_events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    event_key: Mapped[str] = mapped_column(String(220), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("scalper_market_snapshots.id"), nullable=False)
    trade_id: Mapped[str | None] = mapped_column(ForeignKey("ast_direct_paper_trades.id"))
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_ast_direct_event_trade_sequence", "trade_id", "sequence"),
        Index("ix_ast_direct_event_type_sequence", "event_type", "sequence"),
    )


@event.listens_for(DirectEvent, "before_update")
@event.listens_for(DirectEvent, "before_delete")
def immutable(*_: Any) -> None:
    raise ValueError("AST_EVIDENCE_IS_IMMUTABLE")
