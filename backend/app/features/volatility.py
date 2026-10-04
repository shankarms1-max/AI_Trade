from app.features.models import VolatilityFeatures


def calculate_volatility_features(
    vix: float | None, *, low: float, elevated: float, high: float
) -> VolatilityFeatures:
    if vix is None:
        return VolatilityFeatures(india_vix=None, vix_available=False, vix_regime=None)
    if vix < low:
        regime = "LOW"
    elif vix < elevated:
        regime = "NORMAL"
    elif vix < high:
        regime = "ELEVATED"
    else:
        regime = "HIGH"
    return VolatilityFeatures(india_vix=vix, vix_available=True, vix_regime=regime)
