from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base

PRICE = Numeric(20, 6)
GREEK = Numeric(24, 10)


class MarketSnapshotRecord(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp_ist: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collection_bucket_ist: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, unique=True
    )
    nifty_spot: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    nifty_future: Mapped[Decimal | None] = mapped_column(PRICE)
    future_instrument_id: Mapped[str | None] = mapped_column(String(160))
    future_expiry: Mapped[date | None] = mapped_column(Date)
    source_market_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    india_vix: Mapped[Decimal | None] = mapped_column(PRICE)
    lot_size: Mapped[int | None] = mapped_column(Integer)
    atm_strike: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    options: Mapped[list["OptionContractSnapshotRecord"]] = relationship(
        back_populates="market_snapshot", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_market_snapshots_timestamp_ist", "timestamp_ist"),
        Index("ix_market_snapshots_expiry", "expiry"),
    )


class OptionContractSnapshotRecord(Base):
    __tablename__ = "option_contract_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    strike: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    trading_symbol: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_token: Mapped[str | None] = mapped_column(String(160))
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, default="nse_fo")
    source_market_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bid_quantity: Mapped[int | None] = mapped_column(BigInteger)
    ask_quantity: Mapped[int | None] = mapped_column(BigInteger)
    depth_unit: Mapped[str] = mapped_column(String(12), nullable=False, default="UNKNOWN", server_default="UNKNOWN")
    tick_size: Mapped[Decimal | None] = mapped_column(PRICE)
    ltp: Mapped[Decimal | None] = mapped_column(PRICE)
    open_interest: Mapped[int | None] = mapped_column(BigInteger)
    previous_open_interest: Mapped[int | None] = mapped_column(BigInteger)
    change_in_open_interest: Mapped[int | None] = mapped_column(BigInteger)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    implied_volatility: Mapped[Decimal | None] = mapped_column(PRICE)
    bid: Mapped[Decimal | None] = mapped_column(PRICE)
    ask: Mapped[Decimal | None] = mapped_column(PRICE)
    delta: Mapped[Decimal | None] = mapped_column(GREEK)
    gamma: Mapped[Decimal | None] = mapped_column(GREEK)
    theta: Mapped[Decimal | None] = mapped_column(GREEK)
    vega: Mapped[Decimal | None] = mapped_column(GREEK)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    market_snapshot: Mapped[MarketSnapshotRecord] = relationship(back_populates="options")

    __table_args__ = (
        UniqueConstraint(
            "market_snapshot_id", "expiry", "strike", "option_type",
            name="uq_option_contract_per_snapshot",
        ),
        CheckConstraint("option_type IN ('CE', 'PE')", name="ck_option_type"),
        Index("ix_option_contract_snapshot_id", "market_snapshot_id"),
        Index("ix_option_contract_strike", "strike"),
        Index("ix_option_contract_option_type", "option_type"),
        Index("ix_option_contract_expiry", "expiry"),
    )


class CollectorRunRecord(Base):
    __tablename__ = "collector_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    contracts_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="SET NULL")
    )
    error_type: Mapped[str | None] = mapped_column(String(120))
    safe_error_message: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        CheckConstraint(
            "status IN ('STARTED', 'SUCCESS', 'FAILED')", name="ck_collector_status"
        ),
        Index("ix_collector_runs_started_at", "started_at"),
        Index("ix_collector_runs_snapshot_id", "snapshot_id"),
    )


class MarketFeatureSnapshotRecord(Base):
    __tablename__ = "market_feature_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    local_pcr_oi: Mapped[Decimal | None] = mapped_column(GREEK)
    local_pcr_oi_change: Mapped[Decimal | None] = mapped_column(GREEK)
    futures_basis: Mapped[Decimal | None] = mapped_column(PRICE)
    india_vix: Mapped[Decimal | None] = mapped_column(PRICE)
    vix_regime: Mapped[str | None] = mapped_column(String(16))
    nearest_support: Mapped[Decimal | None] = mapped_column(PRICE)
    nearest_resistance: Mapped[Decimal | None] = mapped_column(PRICE)
    feature_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "market_snapshot_id", "feature_version",
            name="uq_feature_snapshot_version",
        ),
        Index("ix_feature_snapshots_timestamp", "timestamp"),
        Index("ix_feature_snapshots_expiry", "expiry"),
        Index("ix_feature_snapshots_market_snapshot_id", "market_snapshot_id"),
    )


