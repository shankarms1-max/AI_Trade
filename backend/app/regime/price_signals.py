from app.features.models import MarketFeatureSnapshot
from app.regime.scoring import scored_signal


def price_structure_signal(feature: MarketFeatureSnapshot, small_move_pct: float):
    price = feature.price_structure_features
    bull = bear = range_value = 0.0
    bull_reasons: list[str] = []
    bear_reasons: list[str] = []
    range_reasons: list[str] = []
    change_pct = price.spot_change_pct_from_previous_snapshot
    if change_pct is not None:
        if change_pct > small_move_pct:
            bull += 0.45
            bull_reasons.append("SPOT_RISING_FROM_PREVIOUS")
        elif change_pct < -small_move_pct:
            bear += 0.45
            bear_reasons.append("SPOT_FALLING_FROM_PREVIOUS")
        else:
            range_value += 0.4
            range_reasons.append("SPOT_MOVE_MUTED")
    if feature.spot > price.session_open_proxy:
        bull += 0.2
        bull_reasons.append("PRICE_ABOVE_SESSION_OPEN_PROXY")
    elif feature.spot < price.session_open_proxy:
        bear += 0.2
        bear_reasons.append("PRICE_BELOW_SESSION_OPEN_PROXY")
    high, low = price.observed_opening_range_high, price.observed_opening_range_low
    if high is not None and feature.spot > high:
        bull += 0.45
        bull_reasons.append("PRICE_ABOVE_OPENING_RANGE")
    elif low is not None and feature.spot < low:
        bear += 0.45
        bear_reasons.append("PRICE_BELOW_OPENING_RANGE")
    elif high is not None and low is not None and low <= feature.spot <= high:
        range_value += 0.45
        range_reasons.append("PRICE_INSIDE_OBSERVED_OPENING_RANGE")
        if price.observed_opening_range_complete:
            range_value += 0.15
            range_reasons.append("OBSERVED_OPENING_RANGE_COMPLETE")
    supports = feature.support_resistance.potential_support_clusters
    resistances = feature.support_resistance.potential_resistance_clusters
    below = [item for item in supports if item.center_strike < feature.spot]
    above = [item for item in resistances if item.center_strike > feature.spot]
    if below and above:
        range_value += 0.35
        range_reasons.append("PRICE_BETWEEN_OI_CLUSTERS")
    return scored_signal(
        "PRICE_STRUCTURE",
        bull=bull,
        bear=bear,
        range_value=range_value,
        bull_reasons=bull_reasons,
        bear_reasons=bear_reasons,
        range_reasons=range_reasons,
        details={"spot_change_pct": change_pct},
    )
