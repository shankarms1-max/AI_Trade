from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.pipeline import get_pipeline_repository
from app.collector.service import CollectorService, TradingCalendar
from app.core.config import get_settings
from app.db.models import (
    MarketFeatureSnapshotRecord,
    MarketRegimeSnapshotRecord,
    MarketSnapshotRecord,
    PipelineRunRecord,
    RiskDecisionRecord,
    ShadowTradeMarkRecord,
    ShadowTradeRecord,
    StrategyCandidateSetRecord,
)
from app.main import app
from app.pipeline.models import PipelineStatus, StepStatus
from app.pipeline.orchestrator import PipelineSteps, ResearchPipelineOrchestrator
from app.pipeline.repository import PipelineRepository
from app.pipeline.service import build_pipeline_orchestrator, shadow_risk_config_from_settings
from app.risk.engine import evaluate_risk
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.risk.exposure import ResearchRiskStateProvider, RiskState
from app.risk.models import EvaluationContext
from app.shadow.repository import ShadowRepository
from app.shadow.risk_state import ShadowRiskStateProvider
from app.shadow.service import build_shadow_entry, config_from_settings as shadow_config
from tests.test_phase2_persistence import save
from tests.test_phase7_risk import approved_config, persisted_context, risk_context


def fake_steps(events, *, fail=None, ai_fail=False, with_alpha=False):
    def call(name, result=None):
        def inner(snapshot_id):
            events.append(name)
            if fail == name or (name == "ai" and ai_fail):
                raise RuntimeError(f"{name} secret detail")
            return result
        return inner

    return PipelineSteps(
        shadow_update=call("shadow_update"),
        features=call("features"),
        alpha=call("alpha") if with_alpha else None,
        regime=call("regime"),
        ai=call("ai"),
        strategy=call("strategy"),
        risk=call("risk", SimpleNamespace(approved_count=0)),
        shadow_entry=call("shadow_entry", SimpleNamespace(created=False, trade=None)),
    )


def test_pipeline_strict_order_default_ai_skip(repository, session_factory, market_snapshot):
    snapshot_id = save(repository, market_snapshot).snapshot_id
    events = []
    result = ResearchPipelineOrchestrator(
        PipelineRepository(session_factory), fake_steps(events)
    ).run(snapshot_id)
    assert events == ["shadow_update", "features", "regime", "strategy", "risk", "shadow_entry"]
    assert result.status == PipelineStatus.SUCCESS
    assert result.ai_status == StepStatus.SKIPPED


def test_ai_failure_is_nonblocking(repository, session_factory, market_snapshot):
    snapshot_id = save(repository, market_snapshot).snapshot_id
    events = []
    result = ResearchPipelineOrchestrator(
        PipelineRepository(session_factory), fake_steps(events, ai_fail=True)
    ).run(snapshot_id, run_ai=True)
    assert events == ["shadow_update", "features", "regime", "ai", "strategy", "risk", "shadow_entry"]
    assert result.status == PipelineStatus.SUCCESS
    assert result.ai_status == StepStatus.FAILED
    assert result.safe_error_message is None


def test_phase14_alpha_runs_after_features_before_regime(
    repository, session_factory, market_snapshot
):
    snapshot_id = save(repository, market_snapshot).snapshot_id
    events = []
    result = ResearchPipelineOrchestrator(
        PipelineRepository(session_factory), fake_steps(events, with_alpha=True)
    ).run(snapshot_id)
    assert events == [
        "shadow_update", "features", "alpha", "regime", "strategy", "risk", "shadow_entry"
    ]
    assert result.alpha_status == StepStatus.SUCCESS


def test_strategy_failure_records_partial_and_skips_risk(repository, session_factory, market_snapshot):
    snapshot_id = save(repository, market_snapshot).snapshot_id
    events = []
    result = ResearchPipelineOrchestrator(
        PipelineRepository(session_factory), fake_steps(events, fail="strategy")
    ).run(snapshot_id)
    assert result.status == PipelineStatus.PARTIAL
    assert result.strategy_status == StepStatus.FAILED
    assert result.risk_status == StepStatus.SKIPPED
    assert "risk" not in events and "shadow_entry" not in events
    assert "secret detail" not in (result.safe_error_message or "")
    with session_factory() as session:
        assert session.get(MarketSnapshotRecord, snapshot_id) is not None


