from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

AI_VERSION = "phase5_v1"
PROMPT_VERSION = "phase5_prompt_v2"

ConfidencePercent = Annotated[
    float,
    Field(
        ge=0,
        le=100,
        description=(
            "Confidence percentage on a 0 to 100 scale, where 0 is no confidence "
            "and 100 is maximum confidence. Do not use a 0 to 1 probability."
        ),
    ),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarketView(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGE = "RANGE"
    UNCERTAIN = "UNCERTAIN"


class AgreementStatus(str, Enum):
    AGREE = "AGREE"
    PARTIAL = "PARTIAL"
    DISAGREE = "DISAGREE"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class Zone(StrictModel):
    low: float
    high: float


class SnapshotInput(StrictModel):
    timestamp: datetime
    spot: float
    future: float | None
    india_vix: float | None
    atm: float
    expiry: date


class DataQualityInput(StrictModel):
    evidence_quality: str
    intraday_oi_usable: bool
    volume_usable: bool
    warnings: list[str]


class Phase3Summary(StrictModel):
    local_pcr_oi: float | None
    local_pcr_oi_change: float | None
    top_call_oi: list[dict[str, Any]]
    top_put_oi: list[dict[str, Any]]
    top_call_oi_additions: list[dict[str, Any]] | None
    top_put_oi_additions: list[dict[str, Any]] | None
    top_call_oi_reductions: list[dict[str, Any]] | None
    top_put_oi_reductions: list[dict[str, Any]] | None
    support_clusters: list[dict[str, Any]]
    resistance_clusters: list[dict[str, Any]]
    futures_basis: float | None
    vix_regime: str | None
    observed_opening_range: dict[str, Any]
    collector_observed_high: float
    collector_observed_low: float
    positioning_summary: dict[str, int]


class Phase4Summary(StrictModel):
    regime: str
    confidence: ConfidencePercent
    evidence_quality: str
    bull_score: float
    bear_score: float
    range_score: float
    bull_evidence: list[str]
    bear_evidence: list[str]
    range_evidence: list[str]
    warnings: list[str]
    reason_codes: list[str]


class AIResearchInput(StrictModel):
    snapshot_id: int
    snapshot: SnapshotInput
    data_quality: DataQualityInput
    phase3_summary: Phase3Summary
    phase4: Phase4Summary


class AIResearchModelOutput(StrictModel):
    market_view: MarketView
    confidence: ConfidencePercent
    support_zone: Zone | None
    resistance_zone: Zone | None
    key_observations: list[str]
    bullish_evidence: list[str]
    bearish_evidence: list[str]
    range_evidence: list[str]
    risks: list[str]
    missing_evidence: list[str]
    what_would_change_view: list[str]
    research_summary: str = Field(max_length=1000)


class ProviderUsage(StrictModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ProviderResult(StrictModel):
    output: AIResearchModelOutput
    provider: str
    model: str
    requested_at: datetime
    responded_at: datetime
    latency_ms: int
    usage: ProviderUsage


class AIResearchResult(StrictModel):
    snapshot_id: int
    ai_version: str = AI_VERSION
    prompt_version: str = PROMPT_VERSION
    deterministic_regime: str
    market_view: MarketView
    confidence: ConfidencePercent
    original_ai_confidence: ConfidencePercent
    confidence_capped: bool
    cap_reason: str | None
    agreement_status: AgreementStatus
    support_zone: Zone | None
    resistance_zone: Zone | None
    key_observations: list[str]
    bullish_evidence: list[str]
    bearish_evidence: list[str]
    range_evidence: list[str]
    risks: list[str]
    missing_evidence: list[str]
    what_would_change_view: list[str]
    research_summary: str
    provider: str
    model: str
    requested_at: datetime
    responded_at: datetime
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    estimated_cost_usd: float | None
