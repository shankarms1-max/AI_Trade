"""Offline executable-book fixtures; none represent inferred broker depth units."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.core.config import Settings
from app.paper.models import PaperCursor, PaperEvent, PaperTrade
from app.paper.service import PaperEngine, assert_no_paper_exposure, execution_mode, paper_summary, verify_journal
from app.risk.models import EvaluationContext
from app.risk.service import build_and_store_risk
from tests.test_phase7_risk import approved_config, persisted_context


def settings_for_test(**updates):
    if updates.get("forward_paper_enabled"):
        updates.setdefault("pipeline_after_snapshot", True)
    return Settings(_env_file=None, kotak_consumer_key="offline-test-only", **updates)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def paper_case(repository, session_factory, market_snapshot):
    at = market_snapshot.timestamp_ist.replace(minute=18, second=0)
    raw = market_snapshot.model_copy(update={"timestamp_ist": at, "options": [item.model_copy(update={
        "source_market_timestamp": at, "bid_quantity": 10000, "ask_quantity": 10000,
        "depth_unit": "UNITS", "tick_size": .05}) for item in market_snapshot.options]})
    snapshot_id, risk_repository = persisted_context(repository, session_factory, raw)
    result = build_and_store_risk(risk_repository, snapshot_id, approved_config(), EvaluationContext.HISTORICAL)
    assert result.approved_count > 0
    with session_factory.begin() as session:
        session.add(PaperCursor(id=1))
    settings = settings_for_test(forward_paper_enabled=True,
        shadow_risk_capital_base=1000000, shadow_risk_max_loss_per_trade=100000,
        shadow_risk_max_capital_per_trade=100000, shadow_risk_max_daily_loss=100000)
    clock = [at]
    paper = PaperEngine(session_factory, settings, clock=lambda: clock[0])
    with session_factory() as session:
        raw = paper.raw(session, snapshot_id)
    case = SimpleNamespace(paper=paper, clock=clock, settings=settings, raw=raw,
                           snapshot_id=snapshot_id, sessions=session_factory, repository=repository)
    paper.observe(snapshot_id)
    paper.enqueue(snapshot_id)
    assert trade(case)[0] == "PENDING_PAPER_ENTRY"
    return case


def trade(case):
    with case.sessions() as session:
        row = session.scalar(select(PaperTrade))
        return row.state, json.loads(row.document)


def events(case):
    with case.sessions() as session:
        return [(row.event_type, json.loads(row.payload)) for row in session.scalars(
            select(PaperEvent).order_by(PaperEvent.sequence))]


def next_snapshot(case, *, seconds=180, mutate=None):
    at = case.raw.timestamp_ist + timedelta(seconds=seconds)
    raw = case.raw.model_copy(update={"timestamp_ist": at, "response_received_at": at,
        "options": [item.model_copy(update={"source_market_timestamp": at}) for item in case.raw.options]})
    if mutate:
        raw = mutate(raw)
    saved = case.repository.save_market_snapshot(raw, at)
    case.clock[0] = at
    case.raw = raw
    return saved.snapshot_id


def change_short(case, **values):
    token = trade(case)[1]["candidate"]["short_leg"]["instrument_token"]
    return lambda raw: raw.model_copy(update={"options": [
        item.model_copy(update=values) if item.instrument_token == token else item for item in raw.options]})


def test_strictly_later_entry_sides_restart_and_duplicate(paper_case):
    case = paper_case
    sequence = verify_journal(case.sessions)
    case.paper.observe(case.snapshot_id)
    case.paper.enqueue(case.snapshot_id)
    assert trade(case)[0] == "PENDING_PAPER_ENTRY"
    assert verify_journal(case.sessions) == sequence
    case.paper = PaperEngine(case.sessions, case.settings, clock=lambda: case.clock[0])
    later = next_snapshot(case)
    case.paper.observe(later)
    state, doc = trade(case)
    assert state == "OPEN"
    assert doc["decision_snapshot_id"] == case.snapshot_id < doc["execution_snapshot_id"] == later
    assert doc["entry_timestamp"] > doc["decision_timestamp"]
    short, long = doc["entry_quotes"]["short"], doc["entry_quotes"]["long"]
    assert short["side"] == "bid" and long["side"] == "ask"
    assert doc["entry_credit"] == short["observed_contract"]["bid"] - long["observed_contract"]["ask"]
    case.paper = PaperEngine(case.sessions, case.settings, clock=lambda: case.clock[0])
    case.paper.observe(later)
    assert [kind for kind, _ in events(case)].count("ENTRY_EXECUTION") == 1
    assert verify_journal(case.sessions) > sequence


@pytest.mark.parametrize("change,reason", [
    ({"bid": None}, "MISSING_EXECUTABLE_SIDE"),
    ({"ask": None}, "MISSING_EXECUTABLE_SIDE"),
    ({"bid": 0}, "NONPOSITIVE_EXECUTABLE_PRICE"),
    ({"ask": -1}, "NONPOSITIVE_EXECUTABLE_PRICE"),
    ({"bid": 1000}, "CROSSED_BOOK"),
    ({"instrument_token": "different-token"}, "MISSING_EXACT_CONTRACT"),
    ({"expiry": None}, "MISSING_EXACT_CONTRACT"),
    ({"depth_unit": "UNKNOWN"}, "UNKNOWN_REQUIRED_DEPTH"),
    ({"bid_quantity": 0}, "INSUFFICIENT_DEPTH"),
    ({"bid_quantity": 1}, "INSUFFICIENT_DEPTH"),
    ({"bid_quantity": None}, "UNKNOWN_REQUIRED_DEPTH"),
    ({"source_market_timestamp": None}, "MISSING_QUOTE_TIMESTAMP"),
    ({"tick_size": 100}, "INVALID_TICK_PRICE"),
])
def test_bad_entry_books_never_fill(paper_case, change, reason):
    # Wrong expiry is another valid date, not an invalid model fixture.
    if change.get("expiry", "present") is None:
        change = {"expiry": paper_case.raw.expiry + timedelta(days=7)}
    mutation = change_short(paper_case, **change)
    if "expiry" in change:
        mutation = lambda raw: raw.model_copy(update={"expiry": change["expiry"], "options": [
            item.model_copy(update=change) for item in raw.options]})
    snapshot = next_snapshot(paper_case, mutate=mutation)
    paper_case.paper.observe(snapshot)
    state, doc = trade(paper_case)
    assert state == "ENTRY_REJECTED" and doc["rejection_reason"] == reason
    assert "entry_credit" not in doc
    assert "ENTRY_EXECUTION" not in [kind for kind, _ in events(paper_case)]
    if reason == "UNKNOWN_REQUIRED_DEPTH":
        quote = events(paper_case)[-2][1]["quotes"]["short"]
        assert quote["validation"]["valid"] is False
    verify_journal(paper_case.sessions)


def test_stale_source_missing_path_and_ttl(paper_case):
    case = paper_case
    later = next_snapshot(case, mutate=change_short(case, source_market_timestamp=case.raw.timestamp_ist))
    case.paper.observe(later)
    assert trade(case)[1]["rejection_reason"] == "STALE_LEG_QUOTE"


@pytest.mark.parametrize("seconds,reason", [(220, "MISSING_EXPECTED_PATH_OBSERVATION"),
                                          (360, "DECISION_TO_FILL_TTL_EXCEEDED")])
def test_missing_next_observation_and_fill_delay(paper_case, seconds, reason):
    later = next_snapshot(paper_case, seconds=seconds)
    paper_case.paper.observe(later)
    assert trade(paper_case)[1]["rejection_reason"] == reason


def test_wall_clock_stale_book(paper_case):
    later = next_snapshot(paper_case)
    paper_case.clock[0] += timedelta(seconds=31)
    paper_case.paper.observe(later)
    assert trade(paper_case)[1]["rejection_reason"] == "STALE_OR_FUTURE_OBSERVATION"


def test_exit_observed_sides_incomplete_costs_no_profitability(paper_case):
    case = paper_case
    case.paper.observe(next_snapshot(case))
    # Deliberate fixture observes profit-target prices on the next book.
    doc = trade(case)[1]
    short = doc["candidate"]["short_leg"]["instrument_token"]
    long = doc["candidate"]["long_leg"]["instrument_token"]
    def exit_book(raw):
        return raw.model_copy(update={"options": [item.model_copy(update=(
            {"bid": 19, "ask": 20} if item.instrument_token == short else
            {"bid": 19, "ask": 20} if item.instrument_token == long else {})) for item in raw.options]})
    case.paper.observe(next_snapshot(case, mutate=exit_book))
    state, doc = trade(case)
    assert state == "CLOSED" and doc["exit_reason"] == "PROFIT_TARGET_EXIT"
    assert doc["exit_quotes"]["short"]["side"] == "ask"
    assert doc["exit_quotes"]["long"]["side"] == "bid"
    assert doc["current_debit"] == 1
    assert doc["final_outcome"]["gross_points_per_unit"] == doc["entry_credit"]-1
    assert doc["final_outcome"]["net_rupees"] is None
    assert doc["final_outcome"]["cost_completeness"] == "GROSS_ONLY"
    assert doc["profitability_claim"] is False
    assert not case.paper.get_state(case.raw.timestamp_ist.date()).monetary_pnl_complete
    assert {"MARK", "EXIT_EXECUTION", "FINAL_OUTCOME"} <= {kind for kind, _ in events(case)}
    verify_journal(case.sessions)


@pytest.mark.parametrize("missing_path", [False, True])
def test_bad_exit_retains_unresolved_exposure(paper_case, missing_path):
    case = paper_case
    case.paper.observe(next_snapshot(case))
    bad = next_snapshot(case, seconds=360 if missing_path else 180,
                        mutate=None if missing_path else change_short(case, ask=None))
    case.paper.observe(bad)
    state, doc = trade(case)
    assert state == "UNRESOLVED_EXPOSURE" and "final_outcome" not in doc
    assert doc["entry_credit"] > 0
    case.paper = PaperEngine(case.sessions, case.settings, clock=lambda: case.clock[0])
    case.paper.observe(next_snapshot(case))
    assert trade(case)[0] == "UNRESOLVED_EXPOSURE"
    assert paper_summary(case.sessions)["unresolved"] == 1
    assert case.paper.get_state(case.raw.timestamp_ist.date()).open_strategy_keys
    with pytest.raises(ValueError, match="NO_FORWARD_PAPER_EXPOSURE"):
        assert_no_paper_exposure(case.sessions)


def test_concurrent_duplicate_callback(paper_case):
    later = next_snapshot(paper_case)
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(paper_case.paper.observe, [later, later]))
    assert [kind for kind, _ in events(paper_case)].count("ENTRY_EXECUTION") == 1
    verify_journal(paper_case.sessions)


def test_atomic_rollback_after_failed_journal_append(paper_case, monkeypatch):
    case = paper_case
    later = next_snapshot(case)
    original = case.paper.emit
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated interruption")
    monkeypatch.setattr(case.paper, "emit", fail)
    with pytest.raises(RuntimeError):
        case.paper.observe(later)
    assert trade(case)[0] == "PENDING_PAPER_ENTRY"
    verify_journal(case.sessions)
    monkeypatch.setattr(case.paper, "emit", original)
    case.paper.observe(later)
    assert trade(case)[0] == "OPEN"


def test_immutable_events_and_projection_verification(paper_case):
    with pytest.raises(ValueError, match="IMMUTABLE"):
        with paper_case.sessions.begin() as session:
            row = session.scalar(select(PaperEvent))
            row.payload = "{}"
    with paper_case.sessions.begin() as session:
        session.execute(update(PaperTrade).values(state="CLOSED"))
    with pytest.raises(ValueError, match="PROJECTION_INTEGRITY"):
        verify_journal(paper_case.sessions)


def test_default_flags_and_authoritative_summary(session_factory):
    settings = settings_for_test()
    assert not settings.forward_paper_enabled and not settings.pipeline_decision_only
    assert execution_mode(settings) == "CAPTURE_ONLY"
    assert execution_mode(settings.model_copy(update={"pipeline_after_snapshot": True})) == "UNAVAILABLE"
    assert execution_mode(settings.model_copy(update={"pipeline_decision_only": True})) == "DECISION_ONLY"
    assert paper_summary(session_factory) == dict(pending=0, open=0, closed=0, unresolved=0, rejected=0, profitability_claim=False)


@pytest.mark.parametrize("updates", [dict(pipeline_decision_only=True),
    dict(phase14_2_1_replay_integrity_enabled=True), dict(pipeline_run_ai_research=True),
    dict(replay_allow_unknown_depth=True), dict(replay_allow_0dte=True), dict(collector_interval_minutes=5),
    dict(telegram_enabled=True), dict(shadow_allow_multiple_open_trades=True), dict(pipeline_after_snapshot=False)])
def test_invalid_modes_rejected(updates):
    with pytest.raises(ValueError, match="FORWARD_PAPER"):
        settings_for_test(forward_paper_enabled=True, **updates)


def test_paper_executes_without_network_or_broker_calls(paper_case, monkeypatch):
    import socket
    from app.broker.kotak.market_data import KotakMarketDataAdapter
    def forbidden(*args, **kwargs):
        raise AssertionError("network/broker must not be touched")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(KotakMarketDataAdapter, "__init__", forbidden)
    paper_case.paper.observe(next_snapshot(paper_case))
    assert trade(paper_case)[0] == "OPEN"


def test_no_market_data_mutation(paper_case):
    with paper_case.sessions() as session:
        before = paper_case.paper.raw(session, paper_case.snapshot_id).model_dump(mode="json")
    paper_case.paper.observe(next_snapshot(paper_case))
    with paper_case.sessions() as session:
        after = paper_case.paper.raw(session, paper_case.snapshot_id).model_dump(mode="json")
    assert before == after


def test_zero_dte_decision_rejected_even_if_legacy_risk_approves(repository, session_factory, market_snapshot):
    at = market_snapshot.timestamp_ist.replace(minute=18, second=0)
    raw = market_snapshot.model_copy(update={"timestamp_ist": at, "expiry": at.date(), "options": [
        item.model_copy(update={"expiry": at.date(), "source_market_timestamp": at,
            "bid_quantity": 10000, "ask_quantity": 10000, "depth_unit": "UNITS"})
        for item in market_snapshot.options]})
    snapshot_id, risks = persisted_context(repository, session_factory, raw)
    # A legacy risk approval is never permission to relax the paper guard.
    approved = build_and_store_risk(risks, snapshot_id, approved_config(allow_expiry_day=True),
                                    EvaluationContext.HISTORICAL)
    assert approved.approved_count > 0
    with session_factory.begin() as session:
        session.add(PaperCursor(id=1))
    paper = PaperEngine(session_factory, settings_for_test(forward_paper_enabled=True), clock=lambda: at)
    paper.observe(snapshot_id)
    paper.enqueue(snapshot_id)
    case = SimpleNamespace(sessions=session_factory)
    assert trade(case)[0] == "ENTRY_REJECTED"
    assert trade(case)[1]["rejection_reason"] == "ZERO_DTE_EXCLUDED"
    verify_journal(session_factory)


def test_real_pipeline_order_timings_and_legacy_paths_disabled(paper_case, monkeypatch):
    from dataclasses import replace
    from app.pipeline.service import build_pipeline_orchestrator
    case = paper_case
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy execution/AI must never run")
    monkeypatch.setattr("app.pipeline.service.build_shadow_entry", forbidden)
    monkeypatch.setattr("app.pipeline.service.update_open_trades", forbidden)
    monkeypatch.setattr("app.pipeline.service.OpenAIResearchProvider", forbidden)
    # The real engine's only non-SQL dependency is its injected clock.
    monkeypatch.setattr("app.paper.service.PaperEngine", lambda sessions, settings:
        PaperEngine(sessions, settings, clock=lambda: case.clock[0]))
    orchestrator = build_pipeline_orchestrator(case.sessions, case.settings)
    order = []
    def wrap(name, call):
        def wrapped(snapshot):
            order.append(name)
            return call(snapshot)
        return wrapped
    orchestrator._steps = replace(orchestrator._steps, **{
        name: wrap(name, getattr(orchestrator._steps, name)) for name in
        ("shadow_update", "features", "regime", "strategy", "risk", "shadow_entry")})
    later = next_snapshot(case)
    result = orchestrator.run(later)
    assert result.status.value == "SUCCESS"
    assert order == ["shadow_update", "features", "regime", "strategy", "risk", "shadow_entry"]
    assert result.stage_timings_ms["total_pipeline"] < 180000
    print("FORWARD_PAPER_TEST_RUNTIME_MS", result.stage_timings_ms)
    assert trade(case)[0] == "OPEN" and result.shadow_trade_id is None
    verify_journal(case.sessions)


def test_decision_only_cannot_strand_forward_exposure(paper_case):
    from app.pipeline.service import build_pipeline_orchestrator
    case = paper_case
    settings = settings_for_test(pipeline_decision_only=True)
    result = build_pipeline_orchestrator(case.sessions, settings).run(next_snapshot(case))
    assert result.status.value == "FAILED"
    assert trade(case)[0] == "PENDING_PAPER_ENTRY"


def test_dashboard_authoritative_mode_and_counts(paper_case, monkeypatch):
    from app.api import dashboard
    case = paper_case
    monkeypatch.setattr(dashboard, "get_settings", lambda: case.settings)
    report = dashboard.latest_dashboard(case.sessions)
    assert report["execution_mode"] == "PAPER"
    assert report["forward_paper"]["pending"] == 1
    assert report["forward_paper"]["open"] == 0
    assert report["open_shadow_trade"] is None


@pytest.mark.parametrize("trigger", ["STOP_LOSS_EXIT", "STRUCTURAL_INVALIDATION_EXIT", "OPPOSITE_REGIME_EXIT"])
def test_existing_exit_policies_unchanged(paper_case, trigger):
    case = paper_case
    case.paper.observe(next_snapshot(case))
    doc = trade(case)[1]
    mutate = None
    if trigger == "STOP_LOSS_EXIT":
        short = doc["candidate"]["short_leg"]["instrument_token"]
        long = doc["candidate"]["long_leg"]["instrument_token"]
        debit = doc["entry_credit"] * 2.5 + 1
        def mutate(raw):
            return raw.model_copy(update={"options": [item.model_copy(update=(
                {"bid": 9+debit, "ask": 10+debit} if item.instrument_token == short else
                {"bid": 10, "ask": 11} if item.instrument_token == long else {})) for item in raw.options]})
    elif trigger == "STRUCTURAL_INVALIDATION_EXIT":
        mutate = lambda raw: raw.model_copy(update={"nifty_spot": doc["candidate"]["support_or_resistance_reference"]-1})
    else:
        # The pre-feature stage uses the most recent *earlier* persisted regime.
        persisted_context(case.repository, case.sessions, case.raw, "BEARISH")
    case.paper.observe(next_snapshot(case, mutate=mutate))
    assert trade(case)[0] == "CLOSED"
    assert trade(case)[1]["exit_reason"] == trigger
    verify_journal(case.sessions)


def test_forced_exit_uses_first_observation_at_existing_cutoff(paper_case):
    case = paper_case
    case.paper.observe(next_snapshot(case))
    while case.raw.timestamp_ist.time().replace(tzinfo=None) < case.paper.exits.force_exit_time:
        case.paper.observe(next_snapshot(case))
    state, doc = trade(case)
    assert state == "CLOSED" and doc["exit_reason"] == "TIME_EXIT"
    assert doc["exit_timestamp"][11:16] == "15:21"
    verify_journal(case.sessions)


def test_legacy_open_exposure_refuses_paper_execution(repository, session_factory, market_snapshot):
    from app.shadow.repository import ShadowRepository
    from app.shadow.service import build_shadow_entry, config_from_settings
    snapshot_id, risks = persisted_context(repository, session_factory, market_snapshot)
    build_and_store_risk(risks, snapshot_id, approved_config(), EvaluationContext.HISTORICAL)
    settings = settings_for_test(forward_paper_enabled=True)
    result = build_shadow_entry(ShadowRepository(session_factory), snapshot_id, config_from_settings(settings))
    assert result.created
    with session_factory.begin() as session:
        session.add(PaperCursor(id=1))
    paper = PaperEngine(session_factory, settings, clock=lambda: market_snapshot.timestamp_ist)
    with pytest.raises(ValueError, match="NO_LEGACY_EXPOSURE"):
        paper.observe(snapshot_id)
    assert paper_summary(session_factory)["open"] == 0
    assert paper_summary(session_factory)["pending"] == 0


def test_future_regime_cannot_trigger_earlier_exit(paper_case):
    case = paper_case
    case.paper.observe(next_snapshot(case))
    later = next_snapshot(case)
    persisted_context(case.repository, case.sessions, case.raw, "BEARISH")
    case.paper.observe(later)
    # Opposite regime at this snapshot is not yet available in normal ordering.
    assert trade(case)[0] == "OPEN"
    case.paper.observe(next_snapshot(case))
    assert trade(case)[1]["exit_reason"] == "OPPOSITE_REGIME_EXIT"
