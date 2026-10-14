"""Deterministic EMA and Wilder ATR/RSI/Supertrend over completed bars only."""
from __future__ import annotations

from app.parallel_paper.bars import Bar


def ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    value = sum(values[:period]) / period
    result[period - 1] = value
    factor = 2 / (period + 1)
    for index in range(period, len(values)):
        value += factor * (values[index] - value)
        result[index] = value
    return result


def wilder(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    average = sum(values[:period]) / period
    result[period - 1] = average
    for index in range(period, len(values)):
        average = (average * (period - 1) + values[index]) / period
        result[index] = average
    return result


def rsi(values: list[float], period: int) -> float | None:
    if len(values) <= period:
        return None
    changes = [right - left for left, right in zip(values, values[1:])]
    gain = wilder([max(value, 0) for value in changes], period)[-1]
    loss = wilder([max(-value, 0) for value in changes], period)[-1]
    assert gain is not None and loss is not None
    if gain == 0 and loss == 0:
        return 50.0
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def supertrend(bars: list[Bar], period: int, multiplier: float) -> dict:
    ranges = [max(bar.high - bar.low,
                  abs(bar.high - bars[index - 1].close) if index else 0,
                  abs(bar.low - bars[index - 1].close) if index else 0)
              for index, bar in enumerate(bars)]
    atrs = wilder(ranges, period)
    upper = lower = previous_close = None
    direction = None
    value = atr = None
    for bar, atr in zip(bars, atrs):
        if atr is None:
            continue
        midpoint = (bar.high + bar.low) / 2
        basic_upper, basic_lower = midpoint + multiplier * atr, midpoint - multiplier * atr
        if upper is None or lower is None or previous_close is None:
            upper, lower = basic_upper, basic_lower
            direction = "BULL" if bar.close >= midpoint else "BEAR"
        else:
            upper = basic_upper if basic_upper < upper or previous_close > upper else upper
            lower = basic_lower if basic_lower > lower or previous_close < lower else lower
            if direction == "BEAR" and bar.close > upper:
                direction = "BULL"
            elif direction == "BULL" and bar.close < lower:
                direction = "BEAR"
        value = lower if direction == "BULL" else upper
        previous_close = bar.close
    return {"supertrend": value, "supertrend_direction": direction, "atr": atr}
