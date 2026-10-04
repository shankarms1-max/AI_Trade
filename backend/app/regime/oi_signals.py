from app.features.models import MarketFeatureSnapshot
from app.regime.models import SignalDirection, SignalGroup
from app.regime.scoring import scored_signal


def dynamic_oi_signal(feature: MarketFeatureSnapshot) -> SignalGroup:
    if not feature.data_quality.intraday_oi_usable:
        return SignalGroup(
            name="DYNAMIC_OI", direction=SignalDirection.UNAVAILABLE, score=0,
            strength=0, reason_codes=["INSUFFICIENT_DYNAMIC_OI"], details={},
        )
    bull = bear = 0.0
    bull_reasons: list[str] = []
    bear_reasons: list[str] = []
    structure = feature.support_resistance
    for cluster in structure.put_oi_addition_clusters or []:
        if cluster.center_strike <= feature.spot:
            bull += 0.45 * cluster.strength_score
            bull_reasons.append("PUT_OI_ADDITION_BELOW_SPOT")
    for cluster in structure.call_oi_addition_clusters or []:
        if cluster.center_strike >= feature.spot:
            bear += 0.45 * cluster.strength_score
            bear_reasons.append("CALL_OI_ADDITION_ABOVE_SPOT")
    for row in feature.oi_features.top_call_oi_reductions or []:
        if row.strike >= feature.spot:
            bull += 0.25
            bull_reasons.append("CALL_OI_REDUCTION_ABOVE_SPOT")
    for row in feature.oi_features.top_put_oi_reductions or []:
        if row.strike <= feature.spot:
            bear += 0.25
            bear_reasons.append("PUT_OI_REDUCTION_BELOW_SPOT")
    return scored_signal(
        "DYNAMIC_OI", bull=bull, bear=bear,
        bull_reasons=bull_reasons, bear_reasons=bear_reasons,
        details={"broker_oi_change_only": True},
    )


_POSITION_MAP = {
    ("CE", "LONG_BUILDUP"): ("BEARISH", 0.8, "CALL_LONG_BUILDUP_NEAR_SPOT"),
    ("CE", "SHORT_BUILDUP"): ("BEARISH", 1.0, "CALL_SHORT_BUILDUP_ABOVE_SPOT"),
    ("CE", "SHORT_COVERING"): ("BULLISH", 1.0, "CALL_SHORT_COVERING_ABOVE_SPOT"),
    ("CE", "LONG_UNWINDING"): ("BEARISH", 0.5, "CALL_LONG_UNWINDING_NEAR_SPOT"),
    ("PE", "LONG_BUILDUP"): ("BEARISH", 0.8, "PUT_LONG_BUILDUP_NEAR_SPOT"),
    ("PE", "SHORT_BUILDUP"): ("BULLISH", 1.0, "PUT_SHORT_BUILDUP_BELOW_SPOT"),
    ("PE", "SHORT_COVERING"): ("BEARISH", 1.0, "PUT_SHORT_COVERING_BELOW_SPOT"),
    ("PE", "LONG_UNWINDING"): ("BULLISH", 0.5, "PUT_LONG_UNWINDING_NEAR_SPOT"),
}


def positioning_signal(feature: MarketFeatureSnapshot) -> SignalGroup:
    step = feature.support_resistance.inferred_strike_step or 50
    bull = bear = 0.0
    bull_reasons: list[str] = []
    bear_reasons: list[str] = []
    considered = 0
    for row in feature.oi_features.contracts:
        mapping = _POSITION_MAP.get((row.option_type, row.positioning_class))
        distance_steps = abs(row.strike - feature.spot) / step
        if mapping is None or distance_steps > 4:
            continue
        location_weight = 1.0 if distance_steps <= 2 else 0.5
        direction, base, reason = mapping
        value = base * location_weight / 4
        considered += 1
        if direction == "BULLISH":
            bull += value
            bull_reasons.append(reason)
        else:
            bear += value
            bear_reasons.append(reason)
    if considered == 0:
        return SignalGroup(
            name="OPTION_POSITIONING", direction=SignalDirection.UNAVAILABLE,
            score=0, strength=0, reason_codes=["INSUFFICIENT_POSITIONING_DATA"],
            details={"deep_strikes_excluded_beyond_steps": 4},
        )
    return scored_signal(
        "OPTION_POSITIONING", bull=bull, bear=bear,
        bull_reasons=bull_reasons, bear_reasons=bear_reasons,
        details={"contracts_considered": considered, "deep_strikes_excluded_beyond_steps": 4},
    )


def static_oi_signal(feature: MarketFeatureSnapshot) -> SignalGroup:
    supports = [
        item for item in feature.support_resistance.potential_support_clusters
        if item.center_strike < feature.spot
    ]
    resistances = [
        item for item in feature.support_resistance.potential_resistance_clusters
        if item.center_strike > feature.spot
    ]
    bull = min(0.6, max((item.strength_score for item in supports), default=0) * 0.6)
    bear = min(0.6, max((item.strength_score for item in resistances), default=0) * 0.6)
    range_value = min(0.7, max(bull, bear) + 0.05) if supports and resistances else 0
    return scored_signal(
        "STATIC_OI", bull=bull, bear=bear, range_value=range_value,
        bull_reasons=["STATIC_PUT_CLUSTER_BELOW_SPOT"] if bull else [],
        bear_reasons=["STATIC_CALL_CLUSTER_ABOVE_SPOT"] if bear else [],
        range_reasons=["STATIC_OI_CONTAINMENT"] if range_value else [],
        details={"contribution_capped": True},
    )
