from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.regime import get_regime_repository
from app.collector.service import collection_bucket
from app.data.models import MarketSnapshot
from app.db.models import MarketRegimeSnapshotRecord
from app.db.repositories import SnapshotRepository
from app.features.engine import FeatureEngineConfig
from app.features.repository import FeatureRepository
from app.features.service import build_and_store_features
from app.main import app
from app.regime.engine import RegimeConfig
from app.regime.repository import RegimeRepository
from app.regime.service import backfill_regimes, build_and_store_regime


def store_raw(repository: SnapshotRepository, value: MarketSnapshot):
    run_id = repository.create_collector_run(value.timestamp_ist)
    return repository.save_market_snapshot(value, collection_bucket(value.timestamp_ist, 3), run_id)


def test_regime_persistence_unique_and_idempotent(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> None:
    raw = store_raw(repository, market_snapshot)
    features = FeatureRepository(session_factory)
    build_and_store_features(features, raw.snapshot_id, FeatureEngineConfig())
    regimes = RegimeRepository(session_factory)
    first = build_and_store_regime(regimes, raw.snapshot_id, RegimeConfig())
    second = build_and_store_regime(regimes, raw.snapshot_id, RegimeConfig())
    with session_factory() as session:
        count = session.scalar(select(func.count(MarketRegimeSnapshotRecord.id)))
    assert first == second
    assert count == 1
    assert regimes.get(raw.snapshot_id)["regime_version"] == "phase4_v1"  # type: ignore[index]


def test_regime_api_latest_detail_and_list(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> None:
    raw = store_raw(repository, market_snapshot)
    features = FeatureRepository(session_factory)
    build_and_store_features(features, raw.snapshot_id, FeatureEngineConfig())
    regimes = RegimeRepository(session_factory)
    build_and_store_regime(regimes, raw.snapshot_id, RegimeConfig())
    app.dependency_overrides[get_regime_repository] = lambda: regimes
    try:
        client = TestClient(app)
        assert client.get("/api/regime/latest").status_code == 200
        assert client.get(f"/api/regime/{raw.snapshot_id}").json()["snapshot_id"] == raw.snapshot_id
        assert len(client.get("/api/regime?limit=50").json()) == 1
    finally:
        app.dependency_overrides.clear()


def test_regime_backfill_is_chronological_and_database_only(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> None:
    later = market_snapshot.model_copy(
        update={"timestamp_ist": market_snapshot.timestamp_ist + timedelta(minutes=3), "nifty_spot": 25020}
    )
    later_raw = store_raw(repository, later)
    earlier_raw = store_raw(repository, market_snapshot)
    features = FeatureRepository(session_factory)
    # Feature builds are also deliberately requested out of order.
    build_and_store_features(features, later_raw.snapshot_id, FeatureEngineConfig())
    build_and_store_features(features, earlier_raw.snapshot_id, FeatureEngineConfig())
    regimes = RegimeRepository(session_factory)
    results = backfill_regimes(regimes, RegimeConfig())
    assert [item.snapshot_id for item in results] == [earlier_raw.snapshot_id, later_raw.snapshot_id]
