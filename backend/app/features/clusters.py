from collections.abc import Callable

from app.features.models import ContractFeature, OICluster


def infer_strike_step(rows: list[ContractFeature]) -> float | None:
    strikes = sorted({row.strike for row in rows})
    differences = [right - left for left, right in zip(strikes, strikes[1:]) if right > left]
    return min(differences) if differences else None


def build_clusters(
    rows: list[ContractFeature],
    *,
    option_type: str,
    spot: float,
    strike_step: float | None,
    maximum_fraction: float,
    metric: Callable[[ContractFeature], int],
    label: str,
) -> list[OICluster]:
    side = [(row, max(0, metric(row))) for row in rows if row.option_type == option_type]
    maximum = max((value for _, value in side), default=0)
    total = sum(value for _, value in side)
    if not maximum or not total:
        return []
    candidates = sorted(
        ((row, value) for row, value in side if value >= maximum * maximum_fraction),
        key=lambda pair: pair[0].strike,
    )
    groups: list[list[tuple[ContractFeature, int]]] = []
    for pair in candidates:
        if not groups or strike_step is None or pair[0].strike - groups[-1][-1][0].strike > strike_step:
            groups.append([pair])
        else:
            groups[-1].append(pair)
    result: list[OICluster] = []
    for group in groups:
        cluster_total = sum(value for _, value in group)
        max_row, max_value = max(group, key=lambda pair: (pair[1], -pair[0].strike))
        center = sum(row.strike * value for row, value in group) / cluster_total
        proximity = 1 / (1 + abs(center - spot) / (strike_step or 1))
        strength = (0.5 * cluster_total / total) + (0.3 * max_value / maximum) + (0.2 * proximity)
        result.append(
            OICluster(
                side=label,
                low_strike=group[0][0].strike,
                high_strike=group[-1][0].strike,
                center_strike=center,
                total_oi=cluster_total,
                share_of_side_oi=cluster_total / total,
                max_oi_strike=max_row.strike,
                max_oi=max_value,
                distance_from_spot=center - spot,
                strength_score=strength,
            )
        )
    return sorted(result, key=lambda item: (-item.strength_score, item.low_strike))
