"""Bias describes evidence; family eligibility is a separate research policy."""

from app.regime.models import MarketBias, DirectionalStrength, StrategyFamilyEligibility
from app.strategy.policy import LOGIC_VERSION, STRENGTH_RANK


def market_state(result, feature, prior, policy, alpha=None, use_alpha=False):
    quality_ok = result.evidence_quality.value in {"MEDIUM", "HIGH"}
    continuity = bool(
        prior
        and prior.timestamp < feature.timestamp
        and prior.timestamp.date() == feature.timestamp.date()
        and prior.expiry == feature.expiry
        and (feature.timestamp - prior.timestamp).total_seconds() <= 600
    )
    usable = quality_ok and continuity and feature.data_quality.intraday_oi_usable
    alpha_state, alpha_bias, alpha_strength = "DISABLED", "NEUTRAL", "NONE"
    if use_alpha:
        alpha_state = "UNAVAILABLE" if alpha is None else alpha.validity_state.value
        if alpha is not None and alpha.validity_state.value == "VALID":
            alpha_state = alpha.joint_alpha_direction.value
            alpha_strength = (
                "STRONG"
                if alpha_state.startswith("STRONG_")
                else ("MODERATE" if "CONFIRMATION" in alpha_state else "NONE")
            )
            alpha_bias = (
                "BULLISH"
                if "BULLISH" in alpha_state
                else "BEARISH" if "BEARISH" in alpha_state else "NEUTRAL"
            )
        # Weak alpha is allowed; broken alpha continuity/quality is not.
        usable = (
            usable
            and alpha is not None
            and alpha.validity_state.value == "VALID"
            and alpha.timestamp == feature.timestamp
            and alpha.snapshot_id == feature.snapshot_id
            and alpha.expiry == feature.expiry
        )
    bull, bear = result.bull_score, result.bear_score
    score = (
        100
        * abs(bull - bear)
        / max(bull + bear + result.range_score, 1)
        * min(1, max(bull, bear) / policy.directional_evidence_scale)
    )
    strength = (
        DirectionalStrength.STRONG
        if score >= policy.strong_directional_score
        else (
            DirectionalStrength.MODERATE
            if score >= policy.moderate_directional_score
            else (
                DirectionalStrength.WEAK
                if score >= policy.weak_directional_score
                else DirectionalStrength.NONE
            )
        )
    )
    bias = (
        MarketBias.BULLISH
        if bull > bear
        else MarketBias.BEARISH if bear > bull else MarketBias.NEUTRAL
    )
    if strength == DirectionalStrength.NONE:
        bias = MarketBias.NEUTRAL
    conflict = (
        min(bull, bear) >= policy.conflict_min_score
        and score <= policy.conflict_max_directional_score
        or "STRONG_SIGNAL_CONTRADICTION" in result.warnings
        or alpha_state == "CONFLICT"
        or any(
            "RANK_SIGN_CONFLICT" in warning
            for warning in (alpha.warnings if use_alpha and alpha else [])
        )
    )
    if not usable:
        bias, strength = MarketBias.INSUFFICIENT, DirectionalStrength.NONE
    elif conflict:
        bias, strength = MarketBias.CONFLICT, DirectionalStrength.NONE
    eligible = bias not in {MarketBias.CONFLICT, MarketBias.INSUFFICIENT}
    directional = (
        eligible
        and bias in {MarketBias.BULLISH, MarketBias.BEARISH}
        and STRENGTH_RANK[strength.value]
        >= max(2, STRENGTH_RANK[policy.directional_alpha_min_strength])
        and policy.family_mode != "THETA_CARRY_ONLY"
    )
    carry = (
        eligible
        and policy.theta_carry_enabled
        and policy.family_mode != "DIRECTIONAL_ONLY"
    )
    family = (
        StrategyFamilyEligibility.BOTH
        if directional and carry
        else (
            StrategyFamilyEligibility.DIRECTIONAL_ONLY
            if directional
            else (
                StrategyFamilyEligibility.THETA_CARRY_ONLY
                if carry
                else StrategyFamilyEligibility.NONE
            )
        )
    )

    def group_state(names):
        groups = [g for g in result.signal_groups if g.name in names]
        directions = {g.direction.value for g in groups if g.score > 0}
        return (
            next(iter(directions))
            if len(directions) == 1
            else "CONFLICT" if len(directions) > 1 else "UNAVAILABLE"
        )

    return dict(
        strategy_logic_version=LOGIC_VERSION,
        regime_version=LOGIC_VERSION,
        market_bias=bias,
        directional_strength=strength,
        directional_score=round(score, 4),
        statistical_alpha_state=alpha_state,
        statistical_alpha_bias=alpha_bias,
        statistical_alpha_strength=alpha_strength,
        positioning_state=group_state(
            {"STATIC_OI", "DYNAMIC_OI", "OPTION_POSITIONING", "PCR"}
        ),
        price_structure_state=group_state({"PRICE_STRUCTURE"}),
        basis_state=group_state({"BASIS"}),
        volatility_state=feature.volatility_features.vix_regime or "UNAVAILABLE",
        data_quality_state="VALID" if usable else "INSUFFICIENT",
        strategy_family_eligibility=family,
    )
