"""Explicit research policy; no guessed optimizer, regime or trading defaults."""

from __future__ import annotations

from datetime import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.research.manifest import digest, json_value


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
    # Legacy policies without a mode retain their original ADX/ER semantics.
    mode: Literal["ADX_ER"] = "ADX_ER"
    adx_period: int = Field(ge=2, le=100)
    adx_threshold: float = Field(ge=0, le=100)
    er_lookback: int = Field(ge=1, le=200)
    er_threshold: float = Field(ge=0, le=1)


class NoRegimePolicy(StrictPolicy):
    mode: Literal["NONE"]


class FuturesExecutionPolicy(StrictPolicy):
    lot_size: int = Field(gt=0, strict=True)
    entry_end_time: time
    session_exit_time: time
    round_trip_cost_per_lot_inr: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_session_times(self) -> FuturesExecutionPolicy:
        if self.entry_end_time.tzinfo is not None or self.session_exit_time.tzinfo is not None:
            raise ValueError("AST_SESSION_TIMES_REQUIRE_LOCAL_IST")
        if not time(9, 30) < self.entry_end_time < self.session_exit_time <= time(15, 30):
            raise ValueError("AST_SESSION_TIMES_INVALID")
        return self


class AstPolicy(StrictPolicy):
    strategy_id: Literal["AST_FUTURES_V1"] = "AST_FUTURES_V1"
    strategy_version: Literal["AST_FUTURES_V1"] = "AST_FUTURES_V1"
    indicator_policy: Literal["WILDER_FINAL_BANDS_V1"] = "WILDER_FINAL_BANDS_V1"
    fill_policy: Literal["NEXT_OBSERVATION_FUTURES_LTP_V1"] = "NEXT_OBSERVATION_FUTURES_LTP_V1"
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
    regime: RegimePolicy | NoRegimePolicy | None = None
    execution: FuturesExecutionPolicy | None = None

    @property
    def missing(self) -> list[str]:
        return [name for name in ("optimizer", "regime", "execution") if getattr(self, name) is None]

    @property
    def policy_hash(self) -> str:
        return digest(json_value(self.model_dump(mode="json")))
