"""Independent scalper entry limits and cooldowns."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.scalper.config import ScalperConfig
from app.scalper.models import ScalperCandidate, ScalperSignal


@dataclass(frozen=True)
class ScalperRiskState:
    open_positions: int = 0
    executed_trades_today: int = 0
    gross_realized_today: float = 0.0
    consecutive_losses: int = 0
    last_exit_at: datetime | None = None
    last_loss_at: datetime | None = None
    unresolved_positions: int = 0


@dataclass(frozen=True)
class ScalperRiskDecision:
    approved: bool
    reasons: tuple[str, ...]
    defined_max_loss_per_lot: float
    broker_margin: None = None


def entry_blockers(signal: ScalperSignal, state: ScalperRiskState,
                   config: ScalperConfig, at: datetime, *,
                   event_blocked: bool = False) -> list[str]:
    reasons: list[str] = []
    local_time = at.time().replace(tzinfo=None)
    if config.kill_switch:
        reasons.append("KILL_SWITCH_ACTIVE")
    if event_blocked:
        reasons.append("MARKET_EVENT_BLOCKED")
    if not config.start_time <= local_time <= config.entry_end_time:
        reasons.append("OUTSIDE_ENTRY_WINDOW")
    threshold = (signal.applicable_entry_threshold if signal.applicable_entry_threshold is not None
                 else config.signal_min_score)
    if not signal.confirmed or signal.score < threshold:
        reasons.append("SIGNAL_NOT_CONFIRMED")
    if state.open_positions + state.unresolved_positions >= config.max_open_positions:
        reasons.append("MAX_OPEN_POSITIONS_REACHED")
    if state.executed_trades_today >= config.max_trades_per_day:
        reasons.append("MAX_TRADES_PER_DAY_REACHED")
    if state.gross_realized_today <= -config.hard_daily_loss:
        reasons.append("HARD_DAILY_LOSS_REACHED")
    if state.consecutive_losses >= config.max_consecutive_losses:
        reasons.append("MAX_CONSECUTIVE_LOSSES_REACHED")
    if (state.last_exit_at and
            (at - state.last_exit_at).total_seconds() < config.cooldown_after_exit_seconds):
        reasons.append("ANY_EXIT_COOLDOWN_ACTIVE")
    if (state.last_loss_at and
            (at - state.last_loss_at).total_seconds() < config.cooldown_after_loss_seconds):
        reasons.append("LOSS_COOLDOWN_ACTIVE")
    return reasons


def evaluate_entry(candidate: ScalperCandidate, signal: ScalperSignal,
                   state: ScalperRiskState, config: ScalperConfig,
                   at: datetime, *, event_blocked: bool = False) -> ScalperRiskDecision:
    reasons = entry_blockers(signal, state, config, at, event_blocked=event_blocked)
    if candidate.defined_max_loss_per_lot > config.max_loss_per_trade:
        reasons.append("MAX_LOSS_PER_TRADE_EXCEEDED")
    return ScalperRiskDecision(not reasons, tuple(reasons),
                               candidate.defined_max_loss_per_lot)
