"""Small, deterministic economic ordering shared by independent spread engines.

All eligibility/risk checks precede ranking. Width is a constraint, not a reward
for narrowness. Within an eligible short, retain premium first, then prefer the
farther hedge at equal protection cost. Scores are ordinal, not probabilities.
"""
from math import isfinite


def premium_retention(short_sell_price, long_buy_price):
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and isfinite(v) and v > 0 for v in (short_sell_price, long_buy_price)):
        raise ValueError("OBSERVED_POSITIVE_PREMIUMS_REQUIRED")
    return (short_sell_price - long_buy_price) / short_sell_price


def short_leg_quality(*, price, premium_reference, distance, distance_scale,
                      oi, min_oi, volume, min_volume, spread_pct, max_spread_pct,
                      structure=None, directional_fit=None, survival=None):
    """Equal-weight, bounded short-only factors, not a probability or a gate.

    Premium is scaled to the best eligible short in this observation. Safety
    distance and liquidity saturate at their existing policy scales. Optional
    evidence stays absent, not fabricated. No hedge price/width enters this score.
    """
    def clip(value):
        return max(0.0, min(1.0, value))

    liquidity = [value / (value + max(minimum, 1))
                 for value, minimum in ((oi, min_oi), (volume, min_volume))
                 if value is not None]
    components = {
        "premium": clip(price / premium_reference),
        "distance": clip(distance / (distance + max(distance_scale, 1))),
        "liquidity": sum(liquidity) / len(liquidity) if liquidity else None,
        "execution": (None if spread_pct is None else
                      float(spread_pct == 0) if max_spread_pct == 0 else
                      clip(1 - spread_pct / max_spread_pct)),
        "structure": None if structure is None else clip(structure),
        "directional_fit": None if directional_fit is None else clip(directional_fit),
        "survival": None if survival is None else clip(survival),
    }
    available = [value for value in components.values() if value is not None]
    return {"short_quality_score": 100 * sum(available) / len(available),
            "short_quality_components": components,
            "short_quality_weighting": "EQUAL_AVAILABLE_FACTORS",
            "premium_reference": premium_reference,
            "distance_scale": distance_scale}


def phase14_short_quality(candidate, premium_reference, config):
    """Reuse existing structure/survival evidence without hedge liquidity leakage."""
    parts = candidate.survival_components
    weights = config.credit_spread_policy.survival_weights
    present = {key: weight for key, weight in weights.items()
               if key != "liquidity" and key in parts}
    survival = (sum(parts[key] * weight for key, weight in present.items()) / sum(present.values())
                if present and sum(present.values()) > 0 else None)
    scale = candidate.required_short_strike_buffer or config.min_short_distance_points
    reference = candidate.support_or_resistance_reference
    structure = (max(0, abs(candidate.short_leg.strike - reference)) / max(scale, 1)
                 if reference is not None else None)
    return short_leg_quality(price=candidate.construction_evidence["short_sell_price"],
        premium_reference=premium_reference, distance=candidate.short_leg_distance_from_spot,
        distance_scale=scale, oi=candidate.short_leg.open_interest,
        min_oi=config.min_short_open_interest, volume=candidate.short_leg.volume,
        min_volume=config.min_short_volume, spread_pct=candidate.liquidity_metrics.short_bid_ask_spread_pct,
        max_spread_pct=config.max_bid_ask_spread_pct, structure=structure,
        directional_fit=candidate.ranking_components.get("directional"), survival=survival)


def construction_key(*, short_quality, retention, width, identity):
    return (-short_quality, -retention, -width, identity)


def ordinal_score(index, count):
    return 100.0 * (count-index) / count


def defined_risk_rejection(width, credit, lot_size, lots=1, *, max_loss=None,
                           max_capital=None, max_width=None):
    if not all(isfinite(v) for v in (width, credit)) or not 0 < credit < width:
        return "INVALID_DEFINED_RISK_PAYOFF"
    if max_width is not None and width > max_width:
        return "HEDGE_REJECTED_MAX_WIDTH"
    if max_loss is not None or max_capital is not None:
        if lot_size is None or lot_size <= 0 or lots <= 0:
            return "LOT_SIZE_UNAVAILABLE"
        loss = (width-credit) * lot_size * lots
        if max_loss is not None and loss > max_loss:
            return "HEDGE_REJECTED_MAX_LOSS"
        if max_capital is not None and loss > max_capital:
            return "HEDGE_REJECTED_MAX_CAPITAL"
    return None


def phase14_quote_failure(snapshot, short, long, config):
    """Use existing quote/depth policies; never upgrade unknown capacity.

    Legacy context may lack source/depth metadata and remains pending hard risk.
    Authoritative replay uses its registered (possibly stricter) quote contract.
    Confirmed but insufficient depth is always rejected, even outside replay.
    """
    from app.research.quotes import (QuotePolicy, executable_quote, information_time,
                                     policy_quote_contract, validate_book)
    policy = config.credit_spread_policy
    quotes = (policy_quote_contract(policy, max_spread_percent=config.max_bid_ask_spread_pct)
              if policy.replay_integrity_enabled else QuotePolicy(
                  source_timestamp_required=False, max_source_age_seconds=config.max_snapshot_age_seconds,
                  max_observation_age_seconds=config.max_snapshot_age_seconds,
                  max_spread_percent=config.max_bid_ask_spread_pct))
    for contract, side in ((short, "bid"), (long, "ask")):
        result = validate_book(contract, snapshot.timestamp_ist, quotes,
                               evaluated_at=information_time(snapshot))
        if not result.valid:
            return result.reason
        if policy.replay_integrity_enabled or contract.depth_unit in {"UNITS", "LOTS"}:
            result = executable_quote(snapshot, contract, side, config.requested_lots,
                                      quotes, evaluated_at=information_time(snapshot))
            if not result.valid:
                return result.reason
    return None
