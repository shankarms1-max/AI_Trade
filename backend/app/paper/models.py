"""Mutable projections plus an append-only, hash-chained event journal."""
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PaperCursor(Base):
    __tablename__ = "paper_cursor"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[str | None] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="GENESIS")
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (CheckConstraint("id = 1", name="ck_paper_single_cursor"),)


class PaperTrade(Base):
    __tablename__ = "paper_trades"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_snapshot_id: Mapped[int] = mapped_column(ForeignKey("market_snapshots.id"), unique=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    document: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (CheckConstraint(
        "state IN ('PENDING_PAPER_ENTRY','OPEN','CLOSED','ENTRY_REJECTED','UNRESOLVED_EXPOSURE')",
        name="ck_paper_trade_state"),)


class PaperEvent(Base):
    __tablename__ = "paper_events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    event_key: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("market_snapshots.id"), nullable=False)
    trade_id: Mapped[str | None] = mapped_column(ForeignKey("paper_trades.id"))
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


@event.listens_for(PaperEvent, "before_update")
@event.listens_for(PaperEvent, "before_delete")
def forbid_event_mutation(*_):
    raise ValueError("PAPER_EVIDENCE_IS_IMMUTABLE")
