from typing import Any

from app.observability.models import AIHealthStatus


def ai_health(configured: bool, model: str | None, metrics: dict[str, Any]) -> dict[str, Any]:
    if not configured:
        status = AIHealthStatus.DISABLED
    elif metrics["latest_status"] is None:
        status = AIHealthStatus.CONFIGURED_NOT_TESTED
    elif metrics["latest_status"] == "FAILED":
        status = AIHealthStatus.FAILED
    elif metrics["last_latency_ms"] is not None and metrics["last_latency_ms"] > 30_000:
        status = AIHealthStatus.DEGRADED
    else:
        status = AIHealthStatus.HEALTHY
    return {"status": status.value, "model_configured": bool(model), **metrics}
