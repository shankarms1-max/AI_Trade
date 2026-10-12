"""External Alpha is read-only evidence; ACTS remains the sole interpreter."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import json
import os
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import make_url

from app.alpha.models import AlphaHypothesis, AlphaValidity, CalculationMode
from app.db.models import AlphaFeatureSnapshotRecord, MarketSnapshotRecord
from app.scalper.config import ScalperConfig
from app.scalper.models import ScalperMarketSnapshotRecord, ScalperOptionQuoteRecord
from app.scalper.paper import cost_schedule
from app.scalper_lab import STRATEGY_IDS, acts
from app.scalper_lab.alpha_bridge import _match, _parse, load_external_alpha
from app.scalper_lab.engine import ScalperLab
from app.scalper_lab.io import load_local_sqlite
from tests.test_scalper_acts import alpha_for
from tests.test_scalper_lab import observed
from tests.test_scalper import settings

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.run_scalper_lab import main  # noqa: E402


def _alpha(row, seconds=60, **updates):
    at = row.snapshot.captured_at - timedelta(seconds=seconds)
    return alpha_for(row, "BEAR").model_copy(update={
        "timestamp": at, "feature_calculated_at": at,
        "response_received_at": at, "source_market_timestamp": at,
        "reference_source_timestamp": at, **updates})


def _record(alpha, identity=1, **updates):
    values = dict(id=identity, timestamp=alpha.timestamp, expiry=alpha.expiry,
                  alpha_version=alpha.alpha_version,
                  calculation_mode=alpha.calculation_mode.value,
                  price_source=alpha.price_source,
                  result_json=alpha.model_dump(mode="json"))
    values.update(updates)
    return SimpleNamespace(**values)


def test_latest_eligible_alpha_uses_timestamp_then_id():
    row = observed(3)[-1]
    older = _alpha(row, seconds=120)
    latest = _alpha(row, seconds=60)
    future_calculated = _alpha(row, seconds=30,
                               feature_calculated_at=row.snapshot.captured_at + timedelta(seconds=1))
    rows = [_parse(_record(older, 1)), _parse(_record(latest, 2)),
            _parse(_record(latest, 3)), _parse(_record(future_calculated, 4))]
    matched, report = _match([row], rows)
    assert matched[0].alpha == latest
    assert report["matched_observations"] == 1
    assert report["alpha_rows_loaded"] == 4
    # Same-timestamp id 3 beats id 2; the later noncausal row is bypassed.
    different = latest.model_copy(update={"alpha_1": .1})
    matched, _ = _match([row], [*rows[:2], _parse(_record(different, 3)), rows[3]])
    assert matched[0].alpha == different


@pytest.mark.parametrize("update,expected", [
    ({"reference_instrument_id": "OTHER"}, "FUTURES_IDENTITY_MISMATCH"),
    ({"reference_expiry": None}, "ALPHA_IDENTITY_INCOMPLETE"),
    ({"reference_instrument_id": None}, "ALPHA_IDENTITY_INCOMPLETE"),
    ({"expiry": None}, "ALPHA_IDENTITY_INCOMPLETE"),
    ({"validity_state": AlphaValidity.INVALID}, "INVALID_ALPHA"),
    ({"hypothesis_type": AlphaHypothesis.REVERSAL}, "INVALID_ALPHA"),
    ({"calculation_mode": CalculationMode.HISTORICAL_REPLAY}, "INVALID_ALPHA"),
    ({"alpha_version": "phase14_v1"}, "INVALID_ALPHA"),
    ({"price_source": "SPOT"}, "INVALID_ALPHA"),
    ({"feature_calculated_at": None}, "INVALID_ALPHA"),
])
def test_fail_closed_intrinsic_alpha_gates(update, expected):
    row = observed(3)[-1]
    alpha = _alpha(row, **update)
    candidate = _parse(_record(alpha))
    matched, report = _match([row], [candidate])
    assert matched[0].alpha is None
    assert report["unmatched_reason_split"] == {expected: 1}


@pytest.mark.parametrize("field,reason", [
    ("reference_expiry", "FUTURES_EXPIRY_MISMATCH"),
    ("expiry", "OPTION_EXPIRY_MISMATCH"),
])
def test_expiry_mismatch_is_not_matched(field, reason):
    row = observed(3)[-1]
    alpha = _alpha(row)
    changed = alpha.model_copy(update={field: alpha.expiry + timedelta(days=7)})
    matched, report = _match([row], [_parse(_record(changed))])
    assert matched[0].alpha is None
    assert report["unmatched_reason_split"] == {reason: 1}


@pytest.mark.parametrize("update,seconds,reason", [
    ({}, -1, "FUTURE_ALPHA_REJECTED"),
    ({}, 181, "STALE_ALPHA"),
    ({"feature_calculated_at": "FUTURE"}, 60, "FUTURE_ALPHA_REJECTED"),
    ({"response_received_at": "FUTURE"}, 60, "FUTURE_ALPHA_REJECTED"),
    ({"source_market_timestamp": "FUTURE"}, 60, "FUTURE_ALPHA_REJECTED"),
    ({"reference_source_timestamp": "FUTURE"}, 60, "FUTURE_ALPHA_REJECTED"),
])
def test_time_gates_never_read_future(update, seconds, reason):
    row = observed(3)[-1]
    values = {key: row.snapshot.captured_at + timedelta(seconds=1)
              for key in update}
    alpha = _alpha(row, seconds=seconds, **values)
    matched, report = _match([row], [_parse(_record(alpha))])
    assert matched[0].alpha is None
    assert report["unmatched_reason_split"] == {reason: 1}


def test_metadata_disagreement_and_corrupt_payload_fail_closed():
    row = observed(3)[-1]
    alpha = _alpha(row)
    for record in (_record(alpha, alpha_version="phase14_v1"),
                   _record(alpha, result_json={"not": "alpha"})):
        matched, report = _match([row], [_parse(record)])
        assert matched[0].alpha is None
        assert report["invalid_alpha_count"] == 1


def test_multi_day_matching_is_deterministic_and_bounded():
    row = observed(3)[-1]
    next_day = replace(row, snapshot=row.snapshot.model_copy(update={
        "captured_at": row.snapshot.captured_at + timedelta(days=1)}), snapshot_id=999)
    rows = [row, next_day]
    alphas = [_parse(_record(_alpha(item), identity=index))
              for index, item in enumerate(rows, 1)]
    first, report = _match(rows, alphas)
    second, duplicate_report = _match(rows, list(reversed(alphas)))
    assert first == second and report == duplicate_report
    assert report["matched_observations"] == 2


def test_empty_external_source_is_alpha_unavailable():
    row = observed(2)[0]
    matched, report = _match([row], [])
    assert matched[0].alpha is None
    assert report["alpha_source"] == "EXTERNAL_DB"
    assert report["unmatched_reason_split"] == {"ALPHA_UNAVAILABLE": 1}


def test_failed_alpha_connection_never_prints_credentials():
    row = observed(2)[0]
    secret = "do-not-print-this-password"
    url = f"postgresql+psycopg://reader:{secret}@127.0.0.1:1/alpha"
    with pytest.raises(RuntimeError, match="LAB_ALPHA_SOURCE_READ_FAILED") as error:
        load_external_alpha([row], url)
    assert secret not in str(error.value)


def test_external_alpha_does_not_change_other_four_strategies():
    rows = observed(30)
    lab_settings = settings()
    lab = ScalperLab(ScalperConfig.from_settings(lab_settings),
                     cost_schedule(lab_settings))
    baseline = lab.run(rows)
    enriched = lab.run([replace(row, alpha=_alpha(row)) for row in rows])
    for strategy in STRATEGY_IDS:
        if strategy == acts.STRATEGY_ID:
            continue
        assert baseline.summaries[strategy] == enriched.summaries[strategy]
        assert [row for row in baseline.decisions if row["strategy_id"] == strategy] == [
            row for row in enriched.decisions if row["strategy_id"] == strategy]
        assert [row for row in baseline.trades if row["strategy_id"] == strategy] == [
            row for row in enriched.trades if row["strategy_id"] == strategy]


def test_no_external_source_keeps_existing_sqlite_loader(engine):
    row = observed(2)[0]
    with engine.begin() as connection:
        connection.execute(insert(ScalperMarketSnapshotRecord), [{
            **row.snapshot.model_dump(exclude={"quotes"}), "id": row.snapshot_id,
            "capture_key": "alpha-bridge-default", "feature_json": "{}",
            "signal_json": "{}"}])
        connection.execute(insert(ScalperOptionQuoteRecord), [
            {**quote.model_dump(), "scalper_snapshot_id": row.snapshot_id}
            for quote in row.snapshot.quotes])
    day = row.snapshot.captured_at.date()
    loaded = load_local_sqlite(Path(engine.url.database), day, day)
    assert len(loaded) == 1 and loaded[0].alpha is None
    assert acts.alpha_evidence(None, row.snapshot.captured_at,
                               row.snapshot.future_instrument_id,
                               row.snapshot.future_expiry,
                               row.snapshot.expiry)["status"] == "ALPHA_UNAVAILABLE"


def test_cli_alpha_url_precedence_and_no_secret_output(engine, tmp_path, monkeypatch,
                                                       capsys):
    row = observed(2)[0]
    with engine.begin() as connection:
        connection.execute(insert(ScalperMarketSnapshotRecord), [{
            **row.snapshot.model_dump(exclude={"quotes"}), "id": row.snapshot_id,
            "capture_key": "alpha-bridge-cli", "feature_json": "{}",
            "signal_json": "{}"}])
        connection.execute(insert(ScalperOptionQuoteRecord), [
            {**quote.model_dump(), "scalper_snapshot_id": row.snapshot_id}
            for quote in row.snapshot.quotes])
    seen = []

    def fake_external(rows, url):
        seen.append(url)
        return rows, {"alpha_source": "EXTERNAL_DB", "alpha_rows_loaded": 0,
                      "alpha_rows_valid": 0, "matched_observations": 0,
                      "unmatched_observations": len(rows)}
    monkeypatch.setattr("scripts.run_scalper_lab.load_external_alpha", fake_external)
    monkeypatch.setenv("ALPHA_RESEARCH_DATABASE_URL", "postgresql://env:secret-env@host/db")
    day = row.snapshot.captured_at.date().isoformat()
    base = ["--database", str(engine.url.database), "--start", day, "--end", day]
    assert main(base + ["--alpha-database-url", "postgresql://cli:secret-cli@host/db",
                        "--output", str(tmp_path / "cli")]) == 0
    assert main(base + ["--output", str(tmp_path / "env")]) == 0
    monkeypatch.delenv("ALPHA_RESEARCH_DATABASE_URL")
    assert main(base + ["--output", str(tmp_path / "default")]) == 0
    assert seen == ["postgresql://cli:secret-cli@host/db",
                    "postgresql://env:secret-env@host/db"]
    output = capsys.readouterr().out
    assert "secret-cli" not in output and "secret-env" not in output
    for directory in (tmp_path / "cli", tmp_path / "env"):
        assert all("secret-" not in path.read_text()
                   for path in (directory / "direct").iterdir()
                   if path.is_file())
        assert json.loads((directory / "direct" / "alpha_bridge.json").read_text())[
            "alpha_source"] == "EXTERNAL_DB"
        acts_summary = json.loads((directory / "direct" / "summary.json").read_text())[
            "ACTS_V1"]
        assert acts_summary["alpha_unavailable_count"] == 1
    default = tmp_path / "default" / "direct"
    assert not (default / "alpha_bridge.json").exists()
    assert json.loads((default / "summary.json").read_text())["ACTS_V1"][
        "alpha_classification_split"] == {"ALPHA_UNAVAILABLE": 1}


@pytest.mark.skipif(not os.getenv("PHASE15_POSTGRES_TEST_URL"),
                    reason="PHASE15_POSTGRES_TEST_URL is required")
def test_two_postgresql_databases_are_read_only(monkeypatch):
    base = make_url(os.environ["PHASE15_POSTGRES_TEST_URL"])
    admin = create_engine(base)
    names = [f"lab_p15_{uuid4().hex}", f"lab_alpha_{uuid4().hex}"]
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            for name in names:
                connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        phase15 = create_engine(base.set(database=names[0]))
        alpha_db = create_engine(base.set(database=names[1]))
        try:
            ScalperMarketSnapshotRecord.__table__.create(phase15)
            ScalperOptionQuoteRecord.__table__.create(phase15)
            MarketSnapshotRecord.__table__.create(alpha_db)
            AlphaFeatureSnapshotRecord.__table__.create(alpha_db)
            row = observed(3)[-1]
            alpha = _alpha(row)
            with phase15.begin() as connection:
                connection.execute(insert(ScalperMarketSnapshotRecord), [{
                    **row.snapshot.model_dump(exclude={"quotes"}), "id": row.snapshot_id,
                    "capture_key": "external-alpha-test", "feature_json": "{}",
                    "signal_json": "{}"}])
                connection.execute(insert(ScalperOptionQuoteRecord), [
                    {**quote.model_dump(), "scalper_snapshot_id": row.snapshot_id}
                    for quote in row.snapshot.quotes])
            with alpha_db.begin() as connection:
                connection.execute(insert(MarketSnapshotRecord), [{
                    "id": 1, "timestamp_ist": alpha.timestamp,
                    "collection_bucket_ist": alpha.timestamp,
                    "nifty_spot": row.snapshot.nifty_spot,
                    "atm_strike": row.snapshot.atm_strike,
                    "expiry": row.snapshot.expiry, "source": "TEST"}])
                connection.execute(insert(AlphaFeatureSnapshotRecord), [{
                    "id": 1, "market_snapshot_id": 1,
                    "alpha_version": alpha.alpha_version,
                    "calculation_mode": alpha.calculation_mode.value,
                    "timestamp": alpha.timestamp, "expiry": alpha.expiry,
                    "price_source": alpha.price_source,
                    "alpha_1_direction": alpha.alpha_1_direction.value,
                    "alpha_2_direction": alpha.alpha_2_direction.value,
                    "joint_alpha_direction": alpha.joint_alpha_direction.value,
                    "consecutive_confirmation_count": 1,
                    "evidence_quality": alpha.evidence_quality.value,
                    "warnings_json": [],
                    "result_json": alpha.model_dump(mode="json")}])
            phase15_url = base.set(database=names[0]).render_as_string(hide_password=False)
            alpha_url = base.set(database=names[1]).render_as_string(hide_password=False)
            day = row.snapshot.captured_at.date()
            from app.scalper_lab.io import load_postgresql
            loaded = load_postgresql(phase15_url, day, day)
            assert loaded[0].alpha is None
            bridged, report = load_external_alpha(loaded, alpha_url)
            assert bridged[0].alpha == alpha
            assert report["matched_observations"] == report["alpha_rows_valid"] == 1
            with alpha_db.connect() as connection:
                original = connection.scalar(select(AlphaFeatureSnapshotRecord.result_json))

            def forbidden_write(connection, *_):
                connection.execute(text("UPDATE alpha_feature_snapshots SET price_source='SPOT'"))
            with monkeypatch.context() as patch:
                patch.setattr("app.scalper_lab.alpha_bridge._read_rows", forbidden_write)
                with pytest.raises(RuntimeError, match="LAB_ALPHA_SOURCE_READ_FAILED"):
                    load_external_alpha(loaded, alpha_url)
            with alpha_db.connect() as connection:
                assert connection.scalar(select(AlphaFeatureSnapshotRecord.result_json)) == original
                assert connection.scalar(select(AlphaFeatureSnapshotRecord.price_source)) == "FUTURE"
        finally:
            phase15.dispose()
            alpha_db.dispose()
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            for name in names:
                connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
        admin.dispose()
