from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.snapshots import get_snapshot_repository
from app.collector.service import collection_bucket
from app.data.models import MarketSnapshot
from app.db.models import (
    CollectorRunRecord,
    MarketSnapshotRecord,
    OptionContractSnapshotRecord,
)
from app.db.repositories import SnapshotRepository
from app.main import app

IST = ZoneInfo("Asia/Kolkata")


def save(repository: SnapshotRepository, snapshot: MarketSnapshot):
    run_id = repository.create_collector_run(snapshot.timestamp_ist)
    return repository.save_market_snapshot(
        snapshot, collection_bucket(snapshot.timestamp_ist, 3), run_id
    )


def test_market_snapshot_and_42_contracts_persist(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> None:
    result = save(repository, market_snapshot)
    with session_factory() as session:
        stored = session.get(MarketSnapshotRecord, result.snapshot_id)
        count = session.scalar(select(func.count(OptionContractSnapshotRecord.id)))
    assert stored is not None
    assert stored.nifty_spot == Decimal("25012.550000")
    assert result.contracts == count == 42


def test_nullable_vix_and_future_persist(
    repository: SnapshotRepository,
    market_snapshot: MarketSnapshot,
) -> None:
    snapshot = market_snapshot.model_copy(update={"nifty_future": None, "india_vix": None})
    result = save(repository, snapshot)
    stored = repository.get(result.snapshot_id)
    assert stored is not None
    assert stored["nifty_future"] is None
    assert stored["india_vix"] is None


def test_oi_values_are_preserved_exactly(
    repository: SnapshotRepository,
    market_snapshot: MarketSnapshot,
) -> None:
    result = save(repository, market_snapshot)
    stored = repository.get(result.snapshot_id)
    first = stored["options"][0]  # type: ignore[index]
    assert first["open_interest"] == 1000
    assert first["previous_open_interest"] == 900
    assert first["change_in_open_interest"] == 100


def test_duplicate_bucket_reuses_complete_snapshot(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> None:
    first = save(repository, market_snapshot)
    second = save(repository, market_snapshot.model_copy(
        update={"timestamp_ist": market_snapshot.timestamp_ist.replace(second=50)}
    ))
    with session_factory() as session:
        snapshots = session.scalar(select(func.count(MarketSnapshotRecord.id)))
        contracts = session.scalar(select(func.count(OptionContractSnapshotRecord.id)))
    assert second.duplicate is True
    assert second.snapshot_id == first.snapshot_id
    assert snapshots == 1
    assert contracts == 42


def test_contract_uniqueness_and_atomic_rollback(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> None:
    bad = market_snapshot.model_copy(
        update={"options": [market_snapshot.options[0], market_snapshot.options[0]]}
    )
    run_id = repository.create_collector_run(bad.timestamp_ist)
    with pytest.raises(IntegrityError):
        repository.save_market_snapshot(bad, collection_bucket(bad.timestamp_ist, 3), run_id)
    with session_factory() as session:
        assert session.scalar(select(func.count(MarketSnapshotRecord.id))) == 0
        assert session.scalar(select(func.count(OptionContractSnapshotRecord.id))) == 0
        run = session.get(CollectorRunRecord, run_id)
        assert run is not None and run.status == "FAILED"


def test_collector_run_success_links_snapshot(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
    market_snapshot: MarketSnapshot,
) -> None:
    result = save(repository, market_snapshot)
    with session_factory() as session:
        run = session.scalar(select(CollectorRunRecord))
    assert run is not None
    assert run.status == "SUCCESS"
    assert run.snapshot_id == result.snapshot_id
    assert run.contracts_received == 42
    assert run.completed_at is not None


def test_failed_run_stores_only_safe_error(
    repository: SnapshotRepository,
    session_factory: sessionmaker[Session],
) -> None:
    run_id = repository.create_collector_run(datetime.now(IST))
    repository.mark_collector_failed(run_id, RuntimeError("token=super-secret"))
    with session_factory() as session:
        run = session.get(CollectorRunRecord, run_id)
    assert run is not None and run.status == "FAILED"
    assert run.error_type == "RuntimeError"
    assert "super-secret" not in (run.safe_error_message or "")


def test_latest_list_and_detail_queries(
    repository: SnapshotRepository,
    market_snapshot: MarketSnapshot,
) -> None:
    result = save(repository, market_snapshot)
    assert repository.latest()["id"] == result.snapshot_id  # type: ignore[index]
    assert repository.list(50)[0]["id"] == result.snapshot_id
    detail = repository.get(result.snapshot_id)
    assert detail is not None and len(detail["options"]) == 42


def test_api_snapshot_read_endpoints(
    repository: SnapshotRepository,
    market_snapshot: MarketSnapshot,
) -> None:
    result = save(repository, market_snapshot)
    app.dependency_overrides[get_snapshot_repository] = lambda: repository
    try:
        client = TestClient(app)
        assert client.get("/api/snapshots/latest").status_code == 200
        listing = client.get("/api/snapshots?limit=1").json()
        assert len(listing) == 1 and "options" not in listing[0]
        detail = client.get(f"/api/snapshots/{result.snapshot_id}").json()
        assert len(detail["options"]) == 42
    finally:
        app.dependency_overrides.clear()


def test_no_sensitive_fields_exist_in_schema(engine) -> None:
    forbidden = {"consumer_key", "authorization", "mpin", "totp", "sid", "rid", "ucc"}
    inspector = inspect(engine)
    names = {
        column["name"].lower()
        for table in inspector.get_table_names()
        for column in inspector.get_columns(table)
    }
    assert names.isdisjoint(forbidden)


def test_alembic_upgrade_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "migration.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database}")
    command.upgrade(config, "head")
    from sqlalchemy import create_engine

    migrated = inspect(create_engine(f"sqlite+pysqlite:///{database}"))
    assert set(migrated.get_table_names()) >= {
        "market_snapshots", "option_contract_snapshots", "collector_runs",
        "market_feature_snapshots", "market_regime_snapshots", "alembic_version",
        "ai_research_snapshots",
        "strategy_candidate_sets",
        "risk_decisions",
        "shadow_trades", "shadow_trade_marks",
        "pipeline_runs",
        "alpha_feature_snapshots",
    }
    assert "lot_size" in {
        column["name"] for column in migrated.get_columns("market_snapshots")
    }
    assert {"alpha_status", "alpha_feature_snapshot_id"}.issubset({
        column["name"] for column in migrated.get_columns("pipeline_runs")
    })
