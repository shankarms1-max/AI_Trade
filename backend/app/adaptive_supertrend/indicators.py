"""Reference Wilder smoothing, final-band SuperTrend, ADX and efficiency ratio."""

from __future__ import annotations

from math import isfinite

from app.parallel_paper.bars import Bar


def wilder(values: list[float], period: int) -> list[float | None]:
    if period < 1 or any(not isfinite(value) for value in values):
        raise ValueError("AST_INVALID_INDICATOR_INPUT")
    result: list[float | None] = [None] * len(values)
    if len(values) >= period:
        current = sum(values[:period]) / period
        result[period - 1] = current
        for index in range(period, len(values)):
            current = (current * (period - 1) + values[index]) / period
            result[index] = current
    return result


def true_ranges(bars: list[Bar]) -> list[float]:
    return [
        max(
            bar.high - bar.low,
            abs(bar.high - bars[index - 1].close) if index else 0,
            abs(bar.low - bars[index - 1].close) if index else 0,
        )
        for index, bar in enumerate(bars)
    ]


def atr(bars: list[Bar], period: int) -> list[float | None]:
    return wilder(true_ranges(bars), period)


def supertrend(
    bars: list[Bar], period: int, multiplier_tenths: int, atr_values: list[float | None] | None = None
) -> list[dict]:
    if not 5 <= period <= 50 or not 10 <= multiplier_tenths <= 50:
        raise ValueError("AST_PARAMETER_OUTSIDE_GRID")
    atrs = atr(bars, period) if atr_values is None else atr_values
    upper = lower = previous_close = None
    direction: str | None = None
    transition: str | None = None
    result: list[dict] = []
    for bar, value in zip(bars, atrs):
        if not bar.valid:
            raise ValueError("AST_INDICATORS_REQUIRE_CONTIGUOUS_VALID_BARS")
        if value is None:
            result.append({"direction": None, "value": None, "atr": None, "transition": None})
            continue
        midpoint = (bar.high + bar.low) / 2
        basic_upper = midpoint + multiplier_tenths / 10 * value
        basic_lower = midpoint - multiplier_tenths / 10 * value
        previous_direction = direction
        if upper is None or lower is None or previous_close is None:
            upper, lower = basic_upper, basic_lower
            # Equality/doji has an explicit deterministic initial BULL convention.
            direction = "BULL" if bar.close >= midpoint else "BEAR"
        else:
            upper = basic_upper if basic_upper < upper or previous_close > upper else upper
            lower = basic_lower if basic_lower > lower or previous_close < lower else lower
            if direction == "BEAR" and bar.close > upper:
                direction = "BULL"
            elif direction == "BULL" and bar.close < lower:
                direction = "BEAR"
        if direction != previous_direction:
            transition = bar.end.isoformat()
        previous_close = bar.close
        result.append(
            {
                "direction": direction,
                "value": lower if direction == "BULL" else upper,
                "atr": value,
                "transition": transition,
            }
        )
    return result


def adx(bars: list[Bar], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    if len(bars) <= period:
        return result
    plus, minus, ranges = [], [], []
    for left, right in zip(bars, bars[1:]):
        up, down = right.high - left.high, left.low - right.low
        plus.append(up if up > down and up > 0 else 0)
        minus.append(down if down > up and down > 0 else 0)
        ranges.append(max(right.high - right.low, abs(right.high - left.close), abs(right.low - left.close)))
    dx = []
    for p, m, tr in zip(wilder(plus, period), wilder(minus, period), wilder(ranges, period)):
        if p is None or m is None or tr is None:
            continue
        pdi, mdi = (100 * p / tr, 100 * m / tr) if tr else (0, 0)
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0)
    for index, value in enumerate(wilder(dx, period), period):
        result[index] = value
    return result


def efficiency_ratio(bars: list[Bar], lookback: int) -> float | None:
    if lookback < 1:
        raise ValueError("AST_ER_LOOKBACK_INVALID")
    if len(bars) <= lookback:
        return None
    values = [bar.close for bar in bars[-lookback - 1:]]
    path = sum(abs(right - left) for left, right in zip(values, values[1:]))
    return abs(values[-1] - values[0]) / path if path else 0.0