class AlphaFeatureSnapshotRecord(Base):
    __tablename__ = "alpha_feature_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    alpha_version: Mapped[str] = mapped_column(String(32), nullable=False)
    calculation_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="HISTORICAL_REPLAY")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    price_source: Mapped[str] = mapped_column(String(16), nullable=False)
    price_return: Mapped[Decimal | None] = mapped_column(GREEK)
    session_id: Mapped[str | None] = mapped_column(String(32))
    session_date: Mapped[date | None] = mapped_column(Date)
    lookback_clock_mode: Mapped[str | None] = mapped_column(String(20))
    actual_horizon_seconds: Mapped[int | None] = mapped_column(Integer)
    signed_log_return: Mapped[Decimal | None] = mapped_column(GREEK)
    hypothesis_type: Mapped[str | None] = mapped_column(String(16))
    validity_state: Mapped[str | None] = mapped_column(String(16))
    participation_state: Mapped[str | None] = mapped_column(String(16))
    underlying_horizon_volatility: Mapped[Decimal | None] = mapped_column(GREEK)
    confirmation_reset_reason: Mapped[str | None] = mapped_column(String(80))
    source_market_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feature_calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    alpha_1: Mapped[Decimal | None] = mapped_column(GREEK)
    atm_strike: Mapped[Decimal | None] = mapped_column(PRICE)
    atm_ce_token: Mapped[str | None] = mapped_column(String(160))
    atm_pe_token: Mapped[str | None] = mapped_column(String(160))
    atm_ce_interval_volume: Mapped[int | None] = mapped_column(BigInteger)
    atm_pe_interval_volume: Mapped[int | None] = mapped_column(BigInteger)
    ce_volume_ratio: Mapped[Decimal | None] = mapped_column(GREEK)
    pe_volume_ratio: Mapped[Decimal | None] = mapped_column(GREEK)
    atm_volume_activity: Mapped[Decimal | None] = mapped_column(GREEK)
    ce_observed_volatility: Mapped[Decimal | None] = mapped_column(GREEK)
    pe_observed_volatility: Mapped[Decimal | None] = mapped_column(GREEK)
    atm_option_volatility: Mapped[Decimal | None] = mapped_column(GREEK)
    directional_impulse_raw: Mapped[Decimal | None] = mapped_column(GREEK)
    alpha_2: Mapped[Decimal | None] = mapped_column(GREEK)
    alpha_1_direction: Mapped[str] = mapped_column(String(24), nullable=False)
    alpha_2_direction: Mapped[str] = mapped_column(String(24), nullable=False)
    joint_alpha_direction: Mapped[str] = mapped_column(String(40), nullable=False)
    consecutive_confirmation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_quality: Mapped[str] = mapped_column(String(16), nullable=False)
    warnings_json: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    result_json: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("market_snapshot_id", "alpha_version", "calculation_mode", name="uq_alpha_snapshot_version_mode"),
        CheckConstraint("price_source IN ('SPOT','FUTURE')", name="ck_alpha_price_source"),
        CheckConstraint(
            "evidence_quality IN ('HIGH','MEDIUM','LOW','INSUFFICIENT')",
            name="ck_alpha_evidence_quality",
        ),
        Index("ix_alpha_feature_timestamp", "timestamp"),
        Index("ix_alpha_feature_expiry", "expiry"),
        Index("ix_alpha_feature_market_snapshot", "market_snapshot_id"),
    )


class ResearchExperimentRecord(Base):
    __tablename__ = "research_experiment_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    train_period: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    validation_period: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    test_period: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    split_name: Mapped[str] = mapped_column(String(20), nullable=False)
    result_summary: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    sample_tier: Mapped[str] = mapped_column(String(24), nullable=False)

    __table_args__ = (Index("ix_research_experiment_config_hash", "config_hash"),)


class MarketRegimeSnapshotRecord(Base):
    __tablename__ = "market_regime_snapshots"

    strategy_logic_version: Mapped[str | None] = mapped_column(String(32), index=True)
    strategy_context_json: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    market_bias: Mapped[str | None] = mapped_column(String(16))
    directional_strength: Mapped[str | None] = mapped_column(String(16))
    strategy_family_eligibility: Mapped[str | None] = mapped_column(String(24))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    feature_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_feature_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    regime_version: Mapped[str] = mapped_column(String(32), nullable=False)
    regime: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False)
    evidence_quality: Mapped[str] = mapped_column(String(16), nullable=False)
    bull_score: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    bear_score: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    range_score: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "market_snapshot_id", "regime_version", name="uq_regime_snapshot_version"
        ),
        CheckConstraint(
            "regime IN ('BULLISH', 'BEARISH', 'RANGE', 'NO_TRADE')",
            name="ck_market_regime",
        ),
        CheckConstraint(
            "evidence_quality IN ('HIGH', 'MEDIUM', 'LOW', 'INSUFFICIENT')",
            name="ck_regime_evidence_quality",
        ),
        Index("ix_regime_feature_snapshot_id", "feature_snapshot_id"),
        Index("ix_regime_market_snapshot_id", "market_snapshot_id"),
        Index("ix_regime_created_at", "created_at"),
    )


