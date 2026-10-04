from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

RISK_VERSION = "phase7_v1"


class RiskModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RiskDecisionType(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class EvaluationContext(str, Enum):
    LIVE = "LIVE"
    SHADOW = "SHADOW"
    HISTORICAL = "HISTORICAL"


class RiskCheck(RiskModel):
    check_code: str
    status: CheckStatus
    actual_value: Any = None
    threshold: Any = None
    message: str


class RiskDecision(RiskModel):
    market_snapshot_id: int
    regime_snapshot_id: int
    strategy_candidate_set_id: int | None
    candidate_reference: str | None
    candidate_fingerprint: str | None
    strategy_version: str
    risk_version: str = RISK_VERSION
    decision: RiskDecisionType
    candidate_strategy: str
    max_profit_per_unit: float | None
    max_loss_per_unit: float | None
    lot_size: int | None
    max_profit_per_lot: float | None
    max_loss_per_lot: float | None
    estimated_capital_required: float | None
    capital_basis: str | None
    capital_at_risk_pct: float | None = None
    reward_to_risk_ratio: float | None
    credit_to_width_ratio: float | None
    selection_score: float | None
    checks: list[RiskCheck]
    failed_checks: list[str]
    warnings: list[str]
    reason_codes: list[str]
    created_at: datetime


class RiskEvaluationSet(RiskModel):
    snapshot_id: int
    strategy_version: str
    risk_version: str = RISK_VERSION
    candidate_count: int = Field(ge=0)
    approved_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    not_applicable: bool
    decisions: list[RiskDecision]
    best_approved_candidate: str | None
    created_at: datetime

    @model_validator(mode="after")
    def counts_match(self) -> "RiskEvaluationSet":
        approved = sum(item.decision == RiskDecisionType.APPROVED for item in self.decisions)
        rejected = sum(item.decision == RiskDecisionType.REJECTED for item in self.decisions)
        if approved != self.approved_count or rejected != self.rejected_count:
            raise ValueError("risk decision counts do not match")
        if self.not_applicable != any(
            item.decision == RiskDecisionType.NOT_APPLICABLE for item in self.decisions
        ):
            raise ValueError("not_applicable does not match decisions")
        return self
