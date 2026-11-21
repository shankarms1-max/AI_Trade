"""Scalar-only bootstrap provenance, transaction safety and native-stream isolation."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import gzip
import inspect
import json

import pytest
from pydantic_settings import BaseSettings
from sqlalchemy import func, select, update

from app.adaptive_supertrend import BOOTSTRAP_MODE, CAPTURE_MODE
from app.adaptive_supertrend import bootstrap
from app.adaptive_supertrend.market import history, mode_clause, pending_ids
from app.adaptive_supertrend.models import AstCursor, AstEvent, AstTrade, OptimizerRun
from app.adaptive_supertrend.service import AstPaperService, freeze
from app.db.session import build_session_factory
from app.research.manifest import canonical
from app.scalper.models import ScalperMarketSnapshotRecord as Snapshot, ScalperOptionQuoteRecord as Quote
from app.scalper.research_capture import ResearchSnapshotRepository
from app.scalper.repository import IST, _aware
from tests.test_adaptive_supertrend import START, policy
from tests.test_adaptive_supertrend_paper import raw, capture
from tests.test_scalper import settings


def initialized(engine):
    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(AstCursor(id=1))
    return sessions


def source(tmp_path, days=None, *, per_day=20, price=25000, compressed=True):
    days = days or [START.date() - timedelta(days=3)]
    path = tmp_path / ("history.jsonl.gz" if compressed else "history.jsonl")
    rows = []
    for day in days:
        for index in range(per_day):
            at = datetime.combine(day, time(9, 15), IST) + timedelta(seconds=15 * index)
            observation = raw(0, price + index / 100).model_copy(
                update={
                    "captured_at": at,
                    "request_started_at": at - timedelta(seconds=1),
                    "response_received_at": at,
                    "source_market_timestamp": None,
                }
            )
            value = json.loads(observation.model_dump_json())
            # Deliberately unusable option data: a scalar-only importer must ignore it.
            value["quotes"] = [{"not_an_option_quote": "MUST_NOT_BE_IMPORTED"}]
            rows.append(value)
    payload = "".join(json.dumps(row) + "\n" for row in rows).encode()
    path.write_bytes(gzip.compress(payload, mtime=0) if compressed else payload)
    return path, rows


def load(engine, path, *, before=START.date()):
    return bootstrap.import_history(
        engine, path, source_file_sha256=bootstrap.file_sha256(path), source_mode="ACTS_PAPER", before_date=before
    )


def native(engine, day, count=20, price=26000):
    ids = []
    for index in range(count):
        at = datetime.combine(day, time(9, 15), IST) + timedelta(seconds=15 * index)
        observed = raw(0, price + index / 100).model_copy(
            update={
                "captured_at": at,
                "request_started_at": at - timedelta(seconds=1),
                "response_received_at": at,
            }
        )
        ids.append(capture(engine, observed))
    return ids


def test_scalar_import_provenance_original_timestamps_identity_and_idempotency(engine, tmp_path):
    sessions = initialized(engine)
    path, original = source(tmp_path)
    bytes_before = path.read_bytes()
    result = load(engine, path)
    assert result["rows_read"] == result["rows_inserted"] == 20
    assert result["rows_already_present"] == result["option_quotes_inserted"] == 0
    assert result["mode"] == BOOTSTRAP_MODE and result["source_mode"] == "ACTS_PAPER"
    assert result["distinct_trading_date_count"] == 1
    repeated = load(engine, path)
    assert repeated["rows_inserted"] == 0 and repeated["rows_already_present"] == 20
    assert path.read_bytes() == bytes_before
    with sessions() as session:
        record = session.scalar(select(Snapshot).order_by(Snapshot.captured_at))
        context = json.loads(record.context_json)
        assert context["mode"] == BOOTSTRAP_MODE and context["source_mode"] == "ACTS_PAPER"
        assert context["source_file_sha256"] == bootstrap.file_sha256(path)
        assert context["purpose"] == "AST_FUTURES_HISTORY_BOOTSTRAP"
        assert context["original_capture_timestamp_preserved"] is True
        for name in ("captured_at", "request_started_at", "response_received_at"):
            assert context["original_timestamps"][name] == original[0][name]
            assert _aware(getattr(record, name)) == datetime.fromisoformat(original[0][name])
        assert record.nifty_future == Decimal(str(original[0]["nifty_future"]))
        assert record.future_instrument_id == original[0]["future_instrument_id"]
        assert record.future_expiry == date.fromisoformat(original[0]["future_expiry"])
        assert record.expiry == date.fromisoformat(original[0]["expiry"])
        for model in (Quote, AstTrade, AstEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.get(AstCursor, 1).sequence == 0


def test_bootstrap_never_enters_pending_or_paper_processing(engine, tmp_path, monkeypatch):
    sessions = initialized(engine)
    path, _ = source(tmp_path)
    load(engine, path)
    assert pending_ids(sessions, None) == []
    service = AstPaperService(sessions, settings(ast_config_json=policy().model_dump_json()))
    with sessions() as session:
        first = session.scalar(select(Snapshot.id))

    def forbidden(*args):
        raise AssertionError("Bootstrap cannot even be loaded as an executable snapshot")

    monkeypatch.setattr("app.adaptive_supertrend.service.history", forbidden)
    with pytest.raises(ValueError, match="FOREIGN_CAPTURE_MODE"):
        service.process_snapshot(first)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AstEvent)) == 0
        assert session.scalar(select(func.count()).select_from(AstTrade)) == 0
    assert "AST_BOOTSTRAP" not in str(mode_clause())


def test_history_bootstrap_past_only_native_wins_date_and_keys_do_not_collide(engine, tmp_path):
    sessions = initialized(engine)
    day = START.date() - timedelta(days=3)
    path, _ = source(tmp_path, [day])
    load(engine, path)
    at = START.replace(hour=9, minute=0)
    seeded = history(sessions, at, 1, 45)
    assert seeded[0].valid and seeded[0].open == 25000
    assert seeded.history_provenance["observation_counts"][BOOTSTRAP_MODE] == 20
    # Even a historical processing clock's current date may not use bootstrap.
    same_day = datetime.combine(day, time(9, 20), IST)
    assert not history(sessions, same_day, 1, 45, current_only=True)
    assert not history(sessions, same_day, 1, 45)
    ids = native(engine, day)
    selected = history(sessions, at, 1, 45)
    assert selected[0].valid and selected[0].open == 26000
    assert selected.history_provenance["observation_counts"] == {CAPTURE_MODE: 20, BOOTSTRAP_MODE: 0}
    assert pending_ids(sessions, None) == ids
    with sessions() as session:
        records = list(session.scalars(select(Snapshot)))
        assert len({r.capture_key for r in records}) == 40
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 40


def test_partial_native_date_is_never_silently_repaired_with_bootstrap(engine, tmp_path):
    sessions = initialized(engine)
    day = START.date() - timedelta(days=3)
    path, _ = source(tmp_path, [day])
    load(engine, path)
    native(engine, day, count=1)
    bars = history(sessions, START.replace(hour=9), 1, 45)
    assert not bars[0].valid
    assert bars.history_provenance["observation_counts"] == {CAPTURE_MODE: 1, BOOTSTRAP_MODE: 0}


def test_latest_fifteen_native_sessions_naturally_retire_bootstrap(engine, tmp_path):
    sessions = initialized(engine)
    day = date(2026, 10, 26)
    dates = [day + timedelta(days=i) for i in range(26) if (day + timedelta(days=i)).weekday() < 5]
    bootstrap_days, native_days = dates[:2], dates[2:17]
    path, _ = source(tmp_path, bootstrap_days)
    load(engine, path, before=native_days[-1] + timedelta(days=1))
    for day in native_days[:14]:
        native(engine, day, count=1)
    at = datetime.combine(native_days[-1] + timedelta(days=1), time(9), IST)
    partial = history(sessions, at, 15, 45)
    assert len(partial.history_provenance["selected_sessions"]) == 15
    assert partial.history_provenance["observation_counts"][BOOTSTRAP_MODE] == 20
    native(engine, native_days[-1], count=1)
    retired = history(sessions, at, 15, 45)
    assert len(retired.history_provenance["selected_sessions"]) == 15
    assert retired.history_provenance["observation_counts"] == {CAPTURE_MODE: 15, BOOTSTRAP_MODE: 0}
    assert [p["trading_date"] for p in retired.history_provenance["selected_sessions"]] == list(map(str, native_days))


def test_changed_source_sha_or_stored_provenance_conflicts_fail_closed(engine, tmp_path):
    sessions = initialized(engine)
    path, _ = source(tmp_path, compressed=False)
    load(engine, path)
    original = path.read_bytes()
    path.write_bytes(original.replace(b"25000.0", b"25001.0", 1))
    with pytest.raises(ValueError, match="CONFLICT"):
        load(engine, path)
    path.write_bytes(original)
    with sessions.begin() as session:
        record = session.scalar(select(Snapshot))
        provenance = json.loads(record.context_json)
        provenance["source_mode"] = "RESEARCH_ONLY"
        session.execute(update(Snapshot).where(Snapshot.id == record.id).values(context_json=canonical(provenance)))
    with pytest.raises(ValueError, match="PROVENANCE"):
        load(engine, path)


def test_entire_import_rolls_back_after_a_late_failure(engine, tmp_path, monkeypatch):
    sessions = initialized(engine)
    path, rows = source(tmp_path, compressed=False)
    monkeypatch.setattr(bootstrap, "BATCH_SIZE", 2)
    rows[-1]["request_started_at"] = rows[-1]["captured_at"]
    rows[-1]["response_received_at"] = rows[0]["captured_at"]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(ValueError, match="CAUSAL"):
        load(engine, path)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 0
        assert session.scalar(select(func.count()).select_from(AstEvent)) == 0


def test_importer_refuses_foreign_database_and_requires_original_timing(engine, tmp_path):
    initialized(engine)
    path, rows = source(tmp_path, compressed=False)
    for name in ("request_started_at", "response_received_at"):
        missing = dict(rows[0])
        del missing[name]
        path.write_text(json.dumps(missing) + "\n")
        with pytest.raises(ValueError, match="ORIGINAL_SNAPSHOT_SCALARS"):
            load(engine, path)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    path.write_text(json.dumps({**rows[0], "context_json": {"mode": "RESEARCH_ONLY"}}) + "\n")
    with pytest.raises(ValueError, match="CONFLICTING_SOURCE_MODE"):
        load(engine, path)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    ResearchSnapshotRepository(engine, mode="ACTS_PAPER").save(raw(0))
    with pytest.raises(ValueError, match="FOREIGN_CAPTURE_DATA"):
        load(engine, path)


def test_utc_timestamps_preserve_original_instant_and_text(engine, tmp_path):
    sessions = initialized(engine)
    path, rows = source(tmp_path, compressed=False)
    for row in rows:
        for name in ("captured_at", "request_started_at", "response_received_at"):
            from datetime import timezone

            row[name] = datetime.fromisoformat(row[name]).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    load(engine, path)
    with sessions() as session:
        first = session.scalar(select(Snapshot).order_by(Snapshot.captured_at))
        assert _aware(first.captured_at) == datetime.fromisoformat(rows[0]["captured_at"])
        assert json.loads(first.context_json)["original_timestamps"]["captured_at"] == rows[0]["captured_at"]
    assert history(sessions, START.replace(hour=9), 1, 45)[0].valid


def test_freeze_records_history_provenance_without_changing_optimizer_math(engine, tmp_path):
    sessions = initialized(engine)
    day = START.date() - timedelta(days=3)
    path, _ = source(tmp_path, [day], per_day=1500)
    load(engine, path)
    observed = history(sessions, START.replace(hour=9, minute=0), 1, 45)
    assert all(bar.valid for bar in observed)
    # The unchanged full-grid optimizer is exercised, not a static fallback.
    document = freeze(sessions, observed, START.date(), START.replace(hour=9, minute=0), policy())
    assert document["history_provenance"] == observed.history_provenance
    assert document["history_provenance"]["observation_counts"][BOOTSTRAP_MODE] == 1500
    with sessions() as session:
        run = json.loads(session.get(OptimizerRun, document["optimizer_run_id"]).document)
        assert run["grid_count"] == 1886
        assert run["history_provenance"] == observed.history_provenance
        assert run["policy_hash"] == policy().policy_hash


def test_cli_has_no_broker_settings_and_requires_explicit_isolated_target(engine, tmp_path, monkeypatch, capsys):
    initialized(engine)
    path, _ = source(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("No BaseSettings or broker credential loading")

    monkeypatch.setattr(BaseSettings, "__init__", forbidden)
    monkeypatch.delenv("KOTAK_CONSUMER_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", str(engine.url))
    args = [
        "--snapshots",
        str(path),
        "--source-file-sha256",
        bootstrap.file_sha256(path),
        "--source-mode",
        "ACTS_PAPER",
        "--before-date",
        str(START.date()),
        "--confirm-isolated-ast-database",
    ]
    assert bootstrap.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["rows_inserted"] == 20 and output["option_quotes_inserted"] == 0
    assert bootstrap.main(args) == 0
    assert json.loads(capsys.readouterr().out)["rows_already_present"] == 20
    with pytest.raises(SystemExit):
        bootstrap.main(args[:-1])
    implementation = inspect.getsource(bootstrap)
    assert "app.acts_paper" not in implementation and "app.broker" not in implementation
    assert "Settings(" not in implementation and "get_settings(" not in implementation