class AIResearchSnapshotRecord(Base):
    __tablename__ = "ai_research_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    feature_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_feature_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    regime_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_regime_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    ai_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    market_view: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    original_ai_confidence: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    confidence_capped: Mapped[bool] = mapped_column(nullable=False, default=False)
    agreement_status: Mapped[str | None] = mapped_column(String(20))
    result_json: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(120))
    safe_error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("market_snapshot_id", "ai_version", name="uq_ai_research_version"),
        CheckConstraint("status IN ('STARTED', 'SUCCESS', 'FAILED')", name="ck_ai_research_status"),
        Index("ix_ai_research_feature_snapshot_id", "feature_snapshot_id"),
        Index("ix_ai_research_regime_snapshot_id", "regime_snapshot_id"),
        Index("ix_ai_research_created_at", "created_at"),
    )


class StrategyCandidateSetRecord(Base):
    __tablename__ = "strategy_candidate_sets"

    strategy_logic_version: Mapped[str | None] = mapped_column(String(32), index=True)
    strategy_context_json: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    strategy_family: Mapped[str | None] = mapped_column(String(32))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    regime_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_regime_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    regime: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy_type: Mapped[str] = mapped_column(String(24), nullable=False)
    eligible: Mapped[bool] = mapped_column(nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    result_json: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "market_snapshot_id", "strategy_version",
            name="uq_strategy_candidate_set_version",
        ),
        CheckConstraint("candidate_count >= 0", name="ck_strategy_candidate_count"),
        Index("ix_strategy_candidate_regime_snapshot_id", "regime_snapshot_id"),
        Index("ix_strategy_candidate_created_at", "created_at"),
    )


class RiskDecisionRecord(Base):
    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    regime_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_regime_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    strategy_candidate_set_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_candidate_sets.id", ondelete="CASCADE"), nullable=False
    )
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_version: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_fingerprint: Mapped[str | None] = mapped_column(String(240))
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy_type: Mapped[str] = mapped_column(String(24), nullable=False)
    max_profit_per_unit: Mapped[Decimal | None] = mapped_column(PRICE)
    max_loss_per_unit: Mapped[Decimal | None] = mapped_column(PRICE)
    lot_size: Mapped[int | None] = mapped_column(Integer)
    max_profit_per_lot: Mapped[Decimal | None] = mapped_column(PRICE)
    max_loss_per_lot: Mapped[Decimal | None] = mapped_column(PRICE)
    failed_check_count: Mapped[int] = mapped_column(Integer, nullable=False)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False)
    result_json: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "market_snapshot_id", "strategy_version", "risk_version",
            "candidate_fingerprint", name="uq_risk_decision_candidate_version",
        ),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED', 'NOT_APPLICABLE')",
            name="ck_risk_decision",
        ),
        Index("ix_risk_decision_candidate_set_id", "strategy_candidate_set_id"),
        Index("ix_risk_decision_created_at", "created_at"),
    )


class ShadowTradeRecord(Base):
    __tablename__ = "shadow_trades"

    strategy_logic_version: Mapped[str | None] = mapped_column(String(32), index=True)
    strategy_context_json: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    strategy_family: Mapped[str | None] = mapped_column(String(32))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_snapshot_id_entry: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    risk_decision_id: Mapped[int] = mapped_column(
        ForeignKey("risk_decisions.id", ondelete="CASCADE"), nullable=False
    )
    candidate_fingerprint: Mapped[str] = mapped_column(String(240), nullable=False)
    shadow_version: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_type: Mapped[str] = mapped_column(String(24), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    entry_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_spot: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    entry_credit: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    entry_pricing_basis: Mapped[str] = mapped_column(String(20), nullable=False)
    spread_width: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    lot_size: Mapped[int | None] = mapped_column(Integer)
    max_profit_per_unit: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    max_loss_per_unit: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    max_profit_per_lot: Mapped[Decimal | None] = mapped_column(PRICE)
    max_loss_per_lot: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    exit_market_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="SET NULL")
    )
    exit_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_reason: Mapped[str | None] = mapped_column(String(64))
    realized_pnl_per_unit: Mapped[Decimal | None] = mapped_column(PRICE)
    realized_pnl_per_lot: Mapped[Decimal | None] = mapped_column(PRICE)
    mae_per_unit: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    mfe_per_unit: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    mae_per_lot: Mapped[Decimal | None] = mapped_column(PRICE)
    mfe_per_lot: Mapped[Decimal | None] = mapped_column(PRICE)
    holding_minutes: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    result_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("candidate_fingerprint", "shadow_version", name="uq_shadow_trade_fingerprint_version"),
        CheckConstraint("status IN ('OPEN', 'CLOSED', 'INVALID')", name="ck_shadow_trade_status"),
        Index("ix_shadow_trade_entry_snapshot", "market_snapshot_id_entry"),
        Index("ix_shadow_trade_status", "status"),
        Index("ix_shadow_trade_entry_timestamp", "entry_timestamp"),
    )


