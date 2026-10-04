from typing import Any

from app.observability.metrics import average, consecutive_failures, percentage, percentile_95
from app.observability.models import HealthStatus, MarketSessionState


def pipeline_health(
    latest: dict[str, Any] | None, runs: list[dict[str, Any]], session: MarketSessionState,
    expected_interval_ms: float, degraded_fraction: float,
) -> dict[str, Any]:
    success = [row for row in runs if row["status"] == "SUCCESS"]
    partial = [row for row in runs if row["status"] == "PARTIAL"]
    failed = [row for row in runs if row["status"] == "FAILED"]
    totals = [float(row.get("stage_timings_ms", {}).get("total_pipeline")) for row in runs
              if row.get("stage_timings_ms", {}).get("total_pipeline") is not None]
    p95 = percentile_95(totals)
    if session != MarketSessionState.OPEN:
        status = HealthStatus.IDLE
    elif latest is None or not runs:
        status = HealthStatus.UNKNOWN
    elif latest["status"] == "FAILED" or (p95 is not None and p95 > expected_interval_ms):
        status = HealthStatus.UNHEALTHY
    elif latest["status"] in {"PARTIAL", "STARTED"} or (
        p95 is not None and p95 > expected_interval_ms * degraded_fraction
    ):
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.HEALTHY
    stage_values: dict[str, list[float]] = {}
    for row in runs:
        for name, value in (row.get("stage_timings_ms") or {}).items():
            if name != "total_pipeline" and value is not None:
                stage_values.setdefault(name, []).append(float(value))
    averages = {name: average(values) for name, values in stage_values.items()}
    slowest = max(averages, key=lambda name: averages[name] or 0) if averages else None
    return {
        "status": status.value, "latest_pipeline_status": None if latest is None else latest["status"],
        "latest_success_at": None if not success else success[0]["completed_at"],
        "latest_partial_at": None if not partial else partial[0]["completed_at"],
        "latest_failure_at": None if not failed else failed[0]["completed_at"],
        "runs_today": len(runs), "success_today": len(success), "partial_today": len(partial),
        "failed_today": len(failed), "pipeline_success_rate_today": percentage(len(success), len(runs)),
        "consecutive_failures": consecutive_failures(row["status"] for row in runs),
        "latest_stage_statuses": {} if latest is None else {
            name: latest.get(f"{name}_status", "SKIPPED") for name in ("feature", "alpha", "regime", "ai", "strategy", "risk", "shadow")
        },
        "latest_stage_timings_ms": {} if latest is None else latest.get("stage_timings_ms", {}),
        "average_total_pipeline_ms": average(totals), "p95_total_pipeline_ms": p95,
        "average_stage_duration_ms": averages, "slowest_stage": slowest,
    }
