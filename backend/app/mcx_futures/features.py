"""Causal indicators computed on COMPLETED bars only.

Every series function returns a list aligned with the input where element i
depends only on bars[: i + 1] (appending future bars never changes past
values).  ``compute`` returns, per feature, ``{"value": float | None,
"reason": str | None}``; a value is None during warm-up or when an input is
unavailable (e.g. VWAP without volume is UNAVAILABLE and is never substituted).
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Sequence

Bar = dict[str, Any]
Series = list[float | None]


def ema_series(values: Sequence[float], period: int) -> Series:
    """EMA seeded with the SMA of the first ``period`` values; None before that."""
    out: Series = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    out[period - 1] = value
    for i in range(period, len(values)):
        value = alpha * values[i] + (1 - alpha) * value
        out[i] = value
    return out


def true_ranges(bars: Sequence[Bar]) -> Series:
    out: Series = [None] * len(bars)
    for i in range(1, len(bars)):
        prev = bars[i - 1]["close"]
        bar = bars[i]
        out[i] = max(bar["high"] - bar["low"], abs(bar["high"] - prev), abs(bar["low"] - prev))
    return out


def atr_series(bars: Sequence[Bar], period: int) -> Series:
    """Wilder ATR; first value at index ``period`` (needs period true ranges)."""
    tr = true_ranges(bars)
    out: Series = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    value = sum(float(x) for x in tr[1:period + 1] if x is not None) / period
    out[period] = value
    for i in range(period + 1, len(bars)):
        value = (value * (period - 1) + float(tr[i] or 0.0)) / period
        out[i] = value
    return out


def adx_series(bars: Sequence[Bar], period: int) -> tuple[Series, Series, Series]:
    """Wilder ADX, +DI, -DI.  DI from index ``period``; ADX from index ``2 * period``."""
    n = len(bars)
    adx: Series = [None] * n
    plus_di: Series = [None] * n
    minus_di: Series = [None] * n
    if n < period + 1:
        return adx, plus_di, minus_di
    tr = true_ranges(bars)
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        up = bars[i]["high"] - bars[i - 1]["high"]
        down = bars[i - 1]["low"] - bars[i]["low"]
        pdm[i] = up if up > down and up > 0 else 0.0
        mdm[i] = down if down > up and down > 0 else 0.0
    s_tr = sum(float(x or 0.0) for x in tr[1:period + 1])
    s_p = sum(pdm[1:period + 1])
    s_m = sum(mdm[1:period + 1])
    dx: list[float] = []
    for i in range(period, n):
        if i > period:
            s_tr = s_tr - s_tr / period + float(tr[i] or 0.0)
            s_p = s_p - s_p / period + pdm[i]
            s_m = s_m - s_m / period + mdm[i]
        p = 100 * s_p / s_tr if s_tr > 0 else 0.0
        m = 100 * s_m / s_tr if s_tr > 0 else 0.0
        plus_di[i], minus_di[i] = p, m
        dx.append(100 * abs(p - m) / (p + m) if p + m > 0 else 0.0)
        if len(dx) == period:
            adx[i] = sum(dx) / period
        elif len(dx) > period:
            prev = adx[i - 1]
            adx[i] = ((prev or 0.0) * (period - 1) + dx[-1]) / period
    return adx, plus_di, minus_di


def efficiency_ratio(closes: Sequence[float], lookback: int) -> float | None:
    if len(closes) < lookback + 1:
        return None
    window = closes[-(lookback + 1):]
    path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    return abs(window[-1] - window[0]) / path if path > 0 else 0.0


def session_vwap(bars: Sequence[Bar]) -> tuple[float | None, str | None]:
    """VWAP of the latest bar's session using typical price; never substitutes."""
    if not bars:
        return None, "NO_BARS"
    session = bars[-1]["session_id"]
    numerator = 0.0
    volume = 0.0
    for bar in reversed(bars):
        if bar["session_id"] != session:
            break
        if bar["volume"] is None:
            return None, "VOLUME_UNAVAILABLE"
        numerator += (bar["high"] + bar["low"] + bar["close"]) / 3 * bar["volume"]
        volume += bar["volume"]
    if volume <= 0:
        return None, "VOLUME_UNAVAILABLE"
    return numerator / volume, None


def percentile_rank(history: Sequence[float], value: float) -> float:
    below = sum(1 for item in history if item < value)
    equal = sum(1 for item in history if item == value)
    return (below + 0.5 * equal) / len(history)


