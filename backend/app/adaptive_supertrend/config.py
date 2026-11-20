"""Explicit research policy; no guessed optimizer, regime or trading defaults."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.costs import CostSchedule
from app.research.manifest import digest, json_value
from app.scalper.config import ScalperConfig


class StrictPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Weights(StrictPolicy):
    profitability: float = Field(ge=0)
    drawdown: float = Field(ge=0)
    profit_factor: float = Field(ge=0)
    sharpe: float = Field(ge=0)
    frequency: float = Field(ge=0)
    consistency: float = Field(ge=0)
    neighbours: float = Field(ge=0)

    @model_validator(mode="after")
    def nonzero(self) -> Weights:
        if self.drawdown + self.profit_factor + self.sharpe + self.consistency + self.neighbours <= 0:
            raise ValueError("AST_ROBUSTNESS_WEIGHT_REQUIRED")
        return self


class OptimizerPolicy(StrictPolicy):
    lookback_sessions: int = Field(ge=1, le=1000)
    subperiods: int = Field(ge=2, le=50)
    minimum_trades: int = Field(ge=1)
    weights: Weights
    validation_sessions: int = Field(ge=1)
    out_of_sample_sessions: int = Field(ge=1)


class RegimePolicy(StrictPolicy):
    adx_period: int = Field(ge=2, le=100)
    adx_threshold: float = Field(ge=0, le=100)
    er_lookback: int = Field(ge=1, le=200)
    er_threshold: float = Field(ge=0, le=1)


class SafetyPolicy(StrictPolicy):
    label: Literal["HARD_SAFETY_ONLY", "LEGACY_SAFETY_BASELINE"]
    forced_exit_time: time
    profit_capture_pct: float | None = Field(default=None, gt=0, lt=100)
    stop_credit_multiple: float | None = Field(default=None, gt=1)
    trailing_activation_pct: float | None = Field(default=None, gt=0, le=100)
    trailing_giveback_pct: float | None = Field(default=None, gt=0, le=100)
    time_stop_minutes: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def baseline_explicit(self) -> SafetyPolicy:
        values = [
            getattr(self, name)
            for name in (
                "profit_capture_pct",
                "stop_credit_multiple",
                "trailing_activation_pct",
                "trailing_giveback_pct",
                "time_stop_minutes",
            )
        ]
        if self.label == "LEGACY_SAFETY_BASELINE" and any(value is None for value in values):
            raise ValueError("AST_LEGACY_SAFETY_VALUES_REQUIRED")
        if self.label == "HARD_SAFETY_ONLY" and any(value is not None for value in values):
            raise ValueError("AST_HARD_SAFETY_HAS_NO_LEGACY_TARGETS")
        return self


class ExecutionPolicy(StrictPolicy):
    allowed_widths: tuple[int, ...]
    entry_end_time: time
    min_short_distance_points: float = Field(ge=0)
    min_credit: float = Field(gt=0)
    min_credit_to_width: float = Field(gt=0, lt=1)
    max_bid_ask_spread_pct: float = Field(gt=0)
    max_quote_age_seconds: int = Field(gt=0, le=60)
    min_open_interest: int = Field(ge=0)
    min_volume: int = Field(ge=0)
    max_loss_per_trade: float = Field(gt=0)
    hard_daily_loss: float = Field(gt=0)
    max_trades_per_day: int = Field(ge=1)
    max_consecutive_losses: int = Field(ge=1)
    cooldown_after_loss_seconds: int = Field(ge=0)
    cooldown_after_exit_seconds: int = Field(ge=0)
    entry_ttl_seconds: int = Field(gt=0)
    kill_switch: bool = False
    market_events_json: str = "[]"
    safety: SafetyPolicy

    @model_validator(mode="after")
    def limits(self) -> ExecutionPolicy:
        if (
            not self.allowed_widths
            or tuple(sorted(set(self.allowed_widths))) != self.allowed_widths
            or any(width <= 0 or width % 50 for width in self.allowed_widths)
        ):
            raise ValueError("AST_ALLOWED_WIDTHS_REQUIRED")
        if not time(9, 30) < self.entry_end_time < self.safety.forced_exit_time <= time(15, 30):
            raise ValueError("AST_SESSION_TIMES_INVALID")
        return self

    def scalper(self, settings) -> ScalperConfig:
        # Reuse only construction/book validators. Signal/legacy exit fields are never consulted by AST.
        fields = self.model_dump(exclude={"safety", "market_events_json"})
        fields.update(
            start_time=time(9, 30),
            forced_exit_time=self.safety.forced_exit_time,
            lots=1,
            max_open_positions=1,
            event_configuration_json=self.market_events_json,
        )
        return replace(ScalperConfig.from_settings(settings), **fields)


class AstPolicy(StrictPolicy):
    strategy_id: Literal["AST_V1"] = "AST_V1"
    strategy_version: Literal["AST_V1"] = "AST_V1"
    indicator_policy: Literal["WILDER_FINAL_BANDS_V1"] = "WILDER_FINAL_BANDS_V1"
    fill_policy: Literal["NEXT_OBSERVATION_EXACT_BID_ASK_V1"] = "NEXT_OBSERVATION_EXACT_BID_ASK_V1"
    exit_book_policy: Literal["PHASE15_EXIT_BOOK_V1"] = "PHASE15_EXIT_BOOK_V1"
    bar_minutes: Literal[5] = 5
    opening_range_minutes: Literal[15] = 15
    atr_period_min: Literal[5] = 5
    atr_period_max: Literal[50] = 50
    multiplier_min_tenths: Literal[10] = 10
    multiplier_max_tenths: Literal[50] = 50
    multiplier_step_tenths: Literal[1] = 1
    lots: Literal[1] = 1
    max_sequence_gap_seconds: int = Field(default=45, ge=15, le=60)
    optimizer: OptimizerPolicy | None = None
    regime: RegimePolicy | None = None
    execution: ExecutionPolicy | None = None
    costs: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_costs(self) -> AstPolicy:
        self.cost_schedule()
        return self

    def cost_schedule(self) -> CostSchedule:
        values = dict(self.costs)
        for key in ("effective_from", "effective_to"):
            if isinstance(values.get(key), str):
                values[key] = date.fromisoformat(values[key])
        return CostSchedule(**values)

    @property
    def missing(self) -> list[str]:
        return [name for name in ("optimizer", "regime", "execution") if getattr(self, name) is None]

    @property
    def policy_hash(self) -> str:
        return digest(json_value(self.model_dump(mode="json")))
