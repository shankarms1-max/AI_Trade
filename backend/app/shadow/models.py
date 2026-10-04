from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.strategy.models import CandidateLeg

SHADOW_VERSION = "phase8_v1"


class ShadowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShadowStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    INVALID = "INVALID"


class ShadowPricingBasis(str, Enum):
    BID_ASK = "BID_ASK"
    LTP_ESTIMATE = "LTP_ESTIMATE"


class ShadowTrade(ShadowModel):
    id: int | None = None
    market_snapshot_id_entry: int
    risk_decision_id: int
    candidate_fingerprint: str
    shadow_version: str = SHADOW_VERSION
    strategy_type: str
    expiry: date
    entry_timestamp: datetime
    entry_spot: float
    short_leg: CandidateLeg
    long_leg: CandidateLeg
    entry_short_price: float
    entry_long_price: float
    entry_credit: float = Field(gt=0)
    entry_pricing_basis: ShadowPricingBasis
    quote_age_seconds: float | None = None
    leg_time_skew_seconds: float | None = None
    depth_available: bool = False
    fill_quality_state: str = "UNVERIFIED_QUOTE_TIME"
    spread_width: float = Field(gt=0)
    lot_size: int | None
    max_profit_per_unit: float
    max_loss_per_unit: float
    max_profit_per_lot: float | None
    max_loss_per_lot: float | None
    structural_reference: float | None
    entry_regime_confidence: float
    entry_evidence_quality: str
    entry_vix_regime: str | None
    entry_alpha_1: float | None = None
    entry_alpha_2: float | None = None
    entry_joint_alpha_direction: str | None = None
    entry_alpha_evidence_quality: str | None = None
    entry_alpha_confirmation_count: int = 0
    credit_to_width_ratio: float
    source_candidate: dict[str, Any]
    source_risk_decision: dict[str, Any]
    status: ShadowStatus = ShadowStatus.OPEN
    exit_market_snapshot_id: int | None = None
    exit_timestamp: datetime | None = None
    exit_reason: str | None = None
    exit_short_price: float | None = None
    exit_long_price: float | None = None
    exit_debit: float | None = None
    realized_pnl_per_unit: float | None = None
    realized_pnl_per_lot: float | None = None
    mae_per_unit: float = 0
    mfe_per_unit: float = 0
    mae_per_lot: float | None = None
    mfe_per_lot: float | None = None
    holding_minutes: float | None = None
    warnings: list[str]
    reason_codes: list[str]
    created_at: datetime
    updated_at: datetime


class ShadowTradeMark(ShadowModel):
    id: int | None = None
    shadow_trade_id: int
    market_snapshot_id: int
    timestamp: datetime
    short_price: float
    long_price: float
    valuation_basis: ShadowPricingBasis
    quote_age_seconds: float | None = None
    leg_time_skew_seconds: float | None = None
    depth_available: bool = False
    fill_quality_state: str = "UNVERIFIED_QUOTE_TIME"
    exit_debit: float
    pnl_per_unit: float
    pnl_per_lot: float | None
    spot: float
    warnings: list[str]
    created_at: datetime


class ShadowEntryResult(ShadowModel):
    snapshot_id: int
    created: bool
    trade: ShadowTrade | None
    reason_codes: list[str]
