from app.features.models import MarketFeatureSnapshot
from app.regime.models import SignalDirection, SignalGroup
from app.regime.scoring import scored_signal


def futures_signal(
    feature: MarketFeatureSnapshot, prior: MarketFeatureSnapshot | None
) -> SignalGroup:
    price = feature.price_structure_features
    if not feature.futures_features.future_available or price.future_change_from_previous_snapshot is None:
        return SignalGroup(
            name="FUTURES", direction=SignalDirection.UNAVAILABLE, score=0,
            strength=0, reason_codes=["FUTURES_COMPARISON_UNAVAILABLE"], details={},
        )
    spot_change = price.spot_change_from_previous_snapshot
    future_change = price.future_change_from_previous_snapshot
    bull = bear = 0.0
    bull_reasons: list[str] = []
    bear_reasons: list[str] = []
    if spot_change is not None and spot_change > 0 and future_change > 0:
        bull = 0.7
        bull_reasons.append("FUTURES_CONFIRM_UPMOVE")
    elif spot_change is not None and spot_change < 0 and future_change < 0:
        bear = 0.7
        bear_reasons.append("FUTURES_CONFIRM_DOWNMOVE")
    basis_change = None
    if prior and prior.futures_features.futures_basis is not None and feature.futures_features.futures_basis is not None:
        basis_change = feature.futures_features.futures_basis - prior.futures_features.futures_basis
        if bull and basis_change >= 0:
            bull += 0.2
            bull_reasons.append("POSITIVE_BASIS_STABLE_OR_EXPANDING")
        if bear and basis_change <= 0:
            bear += 0.2
            bear_reasons.append("BASIS_WEAKENING_WITH_DOWNMOVE")
    return scored_signal(
        "FUTURES", bull=bull, bear=bear,
        bull_reasons=bull_reasons, bear_reasons=bear_reasons,
        details={"basis_change": basis_change, "futures_oi_available": False},
    )
