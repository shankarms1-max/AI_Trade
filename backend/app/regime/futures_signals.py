from app.features.models import MarketFeatureSnapshot
from app.regime.models import SignalDirection, SignalGroup
from app.regime.scoring import scored_signal


def futures_signal(
    feature: MarketFeatureSnapshot, prior: MarketFeatureSnapshot | None,
    *, include_basis: bool = True,
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
    if include_basis and prior and prior.futures_features.futures_basis is not None and feature.futures_features.futures_basis is not None:
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


def basis_signal(feature: MarketFeatureSnapshot, prior: MarketFeatureSnapshot | None,
                 *, synchronized: bool = False) -> SignalGroup:
    """Basis remains context until separate spot/future quote times prove synchronization."""
    if not synchronized or prior is None:
        return SignalGroup(name="BASIS", direction=SignalDirection.UNAVAILABLE, score=0,
                           strength=0, reason_codes=["BASIS_TIMESTAMPS_UNVERIFIED"],
                           details={"evidence_group": "BASIS"})
    old, new = prior.futures_features.futures_basis, feature.futures_features.futures_basis
    if old is None or new is None:
        return SignalGroup(name="BASIS", direction=SignalDirection.UNAVAILABLE, score=0,
                           strength=0, reason_codes=["BASIS_CHANGE_UNAVAILABLE"],
                           details={"evidence_group": "BASIS"})
    change = new - old
    return scored_signal("BASIS", bull=.3 if change > 0 else 0,
                         bear=.3 if change < 0 else 0,
                         bull_reasons=["SYNCHRONIZED_BASIS_WIDENING"],
                         bear_reasons=["SYNCHRONIZED_BASIS_NARROWING"],
                         details={"basis_change": change, "evidence_group": "BASIS"})
