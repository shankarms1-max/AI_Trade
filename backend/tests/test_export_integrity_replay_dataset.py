"""Focused offline checks for the read-only replay dataset export."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import importlib.util
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.alpha.engine import AlphaConfig, build_alpha_features
from app.alpha.models import AlphaFeatureSnapshot, CalculationMode
from app.alpha.repository import AlphaRepository
from app.data.models import MarketSnapshot
from app.db.models import (AlphaFeatureSnapshotRecord, MarketFeatureSnapshotRecord,
                           MarketSnapshotRecord)
from app.features.engine import build_market_features
from app.features.models import MarketFeatureSnapshot
from app.features.repository import FeatureRepository


@pytest.fixture
def exporter():
    path = Path(__file__).resolve().parents[2] / "scripts" / "export_integrity_replay_dataset.py"
    spec = importlib.util.spec_from_file_location("integrity_exporter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw(market_snapshot, minute):
    at = market_snapshot.timestamp_ist + timedelta(minutes=minute)
    option_rows = [row.model_copy(update={
        "source_market_timestamp": at, "bid": Decimal("12.123456"),
        "ask": Decimal("12.173456"), "bid_quantity": 75, "ask_quantity": 125,
        "depth_unit": "UNITS", "tick_size": Decimal("0.050000"),
        "open_interest": 1201, "previous_open_interest": 1200,
        "change_in_open_interest": 1, "volume": 567,
    }) for row in market_snapshot.options[:2]]
    return market_snapshot.model_copy(update={
        "timestamp_ist": at, "options": option_rows,
        "future_instrument_id": "NIFTY-FUT-TEST", "future_expiry": market_snapshot.expiry,
        "source_market_timestamp": at, "request_started_at": at,
        "response_received_at": at + timedelta(seconds=2), "india_vix": 13.25,
    })


def _save(repository, snapshot, *, bucket_offset=0):
    return repository.save_market_snapshot(
        snapshot, snapshot.timestamp_ist + timedelta(seconds=bucket_offset)
    ).snapshot_id


def _export(exporter, session_factory, path, start=date(2099, 10, 1), end=date(2099, 10, 1)):
    with session_factory() as session:
        return exporter.export_dataset(session, start, end, path)


def test_missing_feature_rows_retained_in_timestamp_then_id_order(
    exporter, repository, session_factory, market_snapshot, tmp_path, monkeypatch
):
    monkeypatch.setattr(exporter, "BATCH_SIZE", 2)
    late = _save(repository, _raw(market_snapshot, 6))
    early = _save(repository, _raw(market_snapshot, 0))
    middle = _save(repository, _raw(market_snapshot, 3))
    tied = _save(repository, _raw(market_snapshot, 3), bucket_offset=1)
    path = tmp_path / "dataset.json"
    summary = _export(exporter, session_factory, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [row["snapshot_id"] for row in payload] == [early, middle, tied, late]
    assert all(row["feature_id"] is None and row["feature"] is None and row["alpha"] is None
               for row in payload)
    assert summary["snapshots_exported"] == 4
    assert summary["snapshots_missing_features"] == 4
    assert summary["snapshots_missing_alpha"] == 4
    assert summary["snapshots_with_features"] == summary["snapshots_with_alpha"] == 0
    # Both bounds are inclusive by IST calendar date; no other date is fabricated.
    assert _export(exporter, session_factory, tmp_path / "empty.json",
                   date(2099, 10, 2), date(2099, 10, 2))["snapshots_exported"] == 0
    assert json.loads((tmp_path / "empty.json").read_text(encoding="utf-8")) == []


def test_exact_option_depth_and_snapshot_metadata_survive_serialization(
    exporter, repository, session_factory, market_snapshot, tmp_path
):
    snapshot_id = _save(repository, _raw(market_snapshot, 0))
    output = tmp_path / "dataset.json"
    _export(exporter, session_factory, output)
    item = json.loads(output.read_text(encoding="utf-8"))[0]
    with session_factory() as session:
        record = session.get(MarketSnapshotRecord, snapshot_id)
        stored = sorted(record.options, key=lambda option: option.id)[0]
        option = item["snapshot"]["options"][0]
        point = item["snapshot"]
        assert point["timestamp_ist"] == record.timestamp_ist.isoformat()
        assert point["source_market_timestamp"] == record.source_market_timestamp.isoformat()
        assert point["request_started_at"] == record.request_started_at.isoformat()
        assert point["response_received_at"] == record.response_received_at.isoformat()
        assert point["snapshot_persisted_at"] == record.created_at.isoformat()
        assert point["future_instrument_id"] == record.future_instrument_id
        assert point["future_expiry"] == record.future_expiry.isoformat()
        assert point["nifty_spot"] == str(record.nifty_spot)
        assert point["nifty_future"] == str(record.nifty_future)
        assert point["india_vix"] == str(record.india_vix)
        assert point["lot_size"] == record.lot_size
        assert point["atm_strike"] == str(record.atm_strike)
        assert point["expiry"] == record.expiry.isoformat()
        assert point["source"] == record.source
        for name in ("trading_symbol", "instrument_token", "exchange", "option_type",
                     "bid_quantity", "ask_quantity", "depth_unit", "open_interest",
                     "previous_open_interest", "change_in_open_interest", "volume"):
            assert option[name] == getattr(stored, name)
        for name in ("strike", "ltp", "bid", "ask", "tick_size"):
            assert option[name] == str(getattr(stored, name))
        assert option["source_market_timestamp"] == stored.source_market_timestamp.isoformat()
        assert option["expiry"] == stored.expiry.isoformat()
        assert option["implied_volatility"] is None
        assert option["delta"] is None and option["theta"] is None
    assert len(item["snapshot"]["options"]) == 2
    assert MarketSnapshot.model_validate(point).options[0].depth_unit == "UNITS"


def test_feature_alpha_payloads_are_attached_unchanged_without_recomputation(
    exporter, repository, session_factory, market_snapshot, tmp_path, monkeypatch
):
    raw = _raw(market_snapshot, 0)
    snapshot_id = _save(repository, raw)
    feature = build_market_features(snapshot_id, raw, [])
    alpha = build_alpha_features(snapshot_id, raw, [], [], AlphaConfig())
    feature_id = FeatureRepository(session_factory).upsert(feature)
    AlphaRepository(session_factory).upsert(alpha)
    with session_factory() as session:
        saved_feature = session.scalar(select(MarketFeatureSnapshotRecord).where(
            MarketFeatureSnapshotRecord.market_snapshot_id == snapshot_id)).feature_json
        saved_alpha = session.scalar(select(AlphaFeatureSnapshotRecord).where(
            AlphaFeatureSnapshotRecord.market_snapshot_id == snapshot_id)).result_json

    def recomputation_forbidden(*args, **kwargs):
        raise AssertionError("exporter must not invoke a feature or alpha engine")

    monkeypatch.setattr("app.features.engine.build_market_features", recomputation_forbidden)
    monkeypatch.setattr("app.alpha.engine.build_alpha_features", recomputation_forbidden)
    statements = []

    def capture_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().upper())

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        output = tmp_path / "dataset.json"
        summary = _export(exporter, session_factory, output)
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)
    assert statements and all(statement.startswith(("SELECT", "PRAGMA")) for statement in statements)
    assert summary["snapshots_with_features"] == summary["snapshots_with_alpha"] == 1
    item = json.loads(output.read_text(encoding="utf-8"))[0]
    assert item["feature_id"] == feature_id
    assert item["feature"] == saved_feature
    assert item["alpha"] == saved_alpha
    assert MarketFeatureSnapshot.model_validate(item["feature"]) == feature
    assert AlphaFeatureSnapshot.model_validate(item["alpha"]) == alpha
    with session_factory() as session:
        assert session.get(MarketFeatureSnapshotRecord, feature_id).feature_json == saved_feature
        assert session.scalar(select(AlphaFeatureSnapshotRecord).where(
            AlphaFeatureSnapshotRecord.market_snapshot_id == snapshot_id)).result_json == saved_alpha


@pytest.mark.parametrize("kind", ["feature", "alpha"])
def test_duplicate_related_rows_abort_without_publishing_partial_file(
    kind, exporter, repository, session_factory, market_snapshot, tmp_path
):
    _save(repository, _raw(market_snapshot, 0))
    raw = _raw(market_snapshot, 3)
    snapshot_id = _save(repository, raw)
    if kind == "feature":
        feature = build_market_features(snapshot_id, raw, [])
        repo = FeatureRepository(session_factory)
        repo.upsert(feature)
        repo.upsert(feature.model_copy(update={"feature_version": "other_version"}))
    else:
        alpha = build_alpha_features(snapshot_id, raw, [], [], AlphaConfig())
        repo = AlphaRepository(session_factory)
        repo.upsert(alpha)
        repo.upsert(alpha.model_copy(update={"calculation_mode": CalculationMode.LIVE_ORIGINAL}))
    output = tmp_path / "dataset.json"
    with pytest.raises(exporter.DuplicateRelatedRowsError, match=f"duplicate {kind} rows"):
        _export(exporter, session_factory, output)
    assert not output.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_deterministic_bytes_and_refuse_existing_output(
    exporter, repository, session_factory, market_snapshot, tmp_path
):
    _save(repository, _raw(market_snapshot, 0))
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    _export(exporter, session_factory, first)
    _export(exporter, session_factory, second)
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(FileExistsError):
        _export(exporter, session_factory, first)
    assert first.read_bytes() == second.read_bytes()


def test_cli_prints_only_summary_and_redacts_failure_details(
    exporter, repository, session_factory, market_snapshot, tmp_path, monkeypatch, capsys
):
    _save(repository, _raw(market_snapshot, 0))
    output = tmp_path / "dataset.json"
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret-user:secret-password@localhost/db")
    assert exporter.main(["--start-date", "2099-10-01", "--end-date", "2099-10-01",
                          "--output", str(output)], session_factory=session_factory) == 0
    printed = capsys.readouterr()
    summary = json.loads(printed.out)
    assert printed.err == ""
    assert set(summary) == {"start_date", "end_date", "snapshots_exported", "snapshots_with_features",
                            "snapshots_missing_features", "snapshots_with_alpha",
                            "snapshots_missing_alpha", "output"}
    assert summary["output"] == str(output.resolve())
    assert "secret-" not in printed.out

    def broken_factory():
        raise RuntimeError("postgresql://secret-user:secret-password@localhost/db")

    failed = tmp_path / "failed.json"
    assert exporter.main(["--start-date", "2099-10-01", "--end-date", "2099-10-01",
                          "--output", str(failed)], session_factory=broken_factory) == 1
    printed = capsys.readouterr()
    assert printed.out == "" and "RuntimeError" in printed.err
    assert "secret-" not in printed.err and not failed.exists()


def test_invalid_date_range_fails_without_output(exporter, session_factory, tmp_path):
    target = tmp_path / "dataset.json"
    with session_factory() as session:
        with pytest.raises(ValueError, match="start date"):
            exporter.export_dataset(session, date(2099, 10, 2), date(2099, 10, 1), target)
    assert not target.exists()


def test_old_0013_schema_omits_absent_depth_columns_without_inventing_values(
    exporter, tmp_path
):
    project = Path(__file__).resolve().parents[2]
    database_url = f"sqlite:///{tmp_path / 'old_schema.db'}"
    config = Config(str(project / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0013_phase14_2_strategy_logic")
    engine = create_engine(database_url)
    observed = date(2099, 10, 1)
    expiry = date(2099, 10, 8)
    with engine.begin() as connection:
        snapshots = Table("market_snapshots", MetaData(), autoload_with=connection)
        inserted = connection.execute(snapshots.insert().values(
            timestamp_ist=datetime(2099, 10, 1, 10, 16, 25),
            collection_bucket_ist=datetime(2099, 10, 1, 10, 16, 25),
            nifty_spot=25000, atm_strike=25000, expiry=expiry, source="STORED_FIXTURE"
        ))
        options = Table("option_contract_snapshots", MetaData(), autoload_with=connection)
        connection.execute(options.insert().values(
            market_snapshot_id=inserted.inserted_primary_key[0], strike=25000,
            option_type="CE", expiry=expiry, trading_symbol="STORED_FIXTURE",
            exchange="nse_fo", instrument_token="TOKEN_FIXTURE", ltp=100
        ))
    try:
        with sessionmaker(bind=engine)() as session:
            output = tmp_path / "old_schema.json"
            summary = exporter.export_dataset(session, observed, observed, output)
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert summary["snapshots_exported"] == 1
        contract = payload[0]["snapshot"]["options"][0]
        assert "depth_unit" not in contract and "tick_size" not in contract
        assert contract["instrument_token"] == "TOKEN_FIXTURE"
        parsed = MarketSnapshot.model_validate(payload[0]["snapshot"])
        assert parsed.options[0].depth_unit == "UNKNOWN" and parsed.options[0].tick_size is None
    finally:
        engine.dispose()
