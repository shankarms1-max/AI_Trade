"""Read-only scalar projection of the engine's stored futures ORB evidence."""
from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from app.parallel_paper.bars import session_start

ORB_MINUTES = {"ORB_15": 15, "ORB_30": 30, "ORB_60": 60}
NUMBERS = ("range_minutes", "opening_high", "opening_low", "breakout_distance_bps", "confirmation_seconds")
STRINGS = ("range_start", "range_end", "breakout_direction", "breakout_timestamp", "reason")


def orb_features(strategy: str, features: Any, timestamp: str | None) -> dict | None:
    """Never calculate a market range or replace missing stored price evidence."""
    if strategy not in ORB_MINUTES or not isinstance(features, dict) or not features:
        return None
    result: dict[str, Any] = {
        key: float(value) if isinstance(value := features.get(key), (int, float))
        and not isinstance(value, bool) and isfinite(value) else None for key in NUMBERS}
    result.update({key: value if isinstance(value := features.get(key), str) else None for key in STRINGS})
    result["complete"] = features.get("complete") if isinstance(features.get("complete"), bool) else None
    # Older evidence may omit session boundaries. Only derive the clock window;
    # prices and completeness always come from the original stored calculation.
    result["range_minutes"] = result["range_minutes"] or ORB_MINUTES[strategy]
    if timestamp and (result["range_start"] is None or result["range_end"] is None):
        try:
            at = datetime.fromisoformat(timestamp)
            if at.tzinfo is not None:
                start = session_start(at)
                result["range_start"] = result["range_start"] or start.isoformat()
                result["range_end"] = result["range_end"] or (
                    start + timedelta(minutes=ORB_MINUTES[strategy])).isoformat()
        except ValueError:
            pass
    return result
