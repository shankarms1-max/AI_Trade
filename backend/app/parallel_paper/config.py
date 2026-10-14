"""Frozen, configurable experiment parameters and per-strategy execution limits."""
from __future__ import annotations

from dataclasses import replace
from datetime import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.scalper.config import ScalperConfig

STRATEGIES = ("ORB_15", "ORB_30", "ORB_60", "EMA_PULLBACK", "SUPERTREND_RSI_VOLUME")
VERSIONS = {name: f"{name}_V1" for name in STRATEGIES}
CAPTURE_MODE = "PARALLEL_PAPER"


class StrategyRisk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_trades_per_day: int = Field(default=6, ge=0, le=100)
    max_consecutive_losses: int = Field(default=3, ge=1, le=20)
    daily_loss_limit: float = Field(default=50000, gt=0, allow_inf_nan=False)
    entry_start: time = time(9, 15)
    entry_end: time = time(14, 45)
    cooldown_after_exit_seconds: int = Field(default=60, ge=0)
    cooldown_after_loss_seconds: int = Field(default=180, ge=0)


class ExperimentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    bar_minutes: int = Field(default=3, ge=1, le=15)
    max_sequence_gap_seconds: int = Field(default=45, ge=15, le=60)
    orb_confirmation_seconds: int = Field(default=30, ge=0, le=180)
    orb_allow_reentry: bool = False
    ema_fast: int = Field(default=9, ge=2, le=100)
    ema_slow: int = Field(default=21, ge=3, le=200)
    ema_zone_tolerance_bps: float = Field(default=5, ge=0, allow_inf_nan=False)
    ema_resumption_bps: float = Field(default=2, gt=0, allow_inf_nan=False)
    ema_setup_lifetime_bars: int = Field(default=5, ge=1, le=30)
    atr_period: int = Field(default=10, ge=2, le=100)
    supertrend_multiplier: float = Field(default=3, gt=0, allow_inf_nan=False)
    rsi_period: int = Field(default=14, ge=2, le=100)
    rsi_bull: float = Field(default=55, gt=50, le=100)
    rsi_bear: float = Field(default=45, ge=0, lt=50)
    volume_baseline_bars: int = Field(default=20, ge=2, le=100)
    volume_min_ratio: float = Field(default=1.20, gt=0, allow_inf_nan=False)
    risk: dict[str, StrategyRisk] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parameters(self) -> ExperimentConfig:
        if self.ema_fast >= self.ema_slow or set(self.risk) - set(STRATEGIES):
            raise ValueError("PARALLEL_PAPER_PARAMETERS_INVALID")
        return self

    def execution(self, settings: Any, strategy: str) -> ScalperConfig:
        base = ScalperConfig.from_settings(settings)
        limits = self.risk.get(strategy, StrategyRisk())
        if not limits.entry_start < limits.entry_end < base.forced_exit_time:
            raise ValueError("PARALLEL_PAPER_SESSION_TIMES_INVALID")
        return replace(base, lots=1, max_open_positions=1,
                       start_time=limits.entry_start, entry_end_time=limits.entry_end,
                       max_trades_per_day=limits.max_trades_per_day,
                       max_consecutive_losses=limits.max_consecutive_losses,
                       hard_daily_loss=limits.daily_loss_limit,
                       cooldown_after_exit_seconds=limits.cooldown_after_exit_seconds,
                       cooldown_after_loss_seconds=limits.cooldown_after_loss_seconds)

    def strategy_payload(self, strategy: str) -> dict[str, Any]:
        shared: dict[str, Any] = {
            "max_sequence_gap_seconds": self.max_sequence_gap_seconds,
            "price_source": "SAME_CONTRACT_NIFTY_FUTURE"}
        if strategy.startswith("ORB_"):
            shared.update(range_minutes=int(strategy.split("_")[1]),
                          confirmation_seconds=self.orb_confirmation_seconds,
                          allow_reentry=self.orb_allow_reentry)
        elif strategy == "EMA_PULLBACK":
            shared["bar_minutes"] = self.bar_minutes
            shared.update({key: getattr(self, key) for key in (
                "ema_fast", "ema_slow", "ema_zone_tolerance_bps",
                "ema_resumption_bps", "ema_setup_lifetime_bars")})
        else:
            shared.update(bar_minutes=self.bar_minutes,
                          volume_source="FIXED_SESSION_ATM_CE_PE_CUMULATIVE_DELTA_V1")
            shared.update({key: getattr(self, key) for key in (
                "atr_period", "supertrend_multiplier", "rsi_period", "rsi_bull",
                "rsi_bear", "volume_baseline_bars", "volume_min_ratio")})
        return shared
