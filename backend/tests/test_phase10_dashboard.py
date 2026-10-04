from fastapi.testclient import TestClient

from app.collector.service import collection_bucket
from app.db.session import get_session_factory
from app.main import app


def test_dashboard_aggregate_empty_state(session_factory):
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    try:
        response = TestClient(app).get("/api/dashboard/latest")
        assert response.status_code == 200
        body = response.json()
        assert body["snapshot"] is None
        assert body["open_shadow_trade"] is None
        assert body["open_shadow_thresholds"] is None
        assert body["daily_summary"]["snapshots_collected"] == 0
        assert body["system"]["database_api"] == "CONNECTED"
        assert "openai_api_key" not in str(body).lower()
    finally:
        app.dependency_overrides.clear()


def test_dashboard_aggregate_uses_persisted_snapshot(repository, session_factory, market_snapshot):
    run_id = repository.create_collector_run(market_snapshot.timestamp_ist)
    result = repository.save_market_snapshot(
        market_snapshot, collection_bucket(market_snapshot.timestamp_ist, 3), run_id
    )
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    try:
        body = TestClient(app).get("/api/dashboard/latest").json()
        assert body["snapshot"]["id"] == result.snapshot_id
        assert float(body["snapshot"]["nifty_spot"]) == market_snapshot.nifty_spot
        assert len(body["snapshot"]["options"]) == 42
        assert body["features"] is None
    finally:
        app.dependency_overrides.clear()
