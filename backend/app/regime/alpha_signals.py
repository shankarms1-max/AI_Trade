from app.alpha.models import AlphaDirection, AlphaEvidenceQuality, AlphaFeatureSnapshot, AlphaValidity
from app.regime.models import SignalDirection, SignalGroup


def _signal(name: str, rank: float | None, direction: AlphaDirection,
            quality: AlphaEvidenceQuality) -> SignalGroup:
    if rank is None or direction == AlphaDirection.INSUFFICIENT_DATA:
        return SignalGroup(name=name, direction=SignalDirection.UNAVAILABLE, score=0,
                           strength=0, reason_codes=["STATISTICAL_ALPHA_UNAVAILABLE"])
    quality_scale = {
        AlphaEvidenceQuality.HIGH: 1.0,
        AlphaEvidenceQuality.MEDIUM: 0.8,
        AlphaEvidenceQuality.LOW: 0.4,
        AlphaEvidenceQuality.INSUFFICIENT: 0.0,
    }[quality]
    if direction in {AlphaDirection.STRONG_BULLISH, AlphaDirection.BULLISH}:
        regime_direction = SignalDirection.BULLISH
        strength = abs(rank - 0.5) * 2
        reason = f"{name}_BULLISH"
    elif direction in {AlphaDirection.STRONG_BEARISH, AlphaDirection.BEARISH}:
        regime_direction = SignalDirection.BEARISH
        strength = abs(rank - 0.5) * 2
        reason = f"{name}_BEARISH"
    else:
        regime_direction = SignalDirection.NEUTRAL
        strength = max(0, 1 - abs(rank - 0.5) * 5)
        reason = f"{name}_NEUTRAL"
    score = min(1.0, strength * quality_scale)
    return SignalGroup(
        name=name, direction=regime_direction, score=score, strength=score,
        reason_codes=[reason],
        details={"rank": rank, "alpha_quality": quality.value},
    )


def statistical_alpha_signals(alpha: AlphaFeatureSnapshot) -> list[SignalGroup]:
    if alpha.validity_state != AlphaValidity.VALID:
        return [SignalGroup(name=name, direction=SignalDirection.UNAVAILABLE, score=0,
                            strength=0, reason_codes=["ALPHA_HARD_VALIDITY_GATE"])
                for name in ("STATISTICAL_PRICE_ALPHA", "STATISTICAL_VOLUME_VOL_ALPHA")]
    return [
        _signal("STATISTICAL_PRICE_ALPHA", alpha.alpha_1, alpha.alpha_1_direction,
                alpha.evidence_quality),
        _signal("STATISTICAL_VOLUME_VOL_ALPHA", alpha.alpha_2, alpha.alpha_2_direction,
                alpha.evidence_quality),
    ]
