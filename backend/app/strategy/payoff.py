from dataclasses import dataclass

from app.strategy.models import StrategyType


@dataclass(frozen=True)
class Payoff:
    width: float
    max_profit: float
    max_loss: float
    breakeven: float


def calculate_payoff(
    strategy_type: StrategyType, short_strike: float, long_strike: float, credit: float
) -> Payoff | None:
    if strategy_type == StrategyType.BULL_PUT_SPREAD:
        width = short_strike - long_strike
        breakeven = short_strike - credit
    elif strategy_type == StrategyType.BEAR_CALL_SPREAD:
        width = long_strike - short_strike
        breakeven = short_strike + credit
    else:
        return None
    max_loss = width - credit
    if width <= 0 or credit <= 0 or max_loss <= 0:
        return None
    return Payoff(width, credit, max_loss, breakeven)
