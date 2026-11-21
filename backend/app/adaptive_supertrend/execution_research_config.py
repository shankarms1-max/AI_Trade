"""Explicit, bounded pass-one research inputs; never a runtime configuration."""

from datetime import time
from typing import Literal

from pydantic import Field, model_validator

from app.adaptive_supertrend.config import AstPolicy, ExecutionPolicy, SafetyPolicy, StrictPolicy
from app.research.manifest import digest

MAX_CANDIDATES = 32
MAX_OBSERVATIONS = 100_000
MAX_EVALUATIONS = 1_500_000
MAX_GRID_FITS = 128


class SignalOnlyPolicy(AstPolicy):
    """Pure decide() view: execution is not required to observe a futures signal.

    Serialization/hash are identical to the supplied signal policy. No execution
    configuration is fabricated; this view is never passed to a paper worker.
    """

    @property
    def missing(self) -> list[str]:
        return [name for name in super().missing if name != "execution"]


class Liquidity(StrictPolicy):
    min_open_interest: int = Field(ge=0)
    min_volume: int = Field(ge=0)


class ResearchControls(StrictPolicy):
    entry_end_time: time
    hard_daily_loss: float = Field(gt=0)
    max_trades_per_day: int = Field(ge=1, le=1000)
    max_consecutive_losses: int = Field(ge=1, le=1000)
    cooldown_after_loss_seconds: int = Field(ge=0)
    cooldown_after_exit_seconds: int = Field(ge=0)
    entry_ttl_seconds: int = Field(gt=0, le=300)
    kill_switch: bool
    market_events_json: str
    safety: SafetyPolicy

    @model_validator(mode="after")
    def hard_only(self):
        if self.safety.label != "HARD_SAFETY_ONLY":
            raise ValueError("AST_EXECUTION_RESEARCH_REQUIRES_HARD_SAFETY_ONLY")
        return self


class Construction(StrictPolicy):
    name: str = Field(min_length=1, max_length=64)
    allowed_widths: tuple[int, ...]
    min_short_distance_points: Literal[100, 150, 200]
    min_credit_to_width: float
    max_bid_ask_spread_pct: float
    max_quote_age_seconds: Literal[4]
    min_credit: Literal[5]
    liquidity_profile: str = Field(min_length=1)
    max_loss_per_trade: Literal[7500, 10000, 12500]

    @model_validator(mode="after")
    def widths(self):
        if self.min_credit_to_width not in (0.10, 0.15, 0.20) or self.max_bid_ask_spread_pct not in (0.35, 0.70, 2.0):
            raise ValueError("AST_PASS_ONE_EXPLICIT_CREDIT_AND_SPREAD_AXIS_REQUIRED")
        if self.allowed_widths not in ((50,), (100,), (150,), (200,), (50, 100), (100, 150), (100, 150, 200)):
            raise ValueError("AST_PASS_ONE_WIDTH_SET_REQUIRED_NO_250_PLUS")
        return self


class ExecutionSpec(StrictPolicy):
    purpose: Literal["RESEARCH_CANDIDATES_NOT_RUNTIME_DEFAULTS"]
    controls_label: Literal["RESEARCH_CONTROL_VALUES_NOT_OPTIMIZED"]
    controls: ResearchControls
    liquidity_profiles: dict[str, Liquidity]
    candidates: tuple[Construction, ...] = Field(min_length=1, max_length=MAX_CANDIDATES)

    @model_validator(mode="after")
    def profiles(self):
        if len({c.name for c in self.candidates}) != len(self.candidates):
            raise ValueError("AST_UNIQUE_EXECUTION_CANDIDATE_NAMES_REQUIRED")
        if not self.liquidity_profiles or len(self.liquidity_profiles) > 8:
            raise ValueError("AST_BOUNDED_NAMED_LIQUIDITY_PROFILES_REQUIRED")
        for candidate in self.candidates:
            if candidate.liquidity_profile not in self.liquidity_profiles:
                raise ValueError("AST_UNKNOWN_LIQUIDITY_PROFILE")
            self.execution(candidate)  # All ordinary ExecutionPolicy constraints still apply.
        return self

    def execution(self, candidate: Construction) -> ExecutionPolicy:
        return ExecutionPolicy.model_validate(
            {
                **self.controls.model_dump(mode="json"),
                **candidate.model_dump(mode="json", exclude={"name", "liquidity_profile"}),
                **self.liquidity_profiles[candidate.liquidity_profile].model_dump(),
            }
        )

    def combined(self, signal: AstPolicy, candidate: Construction) -> AstPolicy:
        return AstPolicy.model_validate(
            {**signal.model_dump(mode="json"), "execution": self.execution(candidate).model_dump(mode="json")}
        )

    def candidate_id(self, candidate: Construction) -> str:
        return digest(
            {
                "candidate": candidate.model_dump(mode="json"),
                "liquidity": self.liquidity_profiles[candidate.liquidity_profile].model_dump(),
            }
        )
