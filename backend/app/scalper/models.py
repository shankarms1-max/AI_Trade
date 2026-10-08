"""Phase 15 domain records and isolated SQL projections."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (BigInteger, CheckConstraint, Date, DateTime, ForeignKey,
                        Index, Integer, Numeric, String, Text, UniqueConstraint,
                        event, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

PRICE = Numeric(20, 6)


class ScalperDirection(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    NEUTRAL = "NEUTRAL"


class ScalperStrength(str, Enum):
    NONE = "NONE"
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


class ScalperTradeState(str, Enum):
    SIGNAL = "SIGNAL"
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    ENTRY_REJECTED = "ENTRY_REJECTED"
    UNRESOLVED = "UNRESOLVED"


class ScalperOptionQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expiry: date
    strike: float = Field(gt=0)
    option_type: Literal["CE", "PE"]
    exchange: str = Field(default="nse_fo", min_length=1)
    trading_symbol: str = Field(min_length=1)
    instrument_token: str = Field(min_length=1)
    source_market_timestamp: datetime | None = None
    bid: float | None = None
    ask: float | None = None
    bid_quantity: int | None = None
    ask_quantity: int | None = None
    depth_unit: Literal["UNKNOWN", "UNITS", "LOTS"] = "UNKNOWN"
    tick_size: float | None = None
    ltp: float | None = None
    volume: int | None = None
    open_interest: int | None = None

    @field_validator("source_market_timestamp")
    @classmethod
    def source_timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("SCALPER_AWARE_SOURCE_TIMESTAMP_REQUIRED")
        return value

    @property
    def identity(self) -> tuple[str, str, date, float, str, str]:
        return (self.exchange, self.instrument_token, self.expiry, self.strike,
                self.option_type, self.trading_symbol)


class ScalperMarketSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    captured_at: datetime
    request_started_at: datetime
    response_received_at: datetime
    source_market_timestamp: datetime | None = None
    nifty_spot: float = Field(gt=0)
    nifty_future: float | None = Field(default=None, gt=0)
    future_instrument_id: str | None = None
    future_expiry: date | None = None
    india_vix: float | None = Field(default=None, ge=0)
    lot_size: int = Field(gt=0)
    atm_strike: float = Field(gt=0)
    expiry: date
    source: str = "KOTAK_NEO"
    quotes: list[ScalperOptionQuote]

    @field_validator("captured_at", "request_started_at", "response_received_at",
                     "source_market_timestamp")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("SCALPER_AWARE_TIMESTAMP_REQUIRED")
        return value

    @model_validator(mode="after")
    def validate_capture(self) -> "ScalperMarketSnapshot":
        if not self.quotes:
            raise ValueError("SCALPER_OPTION_QUOTES_EMPTY")
        if self.request_started_at > self.response_received_at:
            raise ValueError("SCALPER_CAPTURE_TIMESTAMPS_INVALID")
        identities = [item.identity for item in self.quotes]
        if len(identities) != len(set(identities)):
            raise ValueError("SCALPER_AMBIGUOUS_QUOTE_IDENTITY")
        if any(item.expiry != self.expiry for item in self.quotes):
            raise ValueError("SCALPER_QUOTE_EXPIRY_MISMATCH")
        return self


@dataclass(frozen=True)
class ScalperPriceObservation:
    """Only the five captured scalar fields needed by the slow context."""

    captured_at: datetime
    nifty_spot: float
    nifty_future: float | None
    future_instrument_id: str | None
    future_expiry: date | None


class IntradayContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_first_at: datetime
    session_open: float
    session_high: float
    session_low: float
    session_move_bps: float
    session_range_bps: float
    range_position: float | None
    returns_bps: dict[str, float | None]
    horizon_reference_at: dict[str, datetime | None]
    horizon_agreement: float
    opening_range_complete: bool
    opening_high_distance_bps: float | None
    opening_low_distance_bps: float | None
    futures_returns_bps: dict[str, float | None]
    futures_basis_change_5m: float | None


class ScalperFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: int | None = None
    timestamp: datetime
    sample_count: int
    returns_bps: dict[str, float | None]
    momentum: float
    rolling_reference: float | None
    vwap: None = None
    opening_high: float | None
    opening_low: float | None
    local_high: float | None
    local_low: float | None
    futures_basis: float | None
    futures_basis_change: float | None
    call_participation_change: float | None
    put_participation_change: float | None
    call_volume_change: float | None
    put_volume_change: float | None
    executable_book_coverage: float
    median_spread_pct: float | None
    futures_return_bps: float | None = None
    intraday: IntradayContext | None = None
    warnings: list[str] = Field(default_factory=list)


class ScalperSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: int | None = None
    timestamp: datetime
    direction: ScalperDirection
    strength: ScalperStrength
    score: float = Field(ge=0, le=100)
    components: dict[str, float]
    contradiction_penalty: float = Field(ge=0)
    reasons: list[str]
    warnings: list[str]
    confirmation_count: int = Field(ge=0)
    confirmed: bool
    phase14_context: dict[str, Any] | None = None
    fast_direction: ScalperDirection | None = None
    slow_direction: ScalperDirection | None = None
    trend_alignment: Literal["ALIGNED", "OPPOSED", "MIXED"] = "MIXED"
    fast_score: float | None = None
    slow_context_score: float | None = None
    combined_score: float | None = None
    applicable_entry_threshold: float | None = None
    threshold_regime: Literal["TREND_ALIGNED", "MIXED", "COUNTERTREND"] = "MIXED"
    strong_slow_trend: bool = False
    signal_qualified: bool = False
    entry_qualified: bool = False
    primary_blockers: list[str] = Field(default_factory=list)
    candidate_count: int = 0
    candidate_rejections: dict[str, int] = Field(default_factory=dict)


class ScalperLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expiry: date
    strike: float
    option_type: Literal["CE", "PE"]
    exchange: str
    trading_symbol: str
    instrument_token: str


class ScalperCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    premium_retention_ratio: float | None = None
    construction_method: str | None = None
    construction_evidence: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)
    candidate_id: str
    strategy_type: Literal["BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"]
    direction: ScalperDirection
    short_leg: ScalperLeg
    long_leg: ScalperLeg
    spread_width: float
    short_distance_points: float
    executable_credit: float
    credit_to_width: float
    defined_max_loss_per_unit: float
    defined_max_loss_per_lot: float
    broker_margin: None = None
    ranking_score: float
    ranking_components: dict[str, float]
    holding_horizon_minutes: int
    pricing_basis: Literal["BID_ASK"] = "BID_ASK"
    quote_evidence: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class ScalperMarketSnapshotRecord(Base):
    __tablename__ = "scalper_market_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    capture_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_market_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nifty_spot: Mapped[Any] = mapped_column(PRICE, nullable=False)
    nifty_future: Mapped[Any | None] = mapped_column(PRICE)
    future_instrument_id: Mapped[str | None] = mapped_column(String(160))
    future_expiry: Mapped[date | None] = mapped_column(Date)
    india_vix: Mapped[Any | None] = mapped_column(PRICE)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    atm_strike: Mapped[Any] = mapped_column(PRICE, nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_json: Mapped[str] = mapped_column(Text, nullable=False)
    signal_json: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=func.now())
    quotes: Mapped[list["ScalperOptionQuoteRecord"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan")
    __table_args__ = (
        Index("ix_scalper_snapshots_captured_at", "captured_at"),
        Index("ix_scalper_snapshots_expiry", "expiry"),
    )


class ScalperOptionQuoteRecord(Base):
    __tablename__ = "scalper_option_quotes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scalper_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("scalper_market_snapshots.id", ondelete="CASCADE"), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[Any] = mapped_column(PRICE, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    trading_symbol: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_token: Mapped[str] = mapped_column(String(160), nullable=False)
    source_market_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bid: Mapped[Any | None] = mapped_column(PRICE)
    ask: Mapped[Any | None] = mapped_column(PRICE)
    bid_quantity: Mapped[int | None] = mapped_column(BigInteger)
    ask_quantity: Mapped[int | None] = mapped_column(BigInteger)
    depth_unit: Mapped[str] = mapped_column(String(12), nullable=False)
    tick_size: Mapped[Any | None] = mapped_column(PRICE)
    ltp: Mapped[Any | None] = mapped_column(PRICE)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    open_interest: Mapped[int | None] = mapped_column(BigInteger)
    snapshot: Mapped[ScalperMarketSnapshotRecord] = relationship(back_populates="quotes")
    __table_args__ = (
        UniqueConstraint("scalper_snapshot_id", "expiry", "strike", "option_type",
                         "exchange", "trading_symbol", "instrument_token",
                         name="uq_scalper_quote_identity"),
        CheckConstraint("option_type IN ('CE','PE')", name="ck_scalper_quote_type"),
        Index("ix_scalper_quotes_snapshot", "scalper_snapshot_id"),
        Index("ix_scalper_quotes_contract", "expiry", "strike", "option_type"),
    )


class ScalperCursor(Base):
    __tablename__ = "scalper_cursor"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[str | None] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="GENESIS")
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confirmation_direction: Mapped[str | None] = mapped_column(String(8))
    confirmation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (CheckConstraint("id = 1", name="ck_scalper_single_cursor"),)


class ScalperTrade(Base):
    __tablename__ = "scalper_trades"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("scalper_market_snapshots.id"), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    document: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (CheckConstraint(
        "state IN ('SIGNAL','PENDING_ENTRY','OPEN','CLOSED','ENTRY_REJECTED','UNRESOLVED')",
        name="ck_scalper_trade_state"),)


class ScalperEvent(Base):
    __tablename__ = "scalper_events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    event_key: Mapped[str] = mapped_column(String(220), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("scalper_market_snapshots.id"), nullable=False)
    trade_id: Mapped[str | None] = mapped_column(ForeignKey("scalper_trades.id"))
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


@event.listens_for(ScalperEvent, "before_update")
@event.listens_for(ScalperEvent, "before_delete")
def forbid_scalper_event_mutation(*_: Any) -> None:
    raise ValueError("SCALPER_EVIDENCE_IS_IMMUTABLE")
