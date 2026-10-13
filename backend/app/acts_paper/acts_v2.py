"""Versioned, causal ACTS V2 paper qualification; V1 and execution stay intact."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from math import isfinite
from typing import Any

from app.scalper.models import ScalperCandidate
from app.scalper_lab import acts

STRATEGY_ID = "ACTS_V2"
WEIGHT_KEYS = frozenset({
    "momentum_15m", "momentum_5m", "momentum_3m", "momentum_resumption",
    "structure_range", "structure_opening", "structure_breakout",
    "alpha_1", "alpha_2", "positioning", "participation_volume",
    "participation_wall", "execution_spread", "execution_depth", "execution_credit",
})
ALPHA_FACTOR_KEYS = frozenset({
    "strong_support", "support", "neutral", "unavailable", "opposite",
    "strong_opposite",
})


@dataclass(frozen=True)
class ActsV2Config:
    weights: dict[str, float]
    alpha_factors: dict[str, float]
    watch_score: float
    tradeable_score: float
    strong_score: float
    very_strong_score: float
    trend_15m_bps: float
    trend_5m_bps: float
    pullback_3m_bps: float
    resumption_3m_bps: float
    setup_lifetime_seconds: int
    opposite_positioning_penalty: float
    veto_strong_opposite_alpha: bool
    max_bid_ask_spread_pct: float
    min_credit_to_width: float

    @classmethod
    def from_settings(cls, settings: Any) -> ActsV2Config:
        weights = json.loads(settings.acts_v2_score_weights_json)
        factors = json.loads(settings.acts_v2_alpha_factors_json)
        if (not isinstance(weights, dict) or set(weights) != WEIGHT_KEYS or any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not isfinite(value) or value < 0 for value in weights.values())
                or abs(sum(weights.values()) - 100) > 1e-9):
            raise ValueError("ACTS_V2_SCORE_WEIGHTS_INVALID")
        if (not isinstance(factors, dict) or set(factors) != ALPHA_FACTOR_KEYS or any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not isfinite(value) or not 0 <= value <= 1
                for value in factors.values()) or not (
                    factors["strong_support"] >= factors["support"] >= factors["neutral"]
                    >= factors["unavailable"] >= factors["opposite"]
                    >= factors["strong_opposite"])):
            raise ValueError("ACTS_V2_ALPHA_FACTORS_INVALID")
        scores = (settings.acts_v2_watch_score, settings.acts_v2_tradeable_score,
                  settings.acts_v2_strong_score, settings.acts_v2_very_strong_score)
        if not 0 < scores[0] < scores[1] < scores[2] < scores[3] <= 100:
            raise ValueError("ACTS_V2_SCORE_BANDS_INVALID")
        return cls({key: float(value) for key, value in weights.items()},
                   {key: float(value) for key, value in factors.items()},
                   *scores, settings.acts_v2_trend_15m_bps,
                   settings.acts_v2_trend_5m_bps,
                   settings.acts_v2_pullback_3m_bps,
                   settings.acts_v2_resumption_3m_bps,
                   settings.acts_v2_setup_lifetime_seconds,
                   settings.acts_v2_opposite_positioning_penalty,
                   settings.acts_v2_veto_strong_opposite_alpha,
                   settings.scalper_max_bid_ask_spread_pct,
                   settings.scalper_min_credit_to_width)

    def payload(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _signed(value: float | None, sign: int, full: float) -> float:
    return 0.0 if value is None or not isfinite(value) else _clamp(sign * value / full)


def market_state(ctx: dict[str, Any], watch: dict[str, Any],
                 config: ActsV2Config) -> tuple[str, str | None, str | None]:
    """Keep the 15-minute thesis through a temporary 3-minute pullback."""
    returns = ctx["futures_returns_bps"]
    three, five, fifteen = (returns.get(key) for key in ("3m", "5m", "15m"))
    position = ctx["structure"]["range_position"]
    if fifteen is None or position is None:
        return "NEUTRAL", None, None
    direction = None
    if abs(fifteen) >= config.trend_15m_bps:
        direction = "BULL" if fifteen > 0 else "BEAR"
    elif five is not None and abs(five) >= config.trend_5m_bps:
        if five > 0 and position >= .55:
            direction = "BULL"
        elif five < 0 and position <= .45:
            direction = "BEAR"
    if direction is None:
        return ("TRANSITION" if five is not None and five * fifteen < 0
                else "NEUTRAL"), None, None
    sign = 1 if direction == "BULL" else -1
    if (five is not None and sign * five <= -config.trend_5m_bps
            and (position <= .35 if sign > 0 else position >= .65)):
        return "TRANSITION", None, None
    now = datetime.fromisoformat(ctx["timestamp"])
    previous = watch.get("started_at") if watch.get("direction") == direction else None
    recent = False
    if previous:
        seconds = (now - datetime.fromisoformat(previous)).total_seconds()
        recent = 0 <= seconds <= config.setup_lifetime_seconds
    if three is not None and sign * three <= -config.pullback_3m_bps:
        return f"{direction}_PULLBACK", direction, "PULLBACK"
    if recent and three is not None and sign * three >= config.resumption_3m_bps:
        return f"{direction}_RESUMPTION", direction, "PULLBACK_RESUMPTION"
    strong = (sign * fifteen >= 2 * config.trend_15m_bps
              and five is not None and sign * five >= 2 * config.trend_5m_bps
              and (position >= .75 if sign > 0 else position <= .25))
    return (f"STRONG_{direction}" if strong else direction), direction, None


def _alpha_factor(value: str | None, direction: str, config: ActsV2Config) -> float:
    if value is None:
        return config.alpha_factors["unavailable"]
    own = "BULLISH" if direction == "BULL" else "BEARISH"
    opposite = "BEARISH" if direction == "BULL" else "BULLISH"
    key = ("strong_support" if value == f"STRONG_{own}" else
           "support" if value == own else
           "strong_opposite" if value == f"STRONG_{opposite}" else
           "opposite" if value == opposite else "neutral")
    return config.alpha_factors[key]


def _score(ctx: dict[str, Any], direction: str, market: str,
           candidate: ScalperCandidate | None, config: ActsV2Config
           ) -> tuple[float, dict[str, float], dict[str, Any]]:
    sign = 1 if direction == "BULL" else -1
    w = config.weights
    returns = ctx["futures_returns_bps"]
    three, five, fifteen = (returns.get(key) for key in ("3m", "5m", "15m"))
    position = ctx["structure"]["range_position"]
    midpoint = ((ctx["opening_high"] + ctx["opening_low"]) / 2
                if ctx["opening_high"] is not None and ctx["opening_low"] is not None
                else None)
    opening_aligned = midpoint is not None and sign * (ctx["spot"] - midpoint) > 0
    boundary = ctx["opening_high"] if sign > 0 else ctx["opening_low"]
    breakout = boundary is not None and sign * (ctx["spot"] - boundary) > 0
    momentum = (w["momentum_15m"] * _signed(fifteen, sign, 2 * config.trend_15m_bps)
                + w["momentum_5m"] * _signed(five, sign, 2 * config.trend_5m_bps)
                + w["momentum_3m"] * _signed(three, sign, 2 * config.resumption_3m_bps)
                + w["momentum_resumption"] * (
                    1 if market.endswith("_RESUMPTION") else
                    .5 if three is not None and five is not None
                    and sign * three >= sign * five > 0 else 0))
    range_factor = (_clamp((position - .5) * 2) if sign > 0 else
                    _clamp((.5 - position) * 2))
    structure = (w["structure_range"] * range_factor
                 + w["structure_opening"] * (1 if opening_aligned else 0)
                 + w["structure_breakout"] * (1 if breakout else 0))
    alpha = ctx["acts"]["alpha"]
    alpha_score = (w["alpha_1"] * _alpha_factor(alpha["alpha_1_direction"], direction, config)
                   + w["alpha_2"] * _alpha_factor(alpha["alpha_2_direction"], direction, config)
                   if alpha["status"] == "AVAILABLE" else
                   (w["alpha_1"] + w["alpha_2"]) * config.alpha_factors["unavailable"])
    positioning = acts.positioning(ctx, direction)
    passed = len(positioning["passed"])
    opposing = sum(positioning["opposite_checks"].values())
    positioning_score = max(0.0, min(
        w["positioning"], w["positioning"] * passed / 4
        - config.opposite_positioning_penalty * opposing))
    wall = positioning["relevant_wall"]
    pcr = ctx["options"]["local_pcr"]
    volume_ratios = [value for radius in ("3", "5")
                     if (value := pcr[radius]["volume"]) is not None
                     and isfinite(value) and value >= 0]
    volume_ratio = sum(volume_ratios) / len(volume_ratios) if volume_ratios else None
    volume_factor = (_clamp(.5 + sign * (volume_ratio - 1) / (volume_ratio + 1))
                     if volume_ratio is not None else 0)
    participation = (w["participation_volume"] * volume_factor
                     + w["participation_wall"] * (
                         1 if wall and any(wall["activity"][h] == "WRITING"
                                           for h in ("1m", "3m", "5m")) else
                         .5 if wall and wall["volume"] is not None else 0))
    execution = 0.0
    if candidate is not None:
        short = candidate.quote_evidence["short"]
        long = candidate.quote_evidence["long"]
        spreads = (short["validation"]["spread_pct"],
                   long["validation"]["spread_pct"])
        max_spread = config.max_bid_ask_spread_pct
        execution += w["execution_spread"] * _clamp(
            1 - sum(spreads) / (2 * max_spread))
        depth = min(short.get("bid_quantity") or 0, long.get("ask_quantity") or 0)
        execution += w["execution_depth"] * _clamp(depth / (2 * ctx["lot_size"]))
        min_ratio = config.min_credit_to_width
        execution += w["execution_credit"] * _clamp(
            candidate.credit_to_width / (2 * min_ratio))
    components = {"momentum": round(momentum, 4), "structure": round(structure, 4),
                  "alpha": round(alpha_score, 4), "positioning": round(positioning_score, 4),
                  "participation": round(participation, 4), "execution": round(execution, 4)}
    evidence = {"positioning_signals_passed": passed, "positioning_total": 4,
                "positioning_score_contribution": round(
                    positioning_score - w["positioning"] / 2, 4),
                "individual_positioning_evidence": positioning,
                "local_pcr_volume": {radius: pcr[radius]["volume"] for radius in ("3", "5")},
                "volume_factor": round(volume_factor, 4),
                "wall_activity": wall["activity"] if wall else None,
                "opening_midpoint": midpoint, "opening_aligned": opening_aligned,
                "opening_breakout": breakout}
    return round(sum(components.values()), 4), components, evidence


def decide(ctx: dict[str, Any], watch: dict[str, Any],
           candidate: ScalperCandidate | None, candidate_rejections: dict[str, int],
           risk_blockers: list[str], config: ActsV2Config) -> dict[str, Any]:
    if watch.get("strategy_version") != STRATEGY_ID:
        watch.clear()
    market, direction, trigger = market_state(ctx, watch, config)
    now = datetime.fromisoformat(ctx["timestamp"])
    if direction and market.endswith("_PULLBACK"):
        if watch.get("direction") != direction or not watch.get("started_at"):
            watch.clear()
            watch.update(strategy_version=STRATEGY_ID, direction=direction,
                         started_at=ctx["timestamp"], stage="PULLBACK")
    elif watch.get("started_at"):
        age = (now - datetime.fromisoformat(watch["started_at"])).total_seconds()
        if age < 0 or age > config.setup_lifetime_seconds or direction != watch.get("direction"):
            watch.clear()
    hard = list(risk_blockers)
    if ctx["future"] is None or not ctx.get("future_instrument_id") or not ctx.get("future_expiry"):
        hard.append("FUTURES_REFERENCE_UNAVAILABLE")
    if direction and candidate is None:
        hard.append("NO_EXECUTABLE_CANDIDATE")
    if config.veto_strong_opposite_alpha and direction and acts.relative_alpha_state(
            ctx["acts"]["alpha"], direction) == "VETO":
        hard.append("CONFIGURED_STRONG_OPPOSITE_ALPHA_VETO")
    score, components, evidence = ((0.0, {name: 0.0 for name in (
        "momentum", "structure", "alpha", "positioning", "participation", "execution")}, {})
        if direction is None else _score(ctx, direction, market, candidate, config))
    band = ("VERY_STRONG" if score >= config.very_strong_score else
            "STRONG" if score >= config.strong_score else
            "TRADEABLE" if score >= config.tradeable_score else
            "WATCH" if score >= config.watch_score else "NO_TRADE")
    status = ("NO_SETUP" if hard or direction is None else
              "WATCH" if market.endswith("_PULLBACK") else
              "ENTER" if score >= config.tradeable_score else
              "WATCH" if score >= config.watch_score else "NO_SETUP")
    if status == "ENTER":
        trigger = trigger or ("FRESH_OPENING_BREAKOUT" if evidence["opening_breakout"]
                              else "DIRECTIONAL_CONTINUATION")
        watch.clear()
    thesis = None
    if status == "ENTER":
        thesis = {"strategy_id": STRATEGY_ID, "strategy_version": STRATEGY_ID,
                  "direction": direction, "market_state": market,
                  "opportunity_score": score, "score_band": band,
                  "score_components": components, "timing_trigger": trigger,
                  "entry_reason": trigger, "alpha": ctx["acts"]["alpha"],
                  "positioning": evidence["individual_positioning_evidence"],
                  "wall": evidence["individual_positioning_evidence"]["relevant_wall"],
                  "opening_high": ctx["opening_high"],
                  "opening_low": ctx["opening_low"],
                  "invalidation_conditions": acts.INVALIDATIONS,
                  "parameters": config.payload()}
    return {"strategy_id": STRATEGY_ID, "timestamp": ctx["timestamp"],
            "status": status, "direction": direction, "trigger": trigger,
            "regime": market, "market_state": market,
            "opportunity_score": score, "score_band": band,
            "score_components": components, "score_evidence": evidence,
            "alpha": ctx["acts"]["alpha"],
            "positioning": evidence.get("individual_positioning_evidence"),
            "positioning_signals_passed": evidence.get("positioning_signals_passed", 0),
            "positioning_total": evidence.get("positioning_total", 4),
            "positioning_score_contribution": evidence.get("positioning_score_contribution", 0),
            "positioning_checks": (evidence.get("individual_positioning_evidence") or
                                   {}).get("checks", {}),
            "candidate_rejections": candidate_rejections,
            "hard_blockers": list(dict.fromkeys(hard)),
            "blockers": list(dict.fromkeys(hard)),
            "watch_state": dict(watch), "thesis": thesis,
            "entry_reason": trigger if status == "ENTER" else None}


def entry_valid(ctx: dict[str, Any], direction: str,
                config: ActsV2Config) -> str | None:
    state, current_direction, _ = market_state(ctx, {}, config)
    if current_direction != direction or state == "TRANSITION":
        return "PENDING_ACTS_V2_DIRECTION_REVERSED"
    return None
