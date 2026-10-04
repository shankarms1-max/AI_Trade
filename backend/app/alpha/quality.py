from app.alpha.models import AlphaEvidenceQuality


def assess_quality(
    *, alpha1_available: bool, alpha2_available: bool, atm_available: bool,
    volume_available: bool, volatility_available: bool, sequence_continuous: bool,
    rank_count: int, minimum_rank: int,
) -> AlphaEvidenceQuality:
    if not alpha1_available and not alpha2_available:
        return AlphaEvidenceQuality.INSUFFICIENT
    checks = sum((atm_available, volume_available, volatility_available, sequence_continuous))
    if alpha1_available and alpha2_available and checks == 4 and rank_count >= minimum_rank * 2:
        return AlphaEvidenceQuality.HIGH
    if alpha1_available and alpha2_available and checks >= 3:
        return AlphaEvidenceQuality.MEDIUM
    return AlphaEvidenceQuality.LOW
