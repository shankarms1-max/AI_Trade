from dataclasses import dataclass

from app.features.models import MarketFeatureSnapshot
from app.regime.confidence import calculate_confidence
from app.regime.futures_signals import futures_signal
from app.regime.models import EvidenceQuality, Regime, RegimeResult, SignalDirection
from app.regime.oi_signals import dynamic_oi_signal, positioning_signal, static_oi_signal
from app.regime.pcr_signals import pcr_signal
from app.regime.price_signals import price_structure_signal
from app.regime.quality import evidence_quality
from app.regime.scoring import RegimeWeights, weighted_scores
from app.regime.volatility_context import volatility_context


@dataclass(frozen=True)
class RegimeConfig:
    minimum_confidence: float = 60
    minimum_directional_margin: float = 2
    minimum_contracts: int = 10
    small_move_pct: float = 0.05
    pcr_low: float = 0.8
    pcr_high: float = 1.2
    low_quality_confidence_cap: float = 55
    insufficient_confidence_cap: float = 30
    weights: RegimeWeights = RegimeWeights()


def classify_regime(
    feature_snapshot_id: int,
    feature: MarketFeatureSnapshot,
    prior: MarketFeatureSnapshot | None,
    config: RegimeConfig = RegimeConfig(),
) -> RegimeResult:
    quality = evidence_quality(feature, config.minimum_contracts)
    volatility, risk_flags = volatility_context(feature)
    groups = [
        price_structure_signal(feature, config.small_move_pct),
        dynamic_oi_signal(feature),
        static_oi_signal(feature),
        futures_signal(feature, prior),
        pcr_signal(feature, config.pcr_low, config.pcr_high),
        positioning_signal(feature),
        volatility,
    ]
    bull, bear, range_score, contributions = weighted_scores(groups, config.weights)
    scores = {Regime.BULLISH: bull, Regime.BEARISH: bear, Regime.RANGE: range_score}
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0].value))
    winner, winning_score = ordered[0]
    runner_up_score = ordered[1][1]
    winning_direction = (
        SignalDirection.NEUTRAL if winner == Regime.RANGE
        else SignalDirection(winner.value)
    )
    confirming = sum(
        group.direction == winning_direction and group.score > 0 for group in groups
    )
    contradictory = sum(
        group.direction in {SignalDirection.BULLISH, SignalDirection.BEARISH}
        and group.direction.value != winner.value
        and group.score >= 0.3
        for group in groups
    )
    maximum_score = (
        config.weights.price_structure + config.weights.futures
        + config.weights.static_oi + config.weights.oi_dependency_cap
    )
    confidence = calculate_confidence(
        winning_score,
        runner_up_score,
        maximum_score=maximum_score,
        confirming_groups=confirming,
        contradictory_groups=contradictory,
        quality=quality,
        low_cap=config.low_quality_confidence_cap,
        insufficient_cap=config.insufficient_confidence_cap,
    )
    warnings: list[str] = []
    missing: list[str] = []
    if not feature.data_quality.intraday_oi_usable:
        missing.append("INTRADAY_OI")
        warnings.append("INSUFFICIENT_DYNAMIC_OI")
    if feature.price_structure_features.spot_change_from_previous_snapshot is None:
        missing.append("PRIOR_COMPARABLE_SNAPSHOT")
        warnings.append("INSUFFICIENT_HISTORY")
    if not feature.data_quality.future_available:
        missing.append("FUTURE")
    if not feature.data_quality.vix_available:
        missing.append("INDIA_VIX")
    margin = winning_score - runner_up_score
    range_confirmations = sum(
        group.direction == SignalDirection.NEUTRAL and group.score > 0 for group in groups
    )
    final = winner
    if quality == EvidenceQuality.INSUFFICIENT:
        final = Regime.NO_TRADE
        warnings.append("INSUFFICIENT_EVIDENCE")
    elif confidence < config.minimum_confidence:
        final = Regime.NO_TRADE
        warnings.append("CONFIDENCE_BELOW_THRESHOLD")
    elif margin < config.minimum_directional_margin:
        final = Regime.NO_TRADE
        warnings.append("REGIME_MARGIN_TOO_SMALL")
    elif winner in {Regime.BULLISH, Regime.BEARISH} and not feature.data_quality.intraday_oi_usable:
        final = Regime.NO_TRADE
        warnings.append("DIRECTION_REQUIRES_DYNAMIC_OI")
    elif winner in {Regime.BULLISH, Regime.BEARISH} and prior is None:
        final = Regime.NO_TRADE
        warnings.append("DIRECTION_REQUIRES_HISTORY")
    elif winner == Regime.RANGE and range_confirmations < 2:
        final = Regime.NO_TRADE
        warnings.append("INSUFFICIENT_POSITIVE_RANGE_EVIDENCE")
    if contradictory >= 2:
        warnings.append("SIGNALS_CONFLICT")

    def reasons(direction: SignalDirection) -> list[str]:
        return sorted({
            reason for group in groups if group.direction == direction and group.score > 0
            for reason in group.reason_codes
        })

    return RegimeResult(
        snapshot_id=feature.snapshot_id,
        feature_snapshot_id=feature_snapshot_id,
        timestamp=feature.timestamp,
        regime=final,
        bull_score=round(bull, 4),
        bear_score=round(bear, 4),
        range_score=round(range_score, 4),
        confidence=confidence,
        evidence_quality=quality,
        signal_groups=groups,
        bull_evidence=reasons(SignalDirection.BULLISH),
        bear_evidence=reasons(SignalDirection.BEARISH),
        range_evidence=reasons(SignalDirection.NEUTRAL),
        warnings=sorted(set(warnings)),
        missing_inputs=missing,
        risk_flags=risk_flags,
    )


def config_from_settings(settings) -> RegimeConfig:
    return RegimeConfig(
        minimum_confidence=settings.regime_min_confidence,
        minimum_directional_margin=settings.regime_min_directional_margin,
        minimum_contracts=settings.regime_min_contracts,
        small_move_pct=settings.regime_small_move_pct,
        pcr_low=settings.regime_pcr_low,
        pcr_high=settings.regime_pcr_high,
        low_quality_confidence_cap=settings.regime_low_quality_confidence_cap,
        insufficient_confidence_cap=settings.regime_insufficient_confidence_cap,
        weights=RegimeWeights(
            price_structure=settings.regime_price_weight,
            dynamic_oi=settings.regime_dynamic_oi_weight,
            positioning=settings.regime_positioning_weight,
            futures=settings.regime_futures_weight,
            static_oi=settings.regime_static_oi_weight,
            pcr=settings.regime_pcr_weight,
            oi_dependency_cap=settings.regime_oi_dependency_cap,
        ),
    )
