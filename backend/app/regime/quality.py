from app.features.models import MarketFeatureSnapshot
from app.regime.models import EvidenceQuality


def evidence_quality(feature: MarketFeatureSnapshot, minimum_contracts: int) -> EvidenceQuality:
    quality = feature.data_quality
    sufficient_contracts = quality.contracts_total >= minimum_contracts
    has_history = feature.price_structure_features.spot_change_from_previous_snapshot is not None
    if not sufficient_contracts:
        return EvidenceQuality.INSUFFICIENT
    if (
        quality.future_available
        and quality.vix_available
        and quality.intraday_oi_usable
        and quality.intraday_volume_usable
        and has_history
    ):
        return EvidenceQuality.HIGH
    available = sum(
        (
            quality.future_available,
            quality.vix_available,
            quality.intraday_oi_usable,
            quality.intraday_volume_usable,
            has_history,
        )
    )
    if has_history and available >= 3:
        return EvidenceQuality.MEDIUM
    if quality.contracts_with_open_interest > 0:
        return EvidenceQuality.LOW
    return EvidenceQuality.INSUFFICIENT
