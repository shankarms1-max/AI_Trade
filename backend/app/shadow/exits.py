from dataclasses import dataclass
from datetime import time

from app.regime.models import RegimeResult
from app.shadow.models import ShadowTrade, ShadowTradeMark


@dataclass(frozen=True)
class ShadowConfig:
    profit_target_credit_capture_pct: float = 50
    stop_loss_credit_multiple: float = 1.5
    force_exit_time: time = time(15, 20)
    hard_exit_cutoff: time = time(15, 29)
    exit_on_opposite_regime: bool = True
    exit_on_structural_breach: bool = True
    max_new_trades_per_day: int = 1
    allow_multiple_open_trades: bool = False


def pnl_thresholds(trade: ShadowTrade, config: ShadowConfig) -> tuple[float, float]:
    """Return the exact per-unit P&L levels already used by the exit policy."""
    return (
        trade.entry_credit * config.profit_target_credit_capture_pct / 100,
        -trade.entry_credit * config.stop_loss_credit_multiple,
    )


def exit_reason(
    trade: ShadowTrade,
    mark: ShadowTradeMark,
    regime: RegimeResult | None,
    config: ShadowConfig,
) -> str | None:
    target, stop = pnl_thresholds(trade, config)
    if mark.pnl_per_unit >= target:
        return "PROFIT_TARGET_EXIT"
    # Loss is the positive magnitude of negative P&L, not an exit-debit multiple.
    if mark.pnl_per_unit <= stop:
        return "STOP_LOSS_EXIT"
    if config.exit_on_opposite_regime and regime is not None:
        current = str(getattr(regime.regime, "value", regime.regime))
        if trade.strategy_type == "BULL_PUT_SPREAD" and current == "BEARISH":
            return "OPPOSITE_REGIME_EXIT"
        if trade.strategy_type == "BEAR_CALL_SPREAD" and current == "BULLISH":
            return "OPPOSITE_REGIME_EXIT"
    if config.exit_on_structural_breach and trade.structural_reference is not None:
        if trade.strategy_type == "BULL_PUT_SPREAD" and mark.spot < trade.structural_reference:
            return "STRUCTURAL_INVALIDATION_EXIT"
        if trade.strategy_type == "BEAR_CALL_SPREAD" and mark.spot > trade.structural_reference:
            return "STRUCTURAL_INVALIDATION_EXIT"
    if mark.timestamp.time().replace(tzinfo=None) >= config.force_exit_time:
        return "TIME_EXIT"
    return None
