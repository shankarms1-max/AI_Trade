from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

ALPHA_VERSION = "phase14_1_v1"


class AlphaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AlphaDirection(str, Enum):
    STRONG_BULLISH = "STRONG_BULLISH"
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    STRONG_BEARISH = "STRONG_BEARISH"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class JointAlphaDirection(str, Enum):
    STRONG_BULLISH_CONFIRMATION = "STRONG_BULLISH_CONFIRMATION"
    BULLISH_CONFIRMATION = "BULLISH_CONFIRMATION"
    STRONG_BEARISH_CONFIRMATION = "STRONG_BEARISH_CONFIRMATION"
    BEARISH_CONFIRMATION = "BEARISH_CONFIRMATION"
    NEUTRAL = "NEUTRAL"
    CONFLICT = "CONFLICT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class AlphaEvidenceQuality(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT = "INSUFFICIENT"


class AlphaStatus(str, Enum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    WARMING_UP = "WARMING_UP"


class AlphaHypothesis(str, Enum):
    CONTINUATION = "CONTINUATION"
    REVERSAL = "REVERSAL"


class AlphaValidity(str, Enum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"


class ParticipationState(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    EXTREME = "EXTREME"
    UNUSABLE = "UNUSABLE"


class CalculationMode(str, Enum):
    LIVE_ORIGINAL = "LIVE_ORIGINAL"
    HISTORICAL_REPLAY = "HISTORICAL_REPLAY"
    RESEARCH_RECOMPUTE = "RESEARCH_RECOMPUTE"


class AlphaFeatureSnapshot(AlphaModel):
    snapshot_id: int
    timestamp: datetime
    expiry: date
    price_source: str
    price_return: float | None
    alpha_1: float | None = Field(default=None, ge=0, le=1)
    atm_strike: float | None
    atm_ce_token: str | None
    atm_pe_token: str | None
    atm_ce_interval_volume: int | None
    atm_pe_interval_volume: int | None
    ce_volume_baseline: float | None
    pe_volume_baseline: float | None
    ce_volume_ratio: float | None
    pe_volume_ratio: float | None
    atm_volume_activity: float | None
    put_call_interval_volume_ratio: float | None
    signed_interval_volume_imbalance: float | None
    ce_observed_volatility: float | None
    pe_observed_volatility: float | None
    atm_option_volatility: float | None
    directional_impulse_raw: float | None
    alpha_2: float | None = Field(default=None, ge=0, le=1)
    alpha_1_direction: AlphaDirection
    alpha_2_direction: AlphaDirection
    joint_alpha_direction: JointAlphaDirection
    consecutive_confirmation_count: int = Field(ge=0)
    confirmed: bool
    evidence_quality: AlphaEvidenceQuality
    status: AlphaStatus
    rank_observations_alpha1: int = Field(ge=0)
    rank_observations_alpha2: int = Field(ge=0)
    volume_baseline_observations: int = Field(ge=0)
    volatility_return_observations: int = Field(ge=0)
    warnings: list[str]
    session_date: date | None = None
    session_id: str | None = None
    hypothesis_type: AlphaHypothesis = AlphaHypothesis.CONTINUATION
    calculation_mode: CalculationMode = CalculationMode.HISTORICAL_REPLAY
    lookback_clock_mode: str = "TRADING_MINUTES"
    effective_history_minutes: float = 0
    history_session_count: int = 0
    history_observation_count: int = 0
    target_horizon_seconds: int = 300
    actual_horizon_seconds: int | None = None
    horizon_error_seconds: int | None = None
    signed_log_return: float | None = None
    legacy_open_normalized_return: float | None = None
    rank_history_count: int = 0
    rank_history_trading_minutes: float = 0
    rank_history_sessions: int = 0
    reference_instrument_id: str | None = None
    reference_expiry: date | None = None
    reference_source_timestamp: datetime | None = None
    reference_age_seconds: float | None = None
    ce_volume_state: str = "MISSING"
    pe_volume_state: str = "MISSING"
    ce_interval_duration_seconds: float | None = None
    pe_interval_duration_seconds: float | None = None
    ce_gap_cumulative_delta: int | None = None
    pe_gap_cumulative_delta: int | None = None
    atm_changed_since_previous_snapshot: bool = False
    atm_ce_history_count: int = 0
    atm_pe_history_count: int = 0
    atm_distance_points: float | None = None
    atm_distance_percent: float | None = None
    activity_score: float | None = None
    volatility_context_score: float | None = None
    participation_state: ParticipationState = ParticipationState.UNUSABLE
    activity_baseline_method: str = "MEDIAN"
    activity_baseline_count: int = 0
    activity_ratio_capped: bool = False
    underlying_horizon_volatility: float | None = None
    standardized_return: float | None = None
    volatility_observation_count: int = 0
    volatility_coverage_minutes: float = 0
    legacy_alpha2_raw: float | None = None
    legacy_alpha2_rank: float | None = None
    validity_state: AlphaValidity = AlphaValidity.INVALID
    signal_strength: float = 0
    confirmation_reset_reason: str | None = None
    broker_oi_current_ce: int | None = None
    broker_oi_previous_ce: int | None = None
    broker_oi_change_ce: int | None = None
    local_oi_change_ce: int | None = None
    broker_oi_current_pe: int | None = None
    broker_oi_previous_pe: int | None = None
    broker_oi_change_pe: int | None = None
    local_oi_change_pe: int | None = None
    source_market_timestamp: datetime | None = None
    request_started_at: datetime | None = None
    response_received_at: datetime | None = None
    snapshot_persisted_at: datetime | None = None
    feature_calculated_at: datetime | None = None
    decision_at: datetime | None = None
    alpha_version: str = ALPHA_VERSION
