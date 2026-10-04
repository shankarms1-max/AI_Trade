from app.features.models import MarketFeatureSnapshot
from app.regime.models import SignalDirection, SignalGroup


def volatility_context(feature: MarketFeatureSnapshot) -> tuple[SignalGroup, list[str]]:
    regime = feature.volatility_features.vix_regime
    if regime is None:
        return (
            SignalGroup(
                name="VOLATILITY_CONTEXT", direction=SignalDirection.UNAVAILABLE,
                score=0, strength=0, reason_codes=["VIX_UNAVAILABLE"], details={},
            ),
            ["VIX_UNAVAILABLE"],
        )
    flag = f"{regime}_VOLATILITY"
    return (
        SignalGroup(
            name="VOLATILITY_CONTEXT", direction=SignalDirection.NEUTRAL,
            score=0, strength=0, reason_codes=[flag],
            details={"directional_weight": 0, "vix_regime": regime},
        ),
        [flag],
    )
