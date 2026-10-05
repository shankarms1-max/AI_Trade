from datetime import datetime, timedelta, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.main import app
from app.observability.ai_health import ai_health
from app.observability.collector_health import collector_health
from app.observability.database_health import check_database
from app.observability.health import (
    aggregate_status, expected_snapshots, freshness_status, heartbeat_status,
    market_session_state,
)
from app.observability.market_data_health import market_data_health
from app.observability.metrics import average, consecutive_failures, percentile_95
from app.observability.models import (
    AIHealthStatus, EventSeverity, HealthStatus, MarketSessionState, safe_exception,
)
from app.observability.pipeline_health import pipeline_health
from app.observability.repository import ObservabilityRepository
from app.observability.service import ObservabilityService
from app.observability.shadow_health import shadow_health
from scripts.system_health import exit_code

IST = ZoneInfo("Asia/Kolkata")
OPEN = datetime(2026, 10, 5, 10, 0, tzinfo=IST)


def settings(**updates):
    return get_settings().model_copy(update={
        "collector_start_time": time(9, 18), "collector_end_time": time(15, 27),
        "collector_holidays": "", **updates,
    })


def quality(**updates):
    base = {"snapshot_id": 1, "timestamp": OPEN - timedelta(minutes=1), "source": "KOTAK_NEO",
            "spot_available": True, "future_available": True, "vix_available": True,
            "lot_size_available": True, "option_contract_count": 42,
            "nonzero_volume_count": 4, "nonzero_oi_change_count": 2,
            "oi_current_prev_difference_count": 2, "oi_mismatch_count": 0,
            "intraday_oi_usable": True, "bid_ask_coverage_pct": 50.0,
            "volume_coverage_pct": 100.0, "oi_change_coverage_pct": 100.0}
    return {**base, **updates}


def test_health_enum_values():
    assert [item.value for item in HealthStatus] == ["HEALTHY", "DEGRADED", "UNHEALTHY", "UNKNOWN", "IDLE"]


@pytest.mark.parametrize(("at", "result"), [
    (datetime(2026, 10, 4, 10, tzinfo=IST), "WEEKEND"),
    (datetime(2026, 10, 5, 8, tzinfo=IST), "PRE_MARKET"),
    (OPEN, "OPEN"), (datetime(2026, 10, 5, 16, tzinfo=IST), "POST_MARKET"),
])
def test_market_session_states(at, result):
    assert market_session_state(at, time(9, 18), time(15, 27)).value == result


def test_market_holiday_state():
    assert market_session_state(OPEN, time(9, 18), time(15, 27), frozenset({OPEN.date()})).value == "HOLIDAY"


@pytest.mark.parametrize(("age", "result"), [(100, "HEALTHY"), (400, "DEGRADED"), (700, "UNHEALTHY"), (None, "UNKNOWN")])
def test_freshness_thresholds(age, result):
    assert freshness_status(age, MarketSessionState.OPEN, 300, 600).value == result


def test_closed_market_health_is_idle():
    assert freshness_status(9999, MarketSessionState.WEEKEND, 300, 600) == HealthStatus.IDLE
    assert heartbeat_status(None, MarketSessionState.POST_MARKET, 60) == HealthStatus.IDLE


@pytest.mark.parametrize(("age", "result"), [(30, "HEALTHY"), (150, "DEGRADED"), (181, "UNHEALTHY"), (None, "UNKNOWN")])
def test_heartbeat_thresholds(age, result):
    assert heartbeat_status(age, MarketSessionState.OPEN, 60).value == result


def test_expected_snapshot_cadence_is_session_aware():
    assert expected_snapshots(OPEN, time(9, 18), time(15, 27), 180) == 15
    assert expected_snapshots(datetime(2026, 10, 4, 10, tzinfo=IST), time(9, 18), time(15, 27), 180) == 0


def test_metric_helpers():
    assert average([1, 2, 3]) == 2
    assert percentile_95(range(1, 101)) == 95
    assert consecutive_failures(["FAILED", "PARTIAL", "SUCCESS"]) == 2


