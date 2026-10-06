"""Forward rehearsal safety and read-only audit of sealed replay evidence."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.pipeline.models import PipelineStatus, StepStatus
from app.pipeline.service import build_pipeline_orchestrator
from app.research.manifest import canonical
from app.shadow.repository import ShadowRepository
from tests.test_phase14_2_1_integrity import context, execute, replay_fixture  # noqa: F401


def test_decision_only_runs_analysis_without_any_legacy_fill_path(
    repository, session_factory, market_snapshot, monkeypatch
):
    snapshot_id = repository.save_market_snapshot(market_snapshot, market_snapshot.timestamp_ist).snapshot_id
    def forbidden(*args, **kwargs):
        pytest.fail("decision-only must never enter legacy shadow execution")
    monkeypatch.setattr("app.pipeline.service.build_shadow_entry", forbidden)
    monkeypatch.setattr("app.pipeline.service.update_open_trades", forbidden)
    monkeypatch.setattr(ShadowRepository, "create_trade", forbidden)
    monkeypatch.setattr(ShadowRepository, "save_update", forbidden)
    settings = Settings(kotak_consumer_key="TEST", pipeline_decision_only=True, _env_file=None)
    engine = build_pipeline_orchestrator(session_factory, settings)
    result = engine.run(snapshot_id)
    assert result.status == PipelineStatus.SUCCESS
    assert result.feature_status == result.regime_status == result.strategy_status == result.risk_status == StepStatus.SUCCESS
    assert result.shadow_trade_id is None
    # Even an approved risk decision cannot cause a fill in this mode.
    outcome = engine._steps.shadow_entry(snapshot_id)
    assert not outcome.created and outcome.reason_codes == ["DECISION_ONLY_NO_EXECUTION"]


def test_decision_only_refuses_to_abandon_open_shadow_exposure(
    repository, session_factory, market_snapshot, monkeypatch
):
    snapshot_id = repository.save_market_snapshot(market_snapshot, market_snapshot.timestamp_ist).snapshot_id
    monkeypatch.setattr(ShadowRepository, "has_open_trade", lambda _: True)
    settings = Settings(kotak_consumer_key="TEST", pipeline_decision_only=True, _env_file=None)
    result = build_pipeline_orchestrator(session_factory, settings).run(snapshot_id)
    assert result.status == PipelineStatus.FAILED
    assert result.feature_status == StepStatus.SKIPPED
    assert result.shadow_trade_id is None


def test_integrity_flag_cannot_silently_select_legacy_forward_execution(session_factory):
    settings = Settings(kotak_consumer_key="TEST", phase14_2_1_replay_integrity_enabled=True, _env_file=None)
    with pytest.raises(ValueError, match="INTEGRITY_REPLAY_IS_OFFLINE_ONLY"):
        build_pipeline_orchestrator(session_factory, settings)


def test_new_mode_does_not_enable_production_flags(monkeypatch):
    # Alembic's existing test setup can load .env into this test process.
    # Check defaults independently of those explicitly configured values.
    for name in ("PIPELINE_AFTER_SNAPSHOT", "PHASE14_2_STRATEGY_LOGIC_ENABLED",
                 "PHASE14_2_1_REPLAY_INTEGRITY_ENABLED", "ALPHA_ENGINE_ENABLED",
                 "REGIME_USE_STATISTICAL_ALPHA", "REPLAY_ALLOW_UNKNOWN_DEPTH", "REPLAY_ALLOW_0DTE"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(kotak_consumer_key="TEST", pipeline_decision_only=True, _env_file=None)
    assert not settings.pipeline_after_snapshot
    assert not settings.phase14_2_strategy_logic_enabled
    assert not settings.phase14_2_1_replay_integrity_enabled
    assert not settings.alpha_engine_enabled and not settings.regime_use_statistical_alpha
    assert not settings.replay_allow_unknown_depth and not settings.replay_allow_0dte


@pytest.fixture
def audit_module():
    path = Path(__file__).resolve().parents[2] / "scripts/audit_integrity_run.py"
    spec = importlib.util.spec_from_file_location("read_only_integrity_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dataset(path, rows):
    path.write_text(canonical([{"snapshot_id": row.snapshot_id, "snapshot": row.snapshot,
                               "feature_id": row.feature_id, "feature": row.feature,
                               "alpha": row.alpha} for row in rows]), encoding="utf-8")


def test_audit_verifies_ledger_without_modification(context, tmp_path, audit_module):
    rows, configs, _ = replay_fixture(context)
    target = tmp_path / "dataset.json"
    dataset(target, rows)
    rows = audit_module.load_dataset(target)
    result, ledger = execute(tmp_path / "evidence", rows, configs)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = audit_module.audit_run(tmp_path / "evidence", ledger.manifest.run_id, target)
    assert report["ledger_verified"] and report["dataset_verified"]
    assert report["coverage"]["path_coverage_ratio"] == 1
    assert report["denominators"] == result["denominators"]
    assert report["profitability_claim"] is False
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("tamper", ["ledger", "dataset"])
def test_audit_rejects_tampering(context, tmp_path, audit_module, tamper):
    rows, configs, _ = replay_fixture(context)
    target = tmp_path / "dataset.json"
    dataset(target, rows)
    rows = audit_module.load_dataset(target)
    _, ledger = execute(tmp_path / "evidence", rows, configs)
    if tamper == "ledger":
        event = sorted((ledger.directory / "events").glob("*.json"))[0]
        value = json.loads(event.read_text())
        value["event_type"] = "ALTERED"
        event.write_text(json.dumps(value))
    else:
        value = json.loads(target.read_text())
        value[0]["snapshot"]["nifty_spot"] += 1
        target.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        audit_module.audit_run(tmp_path / "evidence", ledger.manifest.run_id, target)


def test_zero_dte_remains_the_terminal_reason_without_fill_attempts(context, tmp_path):
    rows, configs, _ = replay_fixture(context)
    expiry = rows[0].snapshot.timestamp_ist.date()
    rows = [replace(row, snapshot=row.snapshot.model_copy(update={"expiry": expiry,
        "options": [item.model_copy(update={"expiry": expiry}) for item in row.snapshot.options]}))
        for row in rows]
    result, ledger = execute(tmp_path, rows, configs)
    decisions = [event["decision"] for event in ledger.events() if event["event_type"] == "DECISION_ATTEMPT"]
    assert decisions and {item["missing_reason"] for item in decisions} == {"ZERO_DTE_EXCLUDED"}
    assert result["denominators"]["fill_attempts"] == 0
    assert result["profitability_claim"] is False
    with pytest.raises(ValueError, match="0DTE is excluded"):
        replace(configs[-1], allow_0dte=True)
