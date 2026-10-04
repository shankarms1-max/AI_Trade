from app.features.models import MarketFeatureSnapshot


def structural_reference(feature: MarketFeatureSnapshot, regime: str) -> float | None:
    if regime == "BULLISH":
        clusters = [
            item for item in feature.support_resistance.potential_support_clusters
            if item.high_strike < feature.spot
        ]
        return max(clusters, key=lambda item: item.high_strike).low_strike if clusters else None
    if regime == "BEARISH":
        clusters = [
            item for item in feature.support_resistance.potential_resistance_clusters
            if item.low_strike > feature.spot
        ]
        return min(clusters, key=lambda item: item.low_strike).high_strike if clusters else None
    return None
