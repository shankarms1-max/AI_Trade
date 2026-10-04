import math
from collections.abc import Iterable


def average(values: Iterable[float]) -> float | None:
    items = [float(item) for item in values]
    return None if not items else round(sum(items) / len(items), 2)


def percentile_95(values: Iterable[float]) -> float | None:
    items = sorted(float(item) for item in values)
    if not items:
        return None
    return round(items[max(0, math.ceil(0.95 * len(items)) - 1)], 2)


def percentage(numerator: int, denominator: int) -> float | None:
    return None if denominator <= 0 else round(100 * numerator / denominator, 2)


def consecutive_failures(statuses: Iterable[str], success: str = "SUCCESS") -> int:
    count = 0
    for status in statuses:
        if status == success:
            break
        if status in {"FAILED", "PARTIAL"}:
            count += 1
    return count
