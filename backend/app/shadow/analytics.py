from collections import defaultdict
from statistics import mean
from typing import Callable

from app.shadow.models import ShadowStatus, ShadowTrade


def _pnl_metrics(values: list[float]) -> dict:
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": None if not values else len(wins) / len(values) * 100,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_pnl": sum(values),
        "average_win": None if not wins else mean(wins),
        "average_loss": None if not losses else mean(losses),
        "profit_factor": None if not losses else gross_profit / abs(gross_loss),
        "expectancy_per_trade": None if not values else mean(values),
        "max_single_trade_profit": None if not values else max(values),
        "max_single_trade_loss": None if not values else min(values),
        "max_drawdown": drawdown,
    }


def performance(trades: list[ShadowTrade]) -> dict:
    closed = [item for item in trades if item.status == ShadowStatus.CLOSED]
    per_unit = [item.realized_pnl_per_unit for item in closed if item.realized_pnl_per_unit is not None]
    per_lot = [item.realized_pnl_per_lot for item in closed if item.realized_pnl_per_lot is not None]
    holding = [item.holding_minutes for item in closed if item.holding_minutes is not None]
    return {
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "open_trades": sum(item.status == ShadowStatus.OPEN for item in trades),
        "invalid_trades": sum(item.status == ShadowStatus.INVALID for item in trades),
        "per_unit": _pnl_metrics(per_unit),
        "per_lot": _pnl_metrics(per_lot),
        "average_holding_minutes": None if not holding else mean(holding),
        "average_mfe_per_unit": None if not trades else mean(item.mfe_per_unit for item in trades),
        "average_mae_per_unit": None if not trades else mean(item.mae_per_unit for item in trades),
        "average_mfe_per_lot": None if not [i for i in trades if i.mfe_per_lot is not None]
        else mean(i.mfe_per_lot for i in trades if i.mfe_per_lot is not None),
        "average_mae_per_lot": None if not [i for i in trades if i.mae_per_lot is not None]
        else mean(i.mae_per_lot for i in trades if i.mae_per_lot is not None),
        "profit_target_frequency": None if not closed else sum(
            item.exit_reason == "PROFIT_TARGET" for item in closed
        ) / len(closed) * 100,
        "stop_loss_frequency": None if not closed else sum(
            item.exit_reason == "STOP_LOSS" for item in closed
        ) / len(closed) * 100,
    }


def _bucket_credit(value: float) -> str:
    if value < 0.05:
        return "LT_5_PCT"
    if value < 0.10:
        return "5_TO_10_PCT"
    return "GE_10_PCT"


def _bucket_alpha(value: float | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    boundaries = (0.70, 0.75, 0.80, 0.85, 0.90, 1.01)
    if value < boundaries[0]:
        return "LT_0.70"
    for left, right in zip(boundaries, boundaries[1:]):
        if left <= value < right:
            return f"{left:.2f}_TO_{min(right, 1.0):.2f}"
    return "UNAVAILABLE"


def breakdown(trades: list[ShadowTrade]) -> dict:
    dimensions: dict[str, Callable[[ShadowTrade], str]] = {
        "strategy_type": lambda item: item.strategy_type,
        "entry_hour": lambda item: str(item.entry_timestamp.hour),
        "day_of_week": lambda item: item.entry_timestamp.strftime("%A"),
        "expiry_dte": lambda item: str((item.expiry - item.entry_timestamp.date()).days),
        "regime_confidence_bucket": lambda item: f"{int(item.entry_regime_confidence // 10) * 10}s",
        "evidence_quality": lambda item: item.entry_evidence_quality,
        "vix_regime": lambda item: item.entry_vix_regime or "UNAVAILABLE",
        "pricing_basis": lambda item: item.entry_pricing_basis.value,
        "spread_width": lambda item: f"{item.spread_width:g}",
        "credit_to_width_bucket": lambda item: _bucket_credit(item.credit_to_width_ratio),
        "alpha_1_bucket": lambda item: _bucket_alpha(item.entry_alpha_1),
        "alpha_2_bucket": lambda item: _bucket_alpha(item.entry_alpha_2),
        "joint_alpha_state": lambda item: item.entry_joint_alpha_direction or "UNAVAILABLE",
        "alpha_evidence_quality": lambda item: item.entry_alpha_evidence_quality or "UNAVAILABLE",
        "alpha_confirmation_count": lambda item: str(item.entry_alpha_confirmation_count),
        "entry_window": lambda item: f"{item.entry_timestamp.hour:02d}:{(item.entry_timestamp.minute // 15) * 15:02d}",
        "volatility_bucket": lambda item: item.entry_vix_regime or "UNAVAILABLE",
    }
    result = {}
    for name, key_fn in dimensions.items():
        groups = defaultdict(list)
        for trade in trades:
            groups[key_fn(trade)].append(trade)
        result[name] = {key: performance(values) for key, values in sorted(groups.items())}
    return result