def warmup_bars(cfg: Any) -> int:
    return max(
        cfg.ema_slow + cfg.slope_bars,
        2 * cfg.adx_period + 1,
        cfg.breakout_lookback + 1,
        cfg.er_lookback + 1,
        cfg.atr_period + cfg.atr_percentile_min_samples,
        cfg.momentum_bars + cfg.atr_period + 1,
    )


def _f(value: float | None, reason: str | None = None) -> dict[str, Any]:
    if value is not None and not isfinite(value):
        return {"value": None, "reason": "NON_FINITE"}
    return {"value": None if value is None else round(float(value), 10), "reason": reason if value is None else None}


def compute(bars: Sequence[Bar], cfg: Any, quote: dict[str, Any] | None = None,
            at: datetime | None = None, tick: float | None = None) -> dict[str, Any]:
    """All features for the LAST completed bar in ``bars`` plus current quote features."""
    need = warmup_bars(cfg)
    n = len(bars)
    out: dict[str, Any] = {"bars_available": n, "warmup_required": need, "warm": n >= need}
    names = ("ema_fast", "ema_slow", "ema_slope_atr", "atr", "adx", "plus_di", "minus_di", "breakout_up_atr",
             "breakout_down_atr", "efficiency_ratio", "atr_percentile", "return_k_atr", "pullback_return_atr",
             "close_vs_vwap_atr", "vwap")
    if n < need:
        for name in names:
            out[name] = _f(None, f"WARMUP {n}/{need}")
        out["vwap"] = _f(None, f"WARMUP {n}/{need}")
    else:
        closes = [float(bar["close"]) for bar in bars]
        fast = ema_series(closes, cfg.ema_fast)
        slow = ema_series(closes, cfg.ema_slow)
        atr = atr_series(bars, cfg.atr_period)
        adx, pdi, mdi = adx_series(bars, cfg.adx_period)
        a = atr[-1]
        if a is None or a <= 0:
            for name in names:
                out[name] = _f(None, "ATR_UNAVAILABLE")
        else:
            close = closes[-1]
            out["atr"] = _f(a)
            out["ema_fast"] = _f(fast[-1])
            out["ema_slow"] = _f(slow[-1])
            base = slow[-1 - cfg.slope_bars]
            out["ema_slope_atr"] = _f(None if base is None or slow[-1] is None else (slow[-1] - base) / a)
            out["adx"], out["plus_di"], out["minus_di"] = _f(adx[-1]), _f(pdi[-1]), _f(mdi[-1])
            prior = bars[-(cfg.breakout_lookback + 1):-1]
            out["breakout_up_atr"] = _f((close - max(bar["high"] for bar in prior)) / a)
            out["breakout_down_atr"] = _f((min(bar["low"] for bar in prior) - close) / a)
            out["efficiency_ratio"] = _f(efficiency_ratio(closes, cfg.er_lookback))
            history = [x for x in atr[-cfg.atr_percentile_window:] if x is not None]
            out["atr_percentile"] = (_f(percentile_rank(history, a)) if len(history) >= cfg.atr_percentile_min_samples
                                     else _f(None, "ATR_HISTORY_SHORT"))
            out["return_k_atr"] = _f((close - closes[-1 - cfg.momentum_bars]) / a)
            out["pullback_return_atr"] = _f((close - closes[-1 - cfg.pullback_bars]) / a)
            vwap, reason = session_vwap(bars)
            out["vwap"] = _f(vwap, reason)
            out["close_vs_vwap_atr"] = _f(None if vwap is None else (close - vwap) / a, reason)
    out.update(quote_features(quote, at, tick))
    return out


def quote_features(quote: dict[str, Any] | None, at: datetime | None, tick: float | None) -> dict[str, Any]:
    if quote is None or at is None or tick is None:
        return {"spread_ticks": _f(None, "QUOTE_UNAVAILABLE"), "quote_age_seconds": _f(None, "QUOTE_UNAVAILABLE")}
    bid, ask = quote.get("bid"), quote.get("ask")
    spread = _f(None, "BID_ASK_UNAVAILABLE") if bid is None or ask is None else _f((ask - bid) / tick)
    source = quote.get("source_ts")
    age = (_f(None, "SOURCE_TIMESTAMP_UNAVAILABLE") if not source
           else _f((at - datetime.fromisoformat(source)).total_seconds()))
    return {"spread_ticks": spread, "quote_age_seconds": age}
