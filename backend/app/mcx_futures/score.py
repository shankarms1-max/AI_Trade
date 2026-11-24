"""100-point directional evidence score for outright MCX futures (LONG / SHORT).

Components and maxima come from ``InstrumentConfig.weights`` (default trend 25,
momentum 20, structure 20, strength 15, regime 10, execution 10).  An
unavailable input scores 0 for its sub-component and the reason is recorded;
the total is never rescaled.  Hard vetoes are reported separately from the
score and never change it.
"""

from __future__ import annotations

from typing import Any

LONG, SHORT = "LONG", "SHORT"


def opposite(direction: str) -> str:
    return SHORT if direction == LONG else LONG


def sign_of(direction: str) -> int:
    return 1 if direction == LONG else -1


def _v(features: dict[str, Any], name: str) -> float | None:
    item = features.get(name)
    return None if not isinstance(item, dict) else item.get("value")


def _why(features: dict[str, Any], name: str) -> str:
    item = features.get(name)
    return f"{name}:{(item or {}).get('reason') or 'UNAVAILABLE'}"


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def direction_and_state(features: dict[str, Any], cfg: Any) -> tuple[str | None, str]:
    fast, slow, slope = _v(features, "ema_fast"), _v(features, "ema_slow"), _v(features, "ema_slope_atr")
    if fast is None or slow is None or slope is None:
        return None, "UNAVAILABLE"
    if fast > slow and slope > 0:
        direction = LONG
    elif fast < slow and slope < 0:
        direction = SHORT
    else:
        return None, "NEUTRAL"
    sign = sign_of(direction)
    pullback = _v(features, "pullback_return_atr")
    if pullback is not None and sign * pullback <= -cfg.pullback_atr:
        return direction, f"{direction}_PULLBACK"
    er = _v(features, "efficiency_ratio")
    if er is not None and er < cfg.chop_er_threshold:
        return direction, f"{direction}_CHOP"
    return direction, f"{direction}_TREND"


def components(features: dict[str, Any], direction: str, cfg: Any) -> dict[str, dict[str, Any]]:
    sign = sign_of(direction)
    w = cfg.weights
    out: dict[str, dict[str, Any]] = {}

    def put(name: str, parts: list[tuple[float, float | None, str | None]]) -> None:
        points = 0.0
        reasons = []
        for share, factor, reason in parts:
            if factor is None:
                reasons.append(reason or "UNAVAILABLE")
            else:
                points += w[name] * share * _clamp(factor)
        out[name] = {"points": round(points, 4), "max": w[name], "unavailable": reasons}

    fast, slow, slope = _v(features, "ema_fast"), _v(features, "ema_slow"), _v(features, "ema_slope_atr")
    stack = None if fast is None or slow is None else (1.0 if sign * (fast - slow) > 0 else 0.0)
    vwap_dist = _v(features, "close_vs_vwap_atr")
    put("trend", [
        (0.4, stack, _why(features, "ema_fast")),
        (0.6, None if slope is None else sign * slope / cfg.slope_full_atr, _why(features, "ema_slope_atr")),
    ])
    momentum = _v(features, "return_k_atr")
    put("momentum", [(1.0, None if momentum is None else sign * momentum / cfg.momentum_full_atr,
                      _why(features, "return_k_atr"))])
    breakout = _v(features, "breakout_up_atr" if direction == LONG else "breakout_down_atr")
    put("structure", [
        (0.5, None if vwap_dist is None else sign * vwap_dist / cfg.vwap_full_atr, _why(features, "vwap")),
        (0.5, None if breakout is None else (breakout + cfg.breakout_full_atr) / (2 * cfg.breakout_full_atr),
         _why(features, "breakout_up_atr")),
    ])
    adx, pdi, mdi = _v(features, "adx"), _v(features, "plus_di"), _v(features, "minus_di")
    strength = None
    if adx is not None and pdi is not None and mdi is not None:
        aligned = (pdi > mdi) if direction == LONG else (mdi > pdi)
        strength = (adx - cfg.adx_floor) / (cfg.adx_full - cfg.adx_floor) if aligned else 0.0
    put("strength", [(1.0, strength, _why(features, "adx"))])
    er, pct = _v(features, "efficiency_ratio"), _v(features, "atr_percentile")
    er_factor = None
    if er is not None:
        er_factor = 0.0 if er < cfg.chop_er_threshold else (
            (er - cfg.chop_er_threshold) / (cfg.trend_er_full - cfg.chop_er_threshold))
    band = None if pct is None else (1.0 if cfg.vol_percentile_low <= pct <= cfg.vol_percentile_high else 0.0)
    put("regime", [(0.6, er_factor, _why(features, "efficiency_ratio")),
                   (0.4, band, _why(features, "atr_percentile"))])
    spread, age = _v(features, "spread_ticks"), _v(features, "quote_age_seconds")
    put("execution", [
        (0.6, None if spread is None else
         (cfg.max_spread_ticks - spread) / (cfg.max_spread_ticks - cfg.full_credit_spread_ticks),
         _why(features, "spread_ticks")),
        (0.4, None if age is None else 1 - max(age, 0.0) / cfg.stale_quote_seconds,
         _why(features, "quote_age_seconds")),
    ])
    return out


def hard_vetoes(features: dict[str, Any], facts: dict[str, Any], cfg: Any) -> list[str]:
    """Entry vetoes independent of the score.  ``facts`` supplies session/event/expiry/bar context."""
    vetoes: list[str] = []
    if not features.get("warm"):
        vetoes.append("WARMUP_INCOMPLETE")
    elif _v(features, "atr") is None:
        vetoes.append("ATR_UNAVAILABLE")
    spread, age = _v(features, "spread_ticks"), _v(features, "quote_age_seconds")
    if spread is None:
        vetoes.append("QUOTE_UNAVAILABLE")
    elif spread > cfg.max_spread_ticks:
        vetoes.append("SPREAD_TOO_WIDE")
    if age is None:
        vetoes.append("QUOTE_AGE_UNAVAILABLE")
    elif age > cfg.stale_quote_seconds:
        vetoes.append("STALE_FEED")
    if facts.get("blackout"):
        vetoes.append("EVENT_BLACKOUT")
    if not facts.get("in_entry_window", False):
        vetoes.append("OUTSIDE_ENTRY_WINDOW")
    days = facts.get("days_to_expiry")
    if days is None or days < cfg.expiry_window_days:
        vetoes.append("EXPIRY_WINDOW")
    if facts.get("bar_missing_minutes", 0) > 0:
        vetoes.append("DATA_GAP_IN_BAR")
    return vetoes


def evaluate(features: dict[str, Any], facts: dict[str, Any], cfg: Any) -> dict[str, Any]:
    direction, market_state = direction_and_state(features, cfg)
    if direction is None:
        parts = {name: {"points": 0.0, "max": cfg.weights[name], "unavailable": ["NO_DIRECTION"]}
                 for name in cfg.weights}
    else:
        parts = components(features, direction, cfg)
    total = round(sum(item["points"] for item in parts.values()), 4)
    evidence = {name: (item.get("value") if isinstance(item, dict) else item)
                for name, item in features.items() if name not in ("warm", "bars_available", "warmup_required")}
    return {
        "direction": direction,
        "market_state": market_state,
        "score": total,
        "entry_score_floor": cfg.entry_score_floor,
        "components": parts,
        "evidence": evidence,
        "warm": bool(features.get("warm")),
        "vetoes": hard_vetoes(features, facts, cfg),
    }
