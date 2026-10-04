from datetime import timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.features import get_feature_repository
from app.collector.service import collection_bucket
from app.data.models import MarketSnapshot
from app.db.models import MarketFeatureSnapshotRecord, MarketSnapshotRecord, OptionContractSnapshotRecord
from app.db.repositories import SnapshotRepository
from app.features.engine import FeatureEngineConfig
from app.features.repository import FeatureRepository
from app.features.service import backfill_features, build_and_store_features
from app.main import app


def store_raw(repository: SnapshotRepository, value: MarketSnapshot):
    run_id = repository.create_collector_run(value.timestamp_ist)
    return repository.save_market_snapshot(value, collection_bucket(value.timestamp_ist, 3), run_id)


def test_feature_persistence_raw_immutability_and_idempotent_recompute(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> None:
    raw = store_raw(repository, market_snapshot)
    features = FeatureRepository(session_factory)
    raw_before = features.load_raw(raw.snapshot_id)
    with session_factory() as session:
        raw_count = session.scalar(select(func.count(MarketSnapshotRecord.id)))
        option_count = session.scalar(select(func.count(OptionContractSnapshotRecord.id)))
    first = build_and_store_features(features, raw.snapshot_id, FeatureEngineConfig())
    second = build_and_store_features(features, raw.snapshot_id, FeatureEngineConfig())
    with session_factory() as session:
        feature_count = session.scalar(select(func.count(MarketFeatureSnapshotRecord.id)))
        assert session.scalar(select(func.count(MarketSnapshotRecord.id))) == raw_count
        assert session.scalar(select(func.count(OptionContractSnapshotRecord.id))) == option_count
    assert first == second
    assert feature_count == 1
    assert features.load_raw(raw.snapshot_id) == raw_before
    assert features.get(raw.snapshot_id)["feature_version"] == "phase3_v1"  # type: ignore[index]


def test_feature_latest_list_and_api(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> None:
    raw = store_raw(repository, market_snapshot)
    features = FeatureRepository(session_factory)
    build_and_store_features(features, raw.snapshot_id, FeatureEngineConfig())
    assert features.latest()["snapshot_id"] == raw.snapshot_id  # type: ignore[index]
    assert features.list(50)[0]["snapshot_id"] == raw.snapshot_id
    app.dependency_overrides[get_feature_repository] = lambda: features
    try:
        client = TestClient(app)
        assert client.get("/api/features/latest").status_code == 200
        assert client.get(f"/api/features/{raw.snapshot_id}").json()["snapshot_id"] == raw.snapshot_id
        assert len(client.get("/api/features?limit=50").json()) == 1
    finally:
        app.dependency_overrides.clear()


def test_backfill_is_chronological_and_uses_only_database(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> None:
    later = market_snapshot.model_copy(
        update={"timestamp_ist": market_snapshot.timestamp_ist + timedelta(minutes=3), "nifty_spot": 25020}
    )
    # Insert deliberately out of chronological order with distinct buckets.
    later_result = store_raw(repository, later)
    earlier_result = store_raw(repository, market_snapshot)
    features = FeatureRepository(session_factory)
    built = backfill_features(features, FeatureEngineConfig())
    assert [item.snapshot_id for item in built] == [earlier_result.snapshot_id, later_result.snapshot_id]
    assert built[1].price_structure_features.spot_change_from_previous_snapshot == pytest.approx(7.45)
