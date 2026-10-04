from datetime import datetime, timedelta


def percentile_rank(value: float, history: list[float], minimum: int) -> float | None:
    """Historical midrank: (strictly lower + 0.5 * equal) / prior count."""
    finite = [item for item in history if item == item and abs(item) != float("inf")]
    if len(finite) < minimum:
        return None
    lower = sum(item < value for item in finite)
    equal = sum(item == value for item in finite)
    return (lower + 0.5 * equal) / len(finite)


def duration_window(items, timestamp: datetime, minutes: int, timestamp_getter=lambda x: x.timestamp):
    start = timestamp - timedelta(minutes=minutes)
    return [item for item in items if start <= timestamp_getter(item) < timestamp]