def test_real_pipeline_is_idempotent_and_snapshot_one_has_no_shadow_trade(
    repository, session_factory, market_snapshot
):
    snapshot_id = save(repository, market_snapshot).snapshot_id
    settings = get_settings().model_copy(update={
        "shadow_risk_capital_base": 1_000_000,
        "shadow_risk_max_loss_per_trade": 100_000,
        "shadow_risk_max_capital_per_trade": 100_000,
        "shadow_risk_max_daily_loss": 100_000,
    })
    orchestrator = build_pipeline_orchestrator(session_factory, settings)
    first = orchestrator.run(snapshot_id)
    second = orchestrator.run(snapshot_id)
    assert first.status == second.status == PipelineStatus.SUCCESS
    assert first.shadow_trade_id is None and second.shadow_trade_id is None
    with session_factory() as session:
        assert session.scalar(select(func.count(PipelineRunRecord.id))) == 1
        assert session.scalar(select(func.count(MarketFeatureSnapshotRecord.id))) == 1
        assert session.scalar(select(func.count(MarketRegimeSnapshotRecord.id))) == 1
        assert session.scalar(select(func.count(StrategyCandidateSetRecord.id))) == 1
        assert session.scalar(select(func.count(RiskDecisionRecord.id))) == 1
        assert session.scalar(select(func.count(ShadowTradeRecord.id))) == 0
        assert session.scalar(select(func.count(ShadowTradeMarkRecord.id))) == 0


def test_pipeline_repository_chronological_and_api(repository, session_factory, market_snapshot):
    first = save(repository, market_snapshot).snapshot_id
    second_snapshot = market_snapshot.model_copy(update={
        "timestamp_ist": market_snapshot.timestamp_ist + timedelta(minutes=3)
    })
    second = save(repository, second_snapshot).snapshot_id
    pipeline = PipelineRepository(session_factory)
    events = []
    orchestrator = ResearchPipelineOrchestrator(pipeline, fake_steps(events))
    for snapshot_id in pipeline.raw_ids_chronological():
        orchestrator.run(snapshot_id)
    assert pipeline.raw_ids_chronological() == [first, second]
    app.dependency_overrides[get_pipeline_repository] = lambda: pipeline
    try:
        client = TestClient(app)
        assert client.get("/api/pipeline/latest").json()["market_snapshot_id"] == second
        assert client.get(f"/api/pipeline/{first}").status_code == 200
        assert len(client.get("/api/pipeline?limit=1").json()) == 1
    finally:
        app.dependency_overrides.clear()


def test_collector_pipeline_failure_does_not_rollback_raw(repository, session_factory, market_snapshot):
    called = []
    def broken(snapshot_id):
        called.append(snapshot_id)
        raise RuntimeError("pipeline failed")
    service = CollectorService(
        lambda: market_snapshot,
        repository,
        TradingCalendar(get_settings().collector_start_time, get_settings().collector_end_time),
        after_snapshot=broken,
    )
    result = service.collect_once()
    assert result is not None and called == [result.snapshot_id]
    with session_factory() as session:
        assert session.get(MarketSnapshotRecord, result.snapshot_id) is not None


def test_shadow_provider_state_authority_and_values(
    repository, session_factory, market_snapshot
):
    snapshot_id, risk_repository = persisted_context(repository, session_factory, market_snapshot)
    from app.risk.service import build_and_store_risk
    risk_result = build_and_store_risk(
        risk_repository, snapshot_id, approved_config(), EvaluationContext.HISTORICAL
    )
    assert risk_result.approved_count > 0
    entry = build_shadow_entry(
        ShadowRepository(session_factory), snapshot_id, shadow_config(get_settings())
    )
    assert entry.created and entry.trade is not None
    provider = ShadowRiskStateProvider(session_factory)
    day = market_snapshot.timestamp_ist.date()
    state = provider.get_state(day)
    assert state.trades_today == 1
    assert entry.trade.candidate_fingerprint in state.open_strategy_keys
    assert provider.is_authoritative("SHADOW")
    assert not provider.is_authoritative("LIVE")
    with session_factory.begin() as session:
        row = session.get(ShadowTradeRecord, entry.trade.id)
        row.status = "CLOSED"
        row.exit_timestamp = market_snapshot.timestamp_ist
        row.realized_pnl_per_lot = 1250
    pnl, complete = provider.get_realized_pnl_today(day)
    assert pnl == 1250 and complete


