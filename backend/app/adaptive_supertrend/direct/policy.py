"""Frozen AST_FUTURES_V1_DIRECT policy. Strategy parameters are literals and cannot be configured."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.adaptive_supertrend.config import StrictPolicy
from app.research.manifest import digest, json_value


class DirectPolicy(StrictPolicy):
    strategy_id: Literal["AST_FUTURES_V1_DIRECT"] = "AST_FUTURES_V1_DIRECT"
    strategy_version: Literal["AST_FUTURES_V1_DIRECT"] = "AST_FUTURES_V1_DIRECT"
    instrument: Literal["NIFTY_FUTURES"] = "NIFTY_FUTURES"
    indicator_policy: Literal["WILDER_FINAL_BANDS_V1"] = "WILDER_FINAL_BANDS_V1"
    bar_minutes: Literal[5] = 5
    opening_range_ist: Literal["09:15:00-09:30:00"] = "09:15:00-09:30:00"
    atr_period: Literal[22] = 22
    multiplier_tenths: Literal[39] = 39
    regime: Literal["NONE"] = "NONE"
    optimizer: Literal["DISABLED"] = "DISABLED"
    session_exit_time_ist: Literal["15:20:00"] = "15:20:00"
    fill_basis: Literal["FUTURES_LTP_PAPER_V1"] = "FUTURES_LTP_PAPER_V1"
    lots: Literal[1] = 1
    max_open_positions: Literal[1] = 1
    # Operational settings. They never alter signal semantics.
    lot_size: int = Field(gt=0, strict=True)
    max_sequence_gap_seconds: int = Field(default=45, ge=15, le=60)
    entry_fill_window_seconds: int = Field(default=60, ge=15, le=240)
    forced_exit_grace_seconds: int = Field(default=120, ge=15, le=480)
    history_sessions: int = Field(default=25, ge=2, le=60)
    friction_points_per_round_trip: float | None = Field(default=None, ge=0)

    @property
    def policy_hash(self) -> str:
        return digest(json_value(self.model_dump(mode="json")))
