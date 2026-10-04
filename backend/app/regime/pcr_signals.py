from app.features.models import MarketFeatureSnapshot
from app.regime.models import SignalDirection, SignalGroup
from app.regime.scoring import scored_signal


def pcr_signal(feature: MarketFeatureSnapshot, low: float, high: float) -> SignalGroup:
    value = feature.pcr_features.local_pcr_oi
    if value is None:
        return SignalGroup(
            name="PCR", direction=SignalDirection.UNAVAILABLE, score=0,
            strength=0, reason_codes=["LOCAL_PCR_UNAVAILABLE"], details={"scope": "LOCAL_WINDOW_PCR"},
        )
    bull = 0.35 if value >= high else 0
    bear = 0.35 if value <= low else 0
    range_value = 0.2 if low < value < high else 0
    details = {
        "scope": "LOCAL_WINDOW_PCR",
        "local_pcr_oi": value,
        "local_pcr_oi_change": feature.pcr_features.local_pcr_oi_change,
        "oi_change_usable": feature.data_quality.intraday_oi_usable,
    }
    reasons = []
    if not feature.data_quality.intraday_oi_usable:
        reasons.append("PCR_OI_CHANGE_UNAVAILABLE")
    signal = scored_signal(
        "PCR", bull=bull, bear=bear, range_value=range_value,
        bull_reasons=["PCR_SUPPORTIVE"], bear_reasons=["PCR_BEARISH"],
        range_reasons=["PCR_BALANCED"], details=details,
    )
    signal.reason_codes.extend(reasons)
    return signal
