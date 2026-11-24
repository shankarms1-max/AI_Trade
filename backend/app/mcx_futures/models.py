"""ACTS_V3_MCX_FUTURES paper persistence: one cursor + one hash-chained journal PER INSTRUMENT.

Instruments never share a cursor, sequence, chain or trade row, so CRUDEOILM and NATGASMINI (and every NIFTY/AST table)
are isolated by construction. Journal events are append-only (ORM update/delete refused)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, Date, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

INSTRUMENTS = ("CRUDEOILM", "NATGASMINI")
TRADE_STATES = ("PENDING_PAPER_ENTRY", "OPEN", "CLOSED", "ENTRY_REJECTED", "UNRESOLVED_EXPOSURE")
_INST_CHECK = "instrument IN ('CRUDEOILM','NATGASMINI')"


class McxFuturesCursor(Base):
    __tablename__ = "mcx_futures_cursors"
    instrument: Mapped[str] = mapped_column(String(16), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="GENESIS")
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_at: Mapped[str | None] = mapped_column(String(64))
    engine_version: Mapped[str | None] = mapped_column(String(64))
    config_hash: Mapped[str | None] = mapped_column(String(64))
    contract_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    latest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    __table_args__ = (CheckConstraint(_INST_CHECK, name="ck_mcx_futures_cursor_instrument"),)


class McxFuturesEvent(Base):
    __tablename__ = "mcx_futures_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    at: Mapped[str] = mapped_column(String(64), nullable=False)
    trade_id: Mapped[str | None] = mapped_column(String(64), index=True)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("instrument", "sequence", name="uq_mcx_futures_event_sequence"),
                      CheckConstraint(_INST_CHECK, name="ck_mcx_futures_event_instrument"))


class McxFuturesTrade(Base):
    __tablename__ = "mcx_futures_trades"
    trade_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    trading_date: Mapped[Any] = mapped_column(Date, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    document: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (CheckConstraint(_INST_CHECK, name="ck_mcx_futures_trade_instrument"),
                      CheckConstraint("state IN (" + ",".join(f"'{s}'" for s in TRADE_STATES) + ")",
                                      name="ck_mcx_futures_trade_state"))


@event.listens_for(McxFuturesEvent, "before_update")
@event.listens_for(McxFuturesEvent, "before_delete")
def _forbid_event_mutation(*_: Any) -> None:
    raise ValueError("MCX_FUTURES_EVIDENCE_IS_IMMUTABLE")
