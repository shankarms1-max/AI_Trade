"""New commodity-only tables; no foreign keys or writes to NIFTY tables."""

from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime

from app.db.base import Base

INSTRUMENT_CHECK = "instrument_id IN ('CRUDEOILM','NATGASMINI')"


class CommodityMarketSnapshotRecord(Base):
    __tablename__ = "commodity_market_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    capture_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint("instrument_id", "id", name="uq_commodity_snapshot_owner"),
        CheckConstraint(INSTRUMENT_CHECK, name="ck_commodity_snapshot_instrument"),
    )


class CommodityOptionQuoteRecord(Base):
    __tablename__ = "commodity_option_quotes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(24), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    quote_json: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["instrument_id", "snapshot_id"],
            ["commodity_market_snapshots.instrument_id", "commodity_market_snapshots.id"],
        ),
        UniqueConstraint("instrument_id", "snapshot_id", "identity_key", name="uq_commodity_quote_identity"),
    )


class CommodityActsCursor(Base):
    __tablename__ = "commodity_acts_cursors"
    instrument_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="GENESIS")
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_at: Mapped[str | None] = mapped_column(String(64))
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    latest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    __table_args__ = (CheckConstraint(INSTRUMENT_CHECK, name="ck_commodity_cursor_instrument"),)


class CommodityActsEvent(Base):
    __tablename__ = "commodity_acts_events"
    instrument_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint("instrument_id", "event_key", name="uq_commodity_event_key"),
        CheckConstraint(INSTRUMENT_CHECK, name="ck_commodity_event_instrument"),
    )


class CommodityActsDecision(Base):
    __tablename__ = "commodity_acts_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(24), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    document: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["instrument_id", "snapshot_id"],
            ["commodity_market_snapshots.instrument_id", "commodity_market_snapshots.id"],
        ),
        UniqueConstraint("instrument_id", "snapshot_id", name="uq_commodity_decision_snapshot"),
    )


def immutable(*_: Any) -> None:
    raise ValueError("COMMODITY_RESEARCH_EVIDENCE_IS_IMMUTABLE")


for model in (CommodityMarketSnapshotRecord, CommodityOptionQuoteRecord, CommodityActsEvent, CommodityActsDecision):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)
