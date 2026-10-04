from typing import Any

from app.observability.models import HealthStatus, MarketSessionState


def market_data_health(quality: dict[str, Any] | None, session: MarketSessionState) -> dict[str, Any]:
    if session != MarketSessionState.OPEN:
        status = HealthStatus.IDLE
    elif quality is None:
        status = HealthStatus.UNKNOWN
    elif not quality["spot_available"] or quality["option_contract_count"] == 0:
        status = HealthStatus.UNHEALTHY
    elif not all(quality[key] for key in ("future_available", "vix_available", "lot_size_available")):
        status = HealthStatus.DEGRADED
    elif quality["oi_mismatch_count"]:
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.HEALTHY
    return {"status": status.value, **(quality or {
        "spot_available": False, "future_available": False, "vix_available": False,
        "lot_size_available": False, "option_contract_count": 0,
        "nonzero_volume_count": 0, "nonzero_oi_change_count": 0,
        "oi_current_prev_difference_count": 0, "oi_mismatch_count": 0,
        "intraday_oi_usable": False, "bid_ask_coverage_pct": None,
        "volume_coverage_pct": None, "oi_change_coverage_pct": None,
    })}