def test_live_rejects_both_non_live_providers(session_factory, market_snapshot):
    raw, feature, regime, candidates = risk_context(market_snapshot)
    for provider in (ResearchRiskStateProvider(), ShadowRiskStateProvider(session_factory)):
        result = evaluate_risk(
            candidates, 99, raw, feature, regime,
            approved_config(capital_base=1_000_000, max_daily_loss=100_000),
            EvaluationContext.LIVE, provider, ConfiguredMarketEventProvider(),
            evaluated_at=raw.timestamp_ist,
        )
        assert "RISK_STATE_UNAVAILABLE" in result.decisions[0].reason_codes


def test_shadow_limits_are_separate_and_missing_rejects(session_factory, market_snapshot):
    raw, feature, regime, candidates = risk_context(market_snapshot)
    settings = get_settings().model_copy(update={
        "risk_max_loss_per_trade": 999_999,
        "risk_max_capital_per_trade": 999_999,
        "shadow_risk_capital_base": None,
        "shadow_risk_max_loss_per_trade": None,
        "shadow_risk_max_capital_per_trade": None,
        "shadow_risk_max_daily_loss": None,
    })
    config = shadow_risk_config_from_settings(settings)
    assert config.max_loss_per_trade is None and config.max_capital_per_trade is None
    result = evaluate_risk(
        candidates, 99, raw, feature, regime, config, EvaluationContext.SHADOW,
        ShadowRiskStateProvider(session_factory), ConfiguredMarketEventProvider(),
        evaluated_at=raw.timestamp_ist,
    )
    assert "SHADOW_RISK_LIMIT_UNCONFIGURED" in result.decisions[0].reason_codes


def test_configured_shadow_limits_allow_authoritative_evaluation(session_factory, market_snapshot):
    raw, feature, regime, candidates = risk_context(market_snapshot)
    result = evaluate_risk(
        candidates, 99, raw, feature, regime,
        approved_config(capital_base=1_000_000, max_daily_loss=100_000),
        EvaluationContext.SHADOW, ShadowRiskStateProvider(session_factory),
        ConfiguredMarketEventProvider(), evaluated_at=raw.timestamp_ist,
    )
    assert result.approved_count > 0
    assert result.decisions[0].capital_at_risk_pct is not None


def test_shadow_daily_trade_and_loss_limits_are_enforced(market_snapshot):
    raw, feature, regime, candidates = risk_context(market_snapshot)

    class Provider:
        def __init__(self, *, trades=0, pnl=0.0):
            self._state = RiskState(
                trades, pnl, frozenset(), False, True, "SHADOW", True
            )
        def get_state(self, trading_date):
            return self._state

    config = approved_config(
        capital_base=1_000_000, max_daily_loss=500, max_trades_per_day=1
    )
    trade_limit = evaluate_risk(
        candidates, 99, raw, feature, regime, config, EvaluationContext.SHADOW,
        Provider(trades=1), ConfiguredMarketEventProvider(), evaluated_at=raw.timestamp_ist,
    )
    loss_limit = evaluate_risk(
        candidates, 99, raw, feature, regime, config, EvaluationContext.SHADOW,
        Provider(pnl=-500), ConfiguredMarketEventProvider(), evaluated_at=raw.timestamp_ist,
    )
    assert "MAX_TRADES_REACHED" in trade_limit.decisions[0].reason_codes
    assert "DAILY_LOSS_LIMIT_REACHED" in loss_limit.decisions[0].reason_codes


def test_daily_summary_counts_no_trade_pipeline(repository, session_factory, market_snapshot):
    snapshot_id = save(repository, market_snapshot).snapshot_id
    settings = get_settings().model_copy(update={
        "shadow_risk_capital_base": 1_000_000,
        "shadow_risk_max_loss_per_trade": 100_000,
        "shadow_risk_max_capital_per_trade": 100_000,
        "shadow_risk_max_daily_loss": 100_000,
    })
    build_pipeline_orchestrator(session_factory, settings).run(snapshot_id)
    summary = PipelineRepository(session_factory).daily_summary(market_snapshot.timestamp_ist.date())
    assert summary["snapshots_collected"] == 1
    assert summary["regime_counts"]["NO_TRADE"] == 1
    assert summary["candidate_sets_created"] == 1
    assert summary["approved_risk_decisions"] == 0
    assert summary["shadow_entries"] == summary["shadow_exits"] == 0
    assert summary["ai_calls"] == 0 and summary["ai_cost"] == 0
