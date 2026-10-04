from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.observability.models import HealthStatus, MarketSessionState

IST = ZoneInfo("Asia/Kolkata")


def market_session_state(
    now: datetime, start: time, end: time, holidays: frozenset[date] = frozenset()
) -> MarketSessionState:
    local = now.astimezone(IST)
    if local.weekday() >= 5:
        return MarketSessionState.WEEKEND
    if local.date() in holidays:
        return MarketSessionState.HOLIDAY
    current = local.time().replace(tzinfo=None)
    if current < start:
        return MarketSessionState.PRE_MARKET
    if current <= end:
        return MarketSessionState.OPEN
    return MarketSessionState.POST_MARKET


def session_bounds(day: date, start: time, end: time) -> tuple[datetime, datetime]:
    return datetime.combine(day, start, IST), datetime.combine(day, end, IST)


def expected_snapshots(
    now: datetime, start: time, end: time, interval_seconds: int,
    holidays: frozenset[date] = frozenset(), day: date | None = None,
) -> int:
    local = now.astimezone(IST)
    target = day or local.date()
    if target.weekday() >= 5 or target in holidays:
        return 0
    start_at, end_at = session_bounds(target, start, end)
    cutoff = end_at if target < local.date() else min(max(local, start_at), end_at)
    if cutoff < start_at:
        return 0
    return int((cutoff - start_at).total_seconds() // interval_seconds) + 1


def freshness_status(
    age_seconds: float | None, session: MarketSessionState,
    healthy_seconds: int, degraded_seconds: int,
) -> HealthStatus:
    if session != MarketSessionState.OPEN:
        return HealthStatus.IDLE
    if age_seconds is None:
        return HealthStatus.UNKNOWN
    if age_seconds <= healthy_seconds:
        return HealthStatus.HEALTHY
    if age_seconds <= degraded_seconds:
        return HealthStatus.DEGRADED
    return HealthStatus.UNHEALTHY


def heartbeat_status(
    age_seconds: float | None, session: MarketSessionState, interval_seconds: int,
) -> HealthStatus:
    if session != MarketSessionState.OPEN:
        return HealthStatus.IDLE
    if age_seconds is None:
        return HealthStatus.UNKNOWN
    if age_seconds <= interval_seconds * 2:
        return HealthStatus.HEALTHY
    if age_seconds <= interval_seconds * 3:
        return HealthStatus.DEGRADED
    return HealthStatus.UNHEALTHY


def aggregate_status(components: dict[str, str], session: MarketSessionState) -> HealthStatus:
    required = [components.get(name) for name in
                ("database", "collector_heartbeat", "data_freshness", "market_data", "pipeline", "shadow")]
    if HealthStatus.UNHEALTHY.value in required:
        return HealthStatus.UNHEALTHY
    if HealthStatus.DEGRADED.value in required or HealthStatus.UNKNOWN.value in required:
        return HealthStatus.DEGRADED
    if session != MarketSessionState.OPEN and all(
        value in {HealthStatus.HEALTHY.value, HealthStatus.IDLE.value, None} for value in required
    ):
        return HealthStatus.IDLE
    return HealthStatus.HEALTHY


def age_seconds(now: datetime, value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return max(0.0, round((now.astimezone(IST) - value.astimezone(IST)).total_seconds(), 2))
