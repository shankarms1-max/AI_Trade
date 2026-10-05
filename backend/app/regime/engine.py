from dataclasses import dataclass
from app.strategy.policy import CreditSpreadPolicy, policy_from_settings
from app.regime.market_state import market_state

from app.features.models import MarketFeatureSnapshot
from app.regime.confidence import calculate_confidence
from app.regime.futures_signals import basis_signal, futures_signal
from app.regime.models import EvidenceQuality, Regime, RegimeResult, SignalDirection, SignalGroup
from app.regime.oi_signals import dynamic_oi_signal, positioning_signal, static_oi_signal
from app.regime.pcr_signals import pcr_signal
from app.regime.price_signals import price_structure_signal
from app.regime.quality import evidence_quality
from app.regime.scoring import RegimeWeights, evidence_mechanism, weighted_scores
from app.regime.volatility_context import volatility_context
from app.alpha.models import AlphaFeatureSnapshot, JointAlphaDirection
from app.regime.alpha_signals import statistical_alpha_signals


@dataclass(frozen=True)
class RegimeConfig:
    credit_spread_policy: CreditSpreadPolicy = CreditSpreadPolicy()
    minimum_confidence: float = 60
    minimum_directional_margin: float = 2
    minimum_contracts: int = 10
    small_move_pct: float = 0.05
    pcr_low: float = 0.8
    pcr_high: float = 1.2
    low_quality_confidence_cap: float = 55
    insufficient_confidence_cap: float = 30
    weights: RegimeWeights = RegimeWeights()
    use_statistical_alpha: bool = False
    require_alpha_for_directional: bool = True
    alpha_contradiction_confidence_penalty: float = 25