def test_database_healthy_and_latency(session_factory):
    result = check_database(session_factory)
    assert result["status"] == "HEALTHY" and result["latency_ms"] >= 0


def test_database_failure_is_safe():
    broken = sessionmaker(bind=create_engine("sqlite+pysqlite:///Z:/path/that/cannot/exist/db.sqlite"))
    result = check_database(broken)
    assert result["status"] == "UNHEALTHY"
    assert result["safe_error_type"] and "sqlite:///" not in result["safe_error_message"]


@pytest.mark.parametrize(("changes", "result"), [
    ({}, "HEALTHY"), ({"spot_available": False}, "UNHEALTHY"),
    ({"vix_available": False}, "DEGRADED"), ({"lot_size_available": False}, "DEGRADED"),
    ({"option_contract_count": 0}, "UNHEALTHY"), ({"oi_mismatch_count": 1}, "HEALTHY"),
])
def test_market_data_statuses(changes, result):
    assert market_data_health(quality(**changes), MarketSessionState.OPEN)["status"] == result


def test_market_data_closed_is_idle():
    assert market_data_health(quality(), MarketSessionState.WEEKEND)["status"] == "IDLE"


@pytest.mark.parametrize(("latest_status", "result"), [("SUCCESS", "HEALTHY"), ("PARTIAL", "DEGRADED"), ("FAILED", "UNHEALTHY")])
def test_pipeline_latest_status(latest_status, result):
    row = {"status": latest_status, "completed_at": OPEN, "feature_status": "SUCCESS",
           "regime_status": "SUCCESS", "ai_status": "SKIPPED", "strategy_status": "SUCCESS",
           "risk_status": "SUCCESS", "shadow_status": "SUCCESS",
           "stage_timings_ms": {"features": 10, "total_pipeline": 100}}
    assert pipeline_health(row, [row], MarketSessionState.OPEN, 180_000, .5)["status"] == result


def test_slow_pipeline_thresholds_and_timings():
    row = {"status": "SUCCESS", "completed_at": OPEN, "feature_status": "SUCCESS",
           "regime_status": "SUCCESS", "ai_status": "SKIPPED", "strategy_status": "SUCCESS",
           "risk_status": "SUCCESS", "shadow_status": "SUCCESS",
           "stage_timings_ms": {"features": 60, "risk": 20, "total_pipeline": 100}}
    result = pipeline_health(row, [row], MarketSessionState.OPEN, 150, .5)
    assert result["status"] == "DEGRADED" and result["p95_total_pipeline_ms"] == 100
    assert result["slowest_stage"] == "features"


@pytest.mark.parametrize(("configured", "latest", "result"), [
    (False, None, AIHealthStatus.DISABLED), (True, None, AIHealthStatus.CONFIGURED_NOT_TESTED),
    (True, "SUCCESS", AIHealthStatus.HEALTHY), (True, "FAILED", AIHealthStatus.FAILED),
])
def test_ai_health_without_probe(configured, latest, result):
    metrics = {"latest_status": latest, "last_latency_ms": 100, "calls_today": 0,
               "cost_today_usd": 0, "last_success_at": None, "last_failure_at": None}
    assert ai_health(configured, "model" if configured else None, metrics)["status"] == result.value


def test_shadow_idle_healthy_stale_and_anomaly():
    base = {"open_shadow_trades": 0, "latest_shadow_mark_at": None,
            "payout_anomalies_today": 0, "invalid_shadow_trades_today": 0}
    assert shadow_health(OPEN, MarketSessionState.OPEN, base, 360)["status"] == "IDLE"
    assert shadow_health(OPEN, MarketSessionState.OPEN, {**base, "open_shadow_trades": 1}, 360)["status"] == "DEGRADED"
    assert shadow_health(OPEN, MarketSessionState.OPEN, {**base, "payout_anomalies_today": 1}, 360)["status"] == "UNHEALTHY"


