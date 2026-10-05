from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class StrategyFamily(str, Enum):
    DIRECTIONAL_CREDIT_SPREAD = "DIRECTIONAL_CREDIT_SPREAD"
    THETA_CARRY_CREDIT_SPREAD = "THETA_CARRY_CREDIT_SPREAD"
    NO_TRADE = "NO_TRADE"


class CreditSpreadEconomics(BaseModel):
    """Research proxies; null fields identify legacy/unavailable measurements."""

    model_config = ConfigDict(extra="forbid")
    strategy_logic_version: str | None = None
    policy_hash: str | None = None
    research_run_id: str | None = None
    execution_mode: str | None = None
    economics_basis: str | None = None
    strategy_family: StrategyFamily | None = None
    market_bias: str | None = None
    directional_strength: str | None = None
    directional_score: float | None = None
    survival_score: float | None = None
    carry_score: float | None = None
    survival_components: dict[str, float] = Field(default_factory=dict)
    carry_components: dict[str, float] = Field(default_factory=dict)
    ranking_components: dict[str, float] = Field(default_factory=dict)
    ranking_penalties: dict[str, float] = Field(default_factory=dict)
    expected_move_source: str | None = None
    expected_move_points: float | None = None
    expected_move_percent: float | None = None
    short_strike_distance_points: float | None = None
    short_strike_distance_percent: float | None = None
    short_strike_distance_expected_move_units: float | None = None
    distance_in_expected_move_units: float | None = None
    distance_beyond_structure_points: float | None = None
    gap_to_opposing_structure_points: float | None = None
    required_short_strike_buffer: float | None = None
    days_to_expiry: int | None = None
    fractional_time_to_expiry: float | None = None
    dte_bucket: str | None = None
    time_remaining_in_session_minutes: float | None = None
    gamma_risk_state: str | None = None
    theta_gamma_balance_state: str | None = None
    risk_state: str | None = None
    spread_width_points: float | None = None
    net_credit_per_unit: float | None = None
    max_loss_per_unit: float | None = None
    gross_credit: float | None = None
    estimated_cost: float | None = None
    net_credit_after_cost: float | None = None
    cost_components: dict[str, Any] = Field(default_factory=dict)
    cost_estimate_complete: bool = False
    credit_to_max_loss: float | None = None
    credit_to_expected_move: float | None = None
    credit_per_dte: float | None = None
    premium_retention_ratio: float | None = None
    carry_model: str | None = None
    side_safety: dict[str, Any] = Field(default_factory=dict)