class ShadowTradeMarkRecord(Base):
    __tablename__ = "shadow_trade_marks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shadow_trade_id: Mapped[int] = mapped_column(
        ForeignKey("shadow_trades.id", ondelete="CASCADE"), nullable=False
    )
    market_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    short_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    long_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    valuation_basis: Mapped[str] = mapped_column(String(20), nullable=False)
    exit_debit: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    pnl_per_unit: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    pnl_per_lot: Mapped[Decimal | None] = mapped_column(PRICE)
    spot: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("shadow_trade_id", "market_snapshot_id", name="uq_shadow_mark_snapshot"),
        Index("ix_shadow_mark_trade_id", "shadow_trade_id"),
        Index("ix_shadow_mark_timestamp", "timestamp"),
    )


class PipelineRunRecord(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    feature_status: Mapped[str] = mapped_column(String(16), nullable=False)
    alpha_status: Mapped[str] = mapped_column(String(16), nullable=False, default="SKIPPED")
    regime_status: Mapped[str] = mapped_column(String(16), nullable=False)
    ai_status: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy_status: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_status: Mapped[str] = mapped_column(String(16), nullable=False)
    shadow_status: Mapped[str] = mapped_column(String(16), nullable=False)
    feature_snapshot_id: Mapped[int | None] = mapped_column(Integer)
    alpha_feature_snapshot_id: Mapped[int | None] = mapped_column(Integer)
    regime_snapshot_id: Mapped[int | None] = mapped_column(Integer)
    ai_research_id: Mapped[int | None] = mapped_column(Integer)
    strategy_candidate_set_id: Mapped[int | None] = mapped_column(Integer)
    approved_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shadow_trade_id: Mapped[int | None] = mapped_column(Integer)
    safe_error_type: Mapped[str | None] = mapped_column(String(120))
    safe_error_message: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stage_timings_ms: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("market_snapshot_id", "pipeline_version", name="uq_pipeline_run_version"),
        CheckConstraint("status IN ('STARTED','SUCCESS','PARTIAL','FAILED','SKIPPED')", name="ck_pipeline_status"),
        Index("ix_pipeline_run_created_at", "created_at"),
    )


class CollectorHeartbeatRecord(Base):
    __tablename__ = "collector_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    safe_error_type: Mapped[str | None] = mapped_column(String(120))
    safe_error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_collector_heartbeat_last_at", "last_heartbeat_at"),)


class OperationalEventRecord(Base):
    __tablename__ = "operational_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    event_code: Mapped[str] = mapped_column(String(120), nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(120))
    safe_message: Mapped[str] = mapped_column(String(500), nullable=False)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="SET NULL")
    )
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    context: Mapped[str | None] = mapped_column(String(120))
    metadata_json: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "severity IN ('INFO','WARN','ERROR','CRITICAL')",
            name="ck_operational_event_severity",
        ),
        Index("ix_operational_events_created_at", "created_at"),
        Index("ix_operational_events_component", "component"),
        Index("ix_operational_events_severity", "severity"),
        Index("ix_operational_events_code", "event_code"),
    )


class NotificationDeliveryRecord(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    event_code: Mapped[str] = mapped_column(String(120), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_ref_type: Mapped[str | None] = mapped_column(String(64))
    subject_ref_id: Mapped[str | None] = mapped_column(String(240))
    message_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rendered_message: Mapped[str] = mapped_column(String(4096), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transient_failure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error_type: Mapped[str | None] = mapped_column(String(120))
    safe_error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("channel", "dedupe_key", name="uq_notification_channel_dedupe"),
        CheckConstraint(
            "status IN ('PENDING','SENT','FAILED','SKIPPED')",
            name="ck_notification_delivery_status",
        ),
        CheckConstraint(
            "priority IN ('INFO','IMPORTANT','CRITICAL')",
            name="ck_notification_priority",
        ),
        Index("ix_notification_deliveries_created_at", "created_at"),
        Index("ix_notification_deliveries_status", "status"),
        Index("ix_notification_deliveries_event_code", "event_code"),
        Index("ix_notification_deliveries_dedupe_key", "dedupe_key"),
    )


# Register isolated paper tables for Alembic metadata and offline create_all tests.
from app.paper.models import PaperCursor, PaperEvent, PaperTrade  # noqa: F401, E402
