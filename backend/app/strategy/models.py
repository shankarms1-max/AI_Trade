from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.strategy.economics_models import CreditSpreadEconomics, StrategyFamily

STRATEGY_VERSION = "phase6_v1"


class StrategyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrategyType(str, Enum):
    BULL_PUT_SPREAD = "BULL_PUT_SPREAD"
    BEAR_CALL_SPREAD = "BEAR_CALL_SPREAD"
    NONE = "NONE"


class PricingBasis(str, Enum):
    BID_ASK = "BID_ASK"
    LTP_ESTIMATE = "LTP_ESTIMATE"


class CandidateLeg(StrategyModel):
    exchange: str = "nse_fo"
    action: Literal["SELL", "BUY"]
    option_type: Literal["CE", "PE"]
    strike: float = Field(gt=0)
    trading_symbol: str
    instrument_token: str | None
    ltp: float | None
    open_interest: int | None
    volume: int | None
    bid: float | None
    ask: float | None
    delta: float | None
    expiry: date


class LiquidityMetrics(StrategyModel):
    short_open_interest: int | None
    long_open_interest: int | None
    short_volume: int | None
    long_volume: int | None
    short_bid_ask_spread_pct: float | None
    long_bid_ask_spread_pct: float | None
    volume_usable: bool


class CreditSpreadCandidate(CreditSpreadEconomics):
    candidate_id: str
    market_snapshot_id: int
    regime_snapshot_id: int
    strategy_version: str = STRATEGY_VERSION
    strategy_type: StrategyType
    expiry: date
    spot: float
    atm_strike: float
    short_leg: CandidateLeg
    long_leg: CandidateLeg
    spread_width: float = Field(gt=0)
    net_credit: float = Field(gt=0)
    credit_to_width_ratio: float = Field(gt=0)
    max_profit: float = Field(gt=0)
    max_loss: float = Field(gt=0)
    breakeven: float
    max_profit_per_lot: float | None = None
    max_loss_per_lot: float | None = None
    lot_size: int | None = None
    short_leg_distance_from_spot: float = Field(gt=0)
    short_leg_distance_pct: float = Field(gt=0)
    support_or_resistance_reference: float | None
    liquidity_metrics: LiquidityMetrics
    pricing_basis: PricingBasis
    selection_score: float = Field(ge=0, le=100)
    eligibility_status: Literal["ELIGIBLE"] = "ELIGIBLE"
    reason_codes: list[str]
    warnings: list[str]
    created_at: datetime

    @model_validator(mode="after")
    def defined_risk_structure(self) -> "CreditSpreadCandidate":
        if self.short_leg.expiry != self.long_leg.expiry or self.expiry != self.short_leg.expiry:
            raise ValueError("candidate legs must share snapshot expiry")
        if self.short_leg.option_type != self.long_leg.option_type:
            raise ValueError("candidate legs must use the same option type")
        if self.strategy_type == StrategyType.BULL_PUT_SPREAD:
            if self.short_leg.option_type != "PE" or self.long_leg.strike >= self.short_leg.strike:
                raise ValueError("invalid bull put defined-risk structure")
        elif self.strategy_type == StrategyType.BEAR_CALL_SPREAD:
            if self.short_leg.option_type != "CE" or self.long_leg.strike <= self.short_leg.strike:
                raise ValueError("invalid bear call defined-risk structure")
        return self


class StrategyCandidateSet(StrategyModel):
    policy_hash: str | None = None
    research_run_id: str | None = None
    execution_mode: str | None = None
    strategy_logic_version: str | None = None
    strategy_family: StrategyFamily | None = None
    side_safety: dict = Field(default_factory=dict)
    snapshot_id: int
    regime_snapshot_id: int
    regime: str
    strategy_type: StrategyType
    eligible: bool
    candidates: list[CreditSpreadCandidate]
    candidate_count: int = Field(ge=0)
    reason_codes: list[str]
    warnings: list[str]
    strategy_version: str = STRATEGY_VERSION
    created_at: datetime

    @model_validator(mode="after")
    def candidate_count_matches(self) -> "StrategyCandidateSet":
        if self.candidate_count != len(self.candidates):
            raise ValueError("candidate_count does not match candidates")
        if self.eligible != bool(self.candidates):
            raise ValueError("eligible must match candidate presence")
        if not self.eligible and self.strategy_type != StrategyType.NONE:
            raise ValueError("ineligible result must use strategy NONE")
        return self
