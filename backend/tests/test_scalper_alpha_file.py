"""CSV Alpha uses the same causal parser and matcher as the DB bridge."""
from datetime import timedelta
from pathlib import Path
import csv
import json
import sys

import pytest
from sqlalchemy import insert

from app.alpha.models import AlphaValidity, CalculationMode
from app.scalper.config import ScalperConfig
from app.scalper.models import ScalperMarketSnapshotRecord, ScalperOptionQuoteRecord
from app.scalper.paper import cost_schedule
from app.scalper_lab import STRATEGY_IDS, acts
from app.scalper_lab.alpha_bridge import load_alpha_file
from app.scalper_lab.engine import ScalperLab
from tests.test_scalper import settings
from tests.test_scalper_alpha_bridge import _alpha, _record
from tests.test_scalper_lab import observed

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.run_scalper_lab import main  # noqa: E402

FIELDS = ("id", "timestamp", "expiry", "alpha_version",
          "calculation_mode", "price_source", "result_json")


def _csv(path, records):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({"id": record.id,
                             "timestamp": record.timestamp.isoformat(),
                             "expiry": record.expiry.isoformat(),
                             "alpha_version": record.alpha_version,
                             "calculation_mode": record.calculation_mode,
                             "price_source": record.price_source,
                             "result_json": json.dumps(record.result_json)})
    return path


def test_valid_csv_alpha_matches_and_reports_file(tmp_path):
    observation = observed(3)[-1]
    alpha = _alpha(observation)
    path = _csv(tmp_path / "alpha.csv", [_record(alpha)])
    matched, report = load_alpha_file([observation], path)
    assert matched[0].alpha == alpha
    assert report["alpha_source"] == "FILE"
    assert report["alpha_rows_loaded"] == report["alpha_rows_valid"] == 1
    assert report["matched_observations"] == 1
    assert report["unmatched_observations"] == 0
    assert acts.alpha_evidence(matched[0].alpha, observation.snapshot.captured_at,
                               observation.snapshot.future_instrument_id,
                               observation.snapshot.future_expiry,
                               observation.snapshot.expiry)["status"] == "AVAILABLE"
    empty, empty_report = load_alpha_file([], path)
    assert empty == [] and empty_report["alpha_rows_loaded"] == 1


@pytest.mark.parametrize("update,seconds,reason", [
    ({}, 181, "STALE_ALPHA"),
    ({}, -1, "FUTURE_ALPHA_REJECTED"),
    ({"feature_calculated_at": "FUTURE"}, 60, "FUTURE_ALPHA_REJECTED"),
    ({"reference_instrument_id": "OTHER"}, 60, "FUTURES_IDENTITY_MISMATCH"),
    ({"reference_expiry": "OTHER"}, 60, "FUTURES_EXPIRY_MISMATCH"),
    ({"expiry": "OTHER"}, 60, "OPTION_EXPIRY_MISMATCH"),
    ({"validity_state": AlphaValidity.INVALID}, 60, "INVALID_ALPHA"),
    ({"calculation_mode": CalculationMode.HISTORICAL_REPLAY}, 60, "INVALID_ALPHA"),
    ({"alpha_version": "phase14_v1"}, 60, "INVALID_ALPHA"),
    ({"price_source": "SPOT"}, 60, "INVALID_ALPHA"),
])
def test_csv_rejects_noncausal_or_invalid_alpha(tmp_path, update, seconds, reason):
    observation = observed(3)[-1]
    changes = {key: (observation.snapshot.captured_at + timedelta(seconds=1)
                     if value == "FUTURE" else observation.snapshot.expiry
                     + timedelta(days=7) if value == "OTHER" and key.endswith("expiry")
                     else value)
               for key, value in update.items()}
    alpha = _alpha(observation, seconds=seconds, **changes)
    path = _csv(tmp_path / "alpha.csv", [_record(alpha)])
    matched, report = load_alpha_file([observation], path)
    assert matched[0].alpha is None
    assert report["matched_observations"] == 0
    assert report["unmatched_observations"] == 1
    assert report["unmatched_reason_split"] == {reason: 1}


def test_csv_latest_eligible_selection_is_deterministic(tmp_path):
    observation = observed(3)[-1]
    older = _alpha(observation, seconds=120)
    latest = _alpha(observation, seconds=60)
    chosen = latest.model_copy(update={"alpha_1": .1})
    future = _alpha(observation, seconds=-1)
    records = [_record(future, 4), _record(older, 1),
               _record(chosen, 3), _record(latest, 2)]
    path = _csv(tmp_path / "alpha.csv", records)
    first, report = load_alpha_file([observation], path)
    _csv(path, list(reversed(records)))
    second, reverse_report = load_alpha_file([observation], path)
    assert first == second and report == reverse_report
    assert first[0].alpha == chosen


def test_file_precedes_external_database_and_keeps_other_strategies(
        engine, tmp_path, monkeypatch):
    observation = observed(3)[-1]
    alpha = _alpha(observation)
    path = _csv(tmp_path / "alpha.csv", [_record(alpha)])
    with engine.begin() as connection:
        connection.execute(insert(ScalperMarketSnapshotRecord), [{
            **observation.snapshot.model_dump(exclude={"quotes"}),
            "id": observation.snapshot_id, "capture_key": "alpha-file-test",
            "feature_json": "{}", "signal_json": "{}"}])
        connection.execute(insert(ScalperOptionQuoteRecord), [
            {**quote.model_dump(), "scalper_snapshot_id": observation.snapshot_id}
            for quote in observation.snapshot.quotes])

    def forbidden(*_args):
        raise AssertionError("external Alpha DB should not be called")

    monkeypatch.setattr("scripts.run_scalper_lab.load_external_alpha", forbidden)
    monkeypatch.setenv("ALPHA_RESEARCH_DATABASE_URL", "postgresql://unused:secret@host/db")
    day = observation.snapshot.captured_at.date().isoformat()
    assert main(["--database", str(engine.url.database), "--start", day,
                 "--end", day, "--alpha-file", str(path),
                 "--alpha-database-url", "postgresql://unused:secret@host/db",
                 "--output", str(tmp_path / "result")]) == 0
    artifact = tmp_path / "result" / "direct"
    assert json.loads((artifact / "alpha_bridge.json").read_text())[
        "alpha_source"] == "FILE"
    assert json.loads((artifact / "summary.json").read_text())[
        "ACTS_V1"]["alpha_unavailable_count"] == 0
    assert "secret" not in (artifact / "manifest.json").read_text()

    rows = observed(30)
    lab_settings = settings()
    lab = ScalperLab(ScalperConfig.from_settings(lab_settings),
                     cost_schedule(lab_settings))
    baseline = lab.run(rows)
    records = [_record(_alpha(row), index)
               for index, row in enumerate(rows, 1)]
    enriched, _ = load_alpha_file(rows, _csv(tmp_path / "many.csv", records))
    with_alpha = lab.run(enriched)
    for strategy in STRATEGY_IDS:
        if strategy == acts.STRATEGY_ID:
            continue
        assert baseline.summaries[strategy] == with_alpha.summaries[strategy]
        assert [item for item in baseline.decisions if item["strategy_id"] == strategy] == [
            item for item in with_alpha.decisions if item["strategy_id"] == strategy]
        assert [item for item in baseline.trades if item["strategy_id"] == strategy] == [
            item for item in with_alpha.trades if item["strategy_id"] == strategy]
