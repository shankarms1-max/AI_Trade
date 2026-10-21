"""Commodity-only paper projections, atomic fills and immutable event evidence."""
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.commodity_acts.models import INSTRUMENT_CHECK, immutable


class CommodityPaperCursor(Base):
    __tablename__ = "commodity_paper_cursors"
    instrument_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), default="GENESIS")
    observed_at: Mapped[str | None] = mapped_column(String(64))
    snapshot_id: Mapped[int | None] = mapped_column(Integer)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    latest_json: Mapped[str] = mapped_column(Text, default="{}")
    __table_args__ = (CheckConstraint(INSTRUMENT_CHECK),)


class CommodityPaperControl(Base):
    __tablename__ = "commodity_paper_controls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lock_version: Mapped[int] = mapped_column(Integer, default=0)
    policy_json: Mapped[str] = mapped_column(Text, default="{}")
    __table_args__ = (CheckConstraint("id = 1"),)


class CommodityPaperPosition(Base):
    __tablename__ = "commodity_paper_positions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(24), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    observed_at: Mapped[str] = mapped_column(String(64), index=True)
    document: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("instrument_id", "id"), CheckConstraint(INSTRUMENT_CHECK))


class CommodityPaperOrder(Base):
    __tablename__ = "commodity_paper_orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(24), index=True)
    position_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[str] = mapped_column(String(64), index=True)
    document: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("instrument_id", "id"),
        ForeignKeyConstraint(["instrument_id", "position_id"], ["commodity_paper_positions.instrument_id", "commodity_paper_positions.id"]))


class CommodityPaperFill(Base):
    __tablename__ = "commodity_paper_fills"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(24), index=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True)
    observed_at: Mapped[str] = mapped_column(String(64), index=True)
    document: Mapped[str] = mapped_column(Text)
    __table_args__ = (ForeignKeyConstraint(["instrument_id", "order_id"],
        ["commodity_paper_orders.instrument_id", "commodity_paper_orders.id"]),)


class CommodityPaperEvent(Base):
    __tablename__ = "commodity_paper_events"
    instrument_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    event_key: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(40))
    previous_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("instrument_id", "event_key"), CheckConstraint(INSTRUMENT_CHECK))


for model in (CommodityPaperFill, CommodityPaperEvent):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)
