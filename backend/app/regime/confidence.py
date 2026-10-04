from app.regime.models import EvidenceQuality


def calculate_confidence(
    winning_score: float,
    runner_up_score: float,
    *,
    maximum_score: float,
    confirming_groups: int,
    contradictory_groups: int,
    quality: EvidenceQuality,
    low_cap: float,
    insufficient_cap: float,
) -> float:
    strength = min(1, winning_score / maximum_score) if maximum_score else 0
    margin = max(0, winning_score - runner_up_score) / (winning_score or 1)
    confirmation = min(1, confirming_groups / 3)
    raw = (45 * strength) + (35 * margin) + (20 * confirmation)
    raw -= min(30, contradictory_groups * 10)
    multipliers = {
        EvidenceQuality.HIGH: 1.0,
        EvidenceQuality.MEDIUM: 0.9,
        EvidenceQuality.LOW: 0.65,
        EvidenceQuality.INSUFFICIENT: 0.4,
    }
    value = max(0, raw * multipliers[quality])
    if quality == EvidenceQuality.LOW:
        value = min(value, low_cap)
    if quality == EvidenceQuality.INSUFFICIENT:
        value = min(value, insufficient_cap)
    return round(value, 2)