def classify_regime(
    feature_snapshot_id: int,
    feature: MarketFeatureSnapshot,
    prior: MarketFeatureSnapshot | None,
    config: RegimeConfig = RegimeConfig(),
    alpha: AlphaFeatureSnapshot | None = None,
) -> RegimeResult:
    quality = evidence_quality(feature, config.minimum_contracts)
    volatility, risk_flags = volatility_context(feature)
    groups = [
        price_structure_signal(feature, config.small_move_pct),
        dynamic_oi_signal(feature),
        static_oi_signal(feature),
        futures_signal(feature, prior, include_basis=not (config.use_statistical_alpha or config.credit_spread_policy.enabled)),
        pcr_signal(feature, config.pcr_low, config.pcr_high),
        positioning_signal(feature),
        volatility,
    ]
    if config.credit_spread_policy.enabled or (config.use_statistical_alpha and alpha is not None):
        # Broker-reported OI change has an unknown baseline. Keep these groups
        # as context until same-contract local OI and price changes are audited.
        groups = [SignalGroup(name=item.name, direction=SignalDirection.UNAVAILABLE,
                              score=0, strength=0,
                              reason_codes=["BROKER_OI_BASELINE_NOT_LOCAL"],
                              details={"evidence_group": "POSITIONING"})
                  if item.name in {"DYNAMIC_OI", "OPTION_POSITIONING"} else item
                  for item in groups]
        if config.use_statistical_alpha and alpha is not None:
            groups.extend(statistical_alpha_signals(alpha))
    if config.use_statistical_alpha or config.credit_spread_policy.enabled:
        groups.append(basis_signal(feature, prior, synchronized=False))
        groups = [item.model_copy(update={"details": {**item.details,
                                                      "evidence_group": evidence_mechanism(item.name)}})
                  for item in groups]
    bull, bear, range_score, contributions = weighted_scores(
        groups, config.weights, group_caps_enabled=config.use_statistical_alpha or config.credit_spread_policy.enabled)
    scores = {Regime.BULLISH: bull, Regime.BEARISH: bear, Regime.RANGE: range_score}
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0].value))
    winner, winning_score = ordered[0]
    runner_up_score = ordered[1][1]
    winning_direction = (
        SignalDirection.NEUTRAL if winner == Regime.RANGE
        else SignalDirection(winner.value)
    )
    confirming_groups = [group for group in groups
                         if group.direction == winning_direction and group.score > 0]
    contradicting_groups = [group for group in groups
                            if group.direction in {SignalDirection.BULLISH, SignalDirection.BEARISH}
                            and group.direction.value != winner.value and group.score >= 0.3]
    confirming = (len({evidence_mechanism(item.name) for item in confirming_groups})
                  if config.use_statistical_alpha else len(confirming_groups))
    contradictory = (len({evidence_mechanism(item.name) for item in contradicting_groups})
                     if config.use_statistical_alpha else len(contradicting_groups))
    maximum_score = ((config.weights.price_movement_cap + config.weights.oi_dependency_cap
                      + config.weights.basis) if config.use_statistical_alpha else
                     (config.weights.price_structure + config.weights.futures
                      + config.weights.static_oi + config.weights.oi_dependency_cap))
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
    if config.use_statistical_alpha and alpha is None:
        missing.append("STATISTICAL_ALPHA")
        warnings.append("ALPHA_HISTORY_INSUFFICIENT")
    margin = winning_score - runner_up_score
    range_confirmations = sum(
        group.direction == SignalDirection.NEUTRAL and group.score > 0 for group in groups
    )
    final = winner
    alpha_direction = None
    if alpha is not None:
        if "BULLISH" in alpha.joint_alpha_direction.value:
            alpha_direction = Regime.BULLISH
        elif "BEARISH" in alpha.joint_alpha_direction.value:
            alpha_direction = Regime.BEARISH
    non_alpha_directional = [
        group for group in groups
        if not group.name.startswith("STATISTICAL_")
        and group.direction in {SignalDirection.BULLISH, SignalDirection.BEARISH}
        and group.score > 0
    ]
    independent_confirmation = any(
        group.direction.value == winner.value and group.name not in {"PRICE_STRUCTURE", "FUTURES"}
        for group in non_alpha_directional
    ) if config.use_statistical_alpha else any(
        group.direction.value == winner.value for group in non_alpha_directional
    )
    strong_contradiction = bool(
        alpha is not None
        and alpha.joint_alpha_direction in {
            JointAlphaDirection.STRONG_BULLISH_CONFIRMATION,
            JointAlphaDirection.STRONG_BEARISH_CONFIRMATION,
        }
        and any(group.direction.value != alpha_direction.value and group.score >= 0.6
                for group in non_alpha_directional)
    )
    if strong_contradiction:
        confidence = max(0, confidence - config.alpha_contradiction_confidence_penalty)
        warnings.append("STRONG_SIGNAL_CONTRADICTION")
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
    elif config.use_statistical_alpha and winner in {Regime.BULLISH, Regime.BEARISH} and (
        alpha is None or not alpha.confirmed or alpha_direction != winner
    ) and config.require_alpha_for_directional and not config.credit_spread_policy.enabled:
        final = Regime.NO_TRADE
        warnings.append("DIRECTION_REQUIRES_CONFIRMED_ALPHA")
    elif config.use_statistical_alpha and winner in {Regime.BULLISH, Regime.BEARISH} and not independent_confirmation:
        final = Regime.NO_TRADE
        warnings.append("DIRECTION_REQUIRES_INDEPENDENT_CONFIRMATION")
    elif strong_contradiction:
        final = Regime.NO_TRADE
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

    result = RegimeResult(
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

    if config.credit_spread_policy.enabled:
        state = market_state(result, feature, prior, config.credit_spread_policy, alpha, config.use_statistical_alpha)
        # Retain compatibility labels, without hiding weak directional or neutral bias.
        bias = state["market_bias"].value
        state["warnings"] = [warning for warning in result.warnings if warning not in {
            "CONFIDENCE_BELOW_THRESHOLD", "REGIME_MARGIN_TOO_SMALL", "DIRECTION_REQUIRES_CONFIRMED_ALPHA"}]
        state["regime"] = Regime.RANGE if bias == "NEUTRAL" else Regime(bias) if bias in {"BULLISH", "BEARISH"} else Regime.NO_TRADE
        result = result.model_copy(update=state)
    return result


def config_from_settings(settings) -> RegimeConfig:
    return RegimeConfig(
        credit_spread_policy=policy_from_settings(settings),
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
            alpha_1=settings.regime_alpha1_weight,
            alpha_2=settings.regime_alpha2_weight,
            statistical_alpha_cap=settings.regime_statistical_alpha_cap,
            price_movement_cap=settings.regime_price_movement_cap,
            basis=settings.regime_basis_weight,
        ),
        use_statistical_alpha=settings.regime_use_statistical_alpha,
        require_alpha_for_directional=settings.regime_require_alpha_for_directional,
        alpha_contradiction_confidence_penalty=settings.regime_alpha_contradiction_confidence_penalty,
    )
