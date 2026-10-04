from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

REGIME_VERSION = "phase4_v1"


class Regime(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGE = "RANGE"
    NO_TRADE = "NO_TRADE"


class SignalDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceQuality(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT = "INSUFFICIENT"


class SignalGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    direction: SignalDirection
    score: float = Field(ge=0, le=1)
    strength: float = Field(ge=0, le=1)
    reason_codes: list[str]
    details: dict[str, Any] = Field(default_factory=dict)


class RegimeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: int
    feature_snapshot_id: int
    timestamp: datetime
    regime: Regime
    bull_score: float
    bear_score: float
    range_score: float
    confidence: float = Field(ge=0, le=100)
    evidence_quality: EvidenceQuality
    signal_groups: list[SignalGroup]
    bull_evidence: list[str]
    bear_evidence: list[str]
    range_evidence: list[str]
    warnings: list[str]
    missing_inputs: list[str]
    risk_flags: list[str]
    regime_version: str = REGIME_VERSION
