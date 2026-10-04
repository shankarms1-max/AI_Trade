from datetime import datetime
from typing import Any

from app.observability.health import age_seconds
from app.observability.models import HealthStatus, MarketSessionState


def shadow_health(
    now: datetime, session: MarketSessionState, metrics: dict[str, Any], stale_seconds: int
) -> dict[str, Any]:
    mark_age = age_seconds(now, metrics["latest_shadow_mark_at"])
    if metrics["payout_anomalies_today"] or metrics["invalid_shadow_trades_today"]:
        status = HealthStatus.UNHEALTHY
    elif not metrics["open_shadow_trades"]:
        status = HealthStatus.IDLE
    elif session == MarketSessionState.OPEN and (mark_age is None or mark_age > stale_seconds):
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.HEALTHY
    return {"status": status.value, "latest_mark_age_seconds": mark_age, **metrics}