def test_safe_exception_redacts_credentials():
    kind, message = safe_exception(RuntimeError("password=hunter2 token=abc postgresql://u:p@host/db"))
    assert kind == "RuntimeError" and "hunter2" not in message and "abc" not in message and "u:p" not in message


def test_event_persistence_filter_and_recovery_no_spam(session_factory):
    repo = ObservabilityRepository(session_factory)
    event_id = repo.record_event("database", EventSeverity.ERROR, "DB_FAILED", "password=secret")
    assert event_id and "secret" not in repo.events()[0]["safe_message"]
    assert repo.record_transition("DATABASE", "UNHEALTHY", "HEALTHY") is not None
    assert repo.record_transition("DATABASE", "UNHEALTHY", "HEALTHY") is None
    assert len(repo.events(component="DATABASE", severity="INFO")) == 1


def test_health_transition_and_metadata_redaction(session_factory):
    repo = ObservabilityRepository(session_factory)
    assert repo.record_health_transition("COLLECTOR", "UNHEALTHY", "MISSED", "RECOVERED", "missed")
    assert repo.record_health_transition("COLLECTOR", "UNHEALTHY", "MISSED", "RECOVERED", "missed") is None
    assert repo.record_health_transition("COLLECTOR", "HEALTHY", "MISSED", "RECOVERED", "missed")
    repo.record_event("TEST", "INFO", "SAFE", "safe", metadata={"token": "leak", "count": 1})
    assert repo.events(component="TEST")[0]["metadata_json"] == {"token": "[REDACTED]", "count": 1}


def test_heartbeat_persistence(session_factory):
    repo = ObservabilityRepository(session_factory)
    repo.upsert_heartbeat("worker", OPEN, started_at=OPEN)
    repo.upsert_heartbeat("worker", OPEN + timedelta(seconds=60))
    stored = repo.latest_heartbeat()["last_heartbeat_at"]
    assert stored.replace(tzinfo=IST) == OPEN + timedelta(seconds=60)


@pytest.mark.parametrize(("components", "session", "result"), [
    ({"database":"HEALTHY","collector_heartbeat":"HEALTHY","data_freshness":"HEALTHY","market_data":"HEALTHY","pipeline":"HEALTHY","shadow":"IDLE"}, MarketSessionState.OPEN, "HEALTHY"),
    ({"database":"HEALTHY","collector_heartbeat":"DEGRADED"}, MarketSessionState.OPEN, "DEGRADED"),
    ({"database":"UNHEALTHY"}, MarketSessionState.OPEN, "UNHEALTHY"),
    ({"database":"HEALTHY","collector_heartbeat":"IDLE","data_freshness":"IDLE","market_data":"IDLE","pipeline":"IDLE","shadow":"IDLE"}, MarketSessionState.WEEKEND, "IDLE"),
])
def test_aggregate_health(components, session, result):
    assert aggregate_status(components, session).value == result


def test_ai_disabled_does_not_change_overall_health():
    components = {"database":"HEALTHY","collector_heartbeat":"HEALTHY","data_freshness":"HEALTHY",
                  "market_data":"HEALTHY","pipeline":"HEALTHY","shadow":"IDLE","ai":"DISABLED"}
    assert aggregate_status(components, MarketSessionState.OPEN) == HealthStatus.HEALTHY


def test_health_apis_and_daily_summary(session_factory):
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    try:
        client = TestClient(app)
        assert client.get("/api/health").status_code == 200
        detail = client.get("/api/system/health")
        assert detail.status_code == 200 and "database" in detail.json()
        assert client.get("/api/system/events?limit=10").status_code == 200
        assert client.get("/api/system/metrics").status_code == 200
        summary = ObservabilityService(session_factory, settings()).daily_operations_summary(OPEN.date())
        assert summary["date"] == OPEN.date() and "data_quality" in summary
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(("status", "code"), [("HEALTHY", 0), ("IDLE", 0), ("DEGRADED", 1), ("UNKNOWN", 1), ("UNHEALTHY", 2)])
def test_health_script_exit_codes(status, code):
    assert exit_code(status) == code

