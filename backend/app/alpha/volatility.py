from math import log, sqrt


def observed_price_volatility(prices: list[float], minimum_returns: int) -> tuple[float | None, int]:
    returns = [log(right / left) for left, right in zip(prices, prices[1:]) if left > 0 and right > 0]
    if len(returns) < minimum_returns:
        return None, len(returns)
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    return sqrt(variance), len(returns)


def combined_option_volatility(ce: float | None, pe: float | None) -> float | None:
    if ce is None or pe is None:
        return None
    return (ce + pe) / 2
