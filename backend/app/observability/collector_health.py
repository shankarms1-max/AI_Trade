from datetime import datetime
from typing import Any

from app.observability.health import age_seconds, freshness_status, heartbeat_status
from app.observability.metrics import average, consecutive_failures, percentage, percentile_95
from app.observability.models import MarketSessionState


def collector_health(
    now: datetime, session: MarketSessionState, heartbeat: dict[str, Any] | None,
    runs: list[dict[str, Any]], latest_snapshot: dict[str, Any] | None,
    heartbeat_seconds: int, freshness_healthy: int, freshness_degraded: int,
) -> dict[str, Any]:
    heartbeat_age = age_seconds(now, None if heartbeat is None else heartbeat["last_heartbeat_at"])
    snapshot_age = age_seconds(now, None if latest_snapshot is None else latest_snapshot["timestamp"])
    successes = [row for row in runs if row["status"] == "SUCCESS"]
    failures = [row for row in runs if row["status"] == "FAILED"]
    durations = [
        (row["completed_at"] - row["started_at"]).total_seconds() * 1000
        for row in runs if row["completed_at"] is not None
    ]
    heartbeat_health = heartbeat_status(heartbeat_age, session, heartbeat_seconds)
    if (session == MarketSessionState.OPEN and heartbeat is not None
            and heartbeat.get("status") == "STOPPED"):
        from app.observability.models import HealthStatus
        heartbeat_health = HealthStatus.UNHEALTHY
    return {
        "heartbeat": {
            "status": heartbeat_health.value,
            "worker_id": None if heartbeat is None else heartbeat["worker_id"],
            "worker_status": None if heartbeat is None else heartbeat["status"],
            "last_heartbeat_at": None if heartbeat is None else heartbeat["last_heartbeat_at"],
            "age_seconds": heartbeat_age,
        },
        "data_freshness": {
            "status": freshness_status(snapshot_age, session, freshness_healthy, freshness_degraded).value,
            "last_snapshot_at": None if latest_snapshot is None else latest_snapshot["timestamp"],
            "last_snapshot_id": None if latest_snapshot is None else latest_snapshot["snapshot_id"],
            "age_seconds": snapshot_age,
        },
        "runs_today": len(runs), "success_today": len(successes), "failed_today": len(failures),
        "success_rate_today": percentage(len(successes), len(runs)),
        "last_success_at": None if not successes else successes[0]["completed_at"],
        "last_failure_at": None if not failures else failures[0]["completed_at"],
        "consecutive_failures": consecutive_failures(row["status"] for row in runs),
        "average_collection_duration_ms": average(durations),
        "p95_collection_duration_ms": percentile_95(durations),
    }
