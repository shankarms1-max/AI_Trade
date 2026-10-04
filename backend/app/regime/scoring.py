from dataclasses import dataclass

from app.regime.models import SignalDirection, SignalGroup


def scored_signal(
    name: str,
    *,
    bull: float = 0,
    bear: float = 0,
    range_value: float = 0,
    bull_reasons: list[str] | None = None,
    bear_reasons: list[str] | None = None,
    range_reasons: list[str] | None = None,
    details: dict | None = None,
) -> SignalGroup:
    bull, bear, range_value = min(1, bull), min(1, bear), min(1, range_value)
    reasons: list[str]
    if bull > bear and bull > range_value:
        direction, score, reasons = SignalDirection.BULLISH, bull, bull_reasons or []
    elif bear > bull and bear > range_value:
        direction, score, reasons = SignalDirection.BEARISH, bear, bear_reasons or []
    elif range_value > 0 and range_value >= max(bull, bear):
        direction, score, reasons = SignalDirection.NEUTRAL, range_value, range_reasons or []
    elif bull == bear and bull > 0:
        direction, score, reasons = SignalDirection.NEUTRAL, 0, ["SIGNALS_CONFLICT"]
    else:
        direction, score, reasons = SignalDirection.NEUTRAL, 0, []
    return SignalGroup(
        name=name,
        direction=direction,
        score=score,
        strength=score,
        reason_codes=reasons,
        details=details or {},
    )


@dataclass(frozen=True)
class RegimeWeights:
    price_structure: float = 3.0
    dynamic_oi: float = 4.0
    positioning: float = 4.0
    futures: float = 2.0
    static_oi: float = 1.5
    pcr: float = 1.0
    oi_dependency_cap: float = 6.0
    alpha_1: float = 4.0
    alpha_2: float = 4.0
    statistical_alpha_cap: float = 5.0
    price_movement_cap: float = 5.0
    basis: float = 1.0


def evidence_mechanism(name: str) -> str:
    if name in {"PRICE_STRUCTURE", "FUTURES", "STATISTICAL_PRICE_ALPHA",
                "STATISTICAL_VOLUME_VOL_ALPHA"}:
        return "PRICE_MOVEMENT"
    if name in {"DYNAMIC_OI", "OPTION_POSITIONING", "STATIC_OI", "PCR"}:
        return "POSITIONING"
    if name == "BASIS":
        return "BASIS"
    if name in {"VOLATILITY_CONTEXT", "VOLATILITY"}:
        return "VOLATILITY_CONTEXT"
    return "DATA_EXECUTION_QUALITY"


def weighted_scores(
    groups: list[SignalGroup], weights: RegimeWeights, *, group_caps_enabled: bool = False
) -> tuple[float, float, float, dict[str, float]]:
    by_name = {group.name: group for group in groups}
    weight_map = {
        "PRICE_STRUCTURE": weights.price_structure,
        "DYNAMIC_OI": weights.dynamic_oi,
        "OPTION_POSITIONING": weights.positioning,
        "FUTURES": weights.futures,
        "STATIC_OI": weights.static_oi,
        "PCR": weights.pcr,
        "STATISTICAL_PRICE_ALPHA": weights.alpha_1,
        "STATISTICAL_VOLUME_VOL_ALPHA": weights.alpha_2,
        "BASIS": weights.basis,
    }
    contributions = {
        name: weight_map.get(name, 0) * group.score for name, group in by_name.items()
    }
    dependent = (("DYNAMIC_OI", "OPTION_POSITIONING", "PCR", "STATIC_OI")
                 if group_caps_enabled else ("DYNAMIC_OI", "OPTION_POSITIONING", "PCR"))
    dependent_total = sum(contributions.get(name, 0) for name in dependent)
    if dependent_total > weights.oi_dependency_cap and dependent_total > 0:
        scale = weights.oi_dependency_cap / dependent_total
        for name in dependent:
            contributions[name] = contributions.get(name, 0) * scale

    alpha_dependent = ("STATISTICAL_PRICE_ALPHA", "STATISTICAL_VOLUME_VOL_ALPHA")
    alpha_total = sum(contributions.get(name, 0) for name in alpha_dependent)
    if alpha_total > weights.statistical_alpha_cap and alpha_total > 0:
        scale = weights.statistical_alpha_cap / alpha_total
        for name in alpha_dependent:
            contributions[name] = contributions.get(name, 0) * scale

    if group_caps_enabled:
        # Correlated underlying-price evidence shares a single budget.
        price_names = ("PRICE_STRUCTURE", "FUTURES", "STATISTICAL_PRICE_ALPHA",
                       "STATISTICAL_VOLUME_VOL_ALPHA")
        total = sum(contributions.get(name, 0) for name in price_names)
        if total > weights.price_movement_cap and total > 0:
            scale = weights.price_movement_cap / total
            for name in price_names:
                contributions[name] = contributions.get(name, 0) * scale

    bull = sum(
        contributions.get(group.name, 0)
        for group in groups
        if group.direction == SignalDirection.BULLISH
    )
    bear = sum(
        contributions.get(group.name, 0)
        for group in groups
        if group.direction == SignalDirection.BEARISH
    )
    range_score = sum(
        contributions.get(group.name, 0)
        for group in groups
        if group.direction == SignalDirection.NEUTRAL and group.score > 0
    )
    return bull, bear, range_score, contributions
