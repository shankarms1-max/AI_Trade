"""Strategy-partitioned paper fills, limits, provenance, reporting and restart recovery."""
from datetime import timedelta
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.db.base import Base
from app.db.session import build_session_factory
from app.parallel_paper.config import CAPTURE_MODE, STRATEGIES, VERSIONS
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperEvent, ParallelPaperTrade
from app.parallel_paper.report import report, statistics
from app.parallel_paper.service import ParallelPaperService, verify_journals
from app.research.manifest import canonical, digest, json_value
from app.scalper.config import ScalperConfig
from app.scalper.models import ScalperCandidate, ScalperEvent, ScalperTrade
from app.scalper.research_capture import ResearchSnapshotRepository
from app.scalper.strategy import build_candidates
from app.scalper_lab.engine import _construction_signal
from tests.test_parallel_paper_strategies import FUTURE_EXPIRY, START
from tests.test_scalper import settings, snapshot
from worker import parallel_paper_worker as worker


def raw(index, price=None):
    base = snapshot(0)
    at = START + timedelta(seconds=index * 15)
    value = price if price is not None else 25000 + (10 if index % 2 else -10)
    return base.model_copy(update={
        "captured_at": at, "request_started_at": at - timedelta(seconds=1), "response_received_at": at,
        "source_market_timestamp": at - timedelta(seconds=1), "nifty_spot": value, "nifty_future": value,
        "future_instrument_id": "NIFTY_FUT", "future_expiry": FUTURE_EXPIRY,
        "quotes": [quote.model_copy(update={"source_market_timestamp": at - timedelta(seconds=1),
                                            "volume": 1000 + index * 10}) for quote in base.quotes]})


def capture(engine, observation):
    return ResearchSnapshotRepository(engine, mode=CAPTURE_MODE, capture_evidence={
        "volume_capture": "KOTAK_REST_ALL_LAST_VOLUME"}).save(observation).snapshot_id


def service(engine, **overrides):
    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add_all([ParallelPaperCursor(strategy_id=name, sequence=0, head_hash="GENESIS",
                                             lock_version=0, state_json="{}", latest_json="{}")
                         for name in STRATEGIES])
    return ParallelPaperService(sessions, settings(**overrides))


def warm(engine, paper):
    ids = [capture(engine, raw(index)) for index in range(240)]
    first = capture(engine, raw(240, 25030))
    assert paper.process_snapshot(first)["ORB_60"] == "WATCH"
    return ids, first


def trades(engine):
    with Session(engine) as session:
        return [(row.strategy_id, row.state, json.loads(row.document)) for row in session.scalars(
            select(ParallelPaperTrade).order_by(ParallelPaperTrade.strategy_id)).all()]


def test_simultaneous_independent_positions_fills_pnl_restart_and_acts_untouched(engine):
    paper = service(engine)
    with paper.sessions.begin() as session:
        session.add(ActsPaperCursor(id=1, sequence=0, head_hash="GENESIS", lock_version=0,
                                    watch_json="{}", latest_json="{}"))
    _, first = warm(engine, paper)
    confirmed = capture(engine, raw(242, 25030))
    statuses = paper.process_snapshot(confirmed)
    assert all(statuses[name] == "PENDING_PAPER_ENTRY" for name in ("ORB_15", "ORB_30", "ORB_60"))
    assert all(name != "FAILED" for name in statuses.values())
    paper.process_snapshot(capture(engine, raw(243, 25030)))
    entries = trades(engine)
    assert len(entries) == 3
    assert all(state == "OPEN" for _, state, _ in entries)
    for name, _, doc in entries:
        assert doc["strategy_id"] == name and doc["strategy_version"] == VERSIONS[name]
        assert doc["candidate"]["construction_method"] == "SHORT_LEG_FIRST_V1"
        assert doc["candidate"]["short_leg"]["option_type"] == "PE"
        assert doc["candidate"]["long_leg"]["strike"] < doc["candidate"]["short_leg"]["strike"]
        assert doc["entry_credit"] == pytest.approx(
            doc["entry_quotes"]["short"]["fill_price"] - doc["entry_quotes"]["long"]["fill_price"])
        assert doc["entry_quotes"]["short"]["side"] == "bid"
        assert doc["entry_quotes"]["long"]["side"] == "ask"
    heads = report(paper.sessions, raw(243).captured_at)
    assert set(heads["strategies"]) == {*STRATEGIES, "ACTS_V2"}
    assert heads["strategies"]["ORB_15"]["trades_today"] == 1
    assert heads["strategies"]["EMA_PULLBACK"]["trades_today"] == 0
    restarted = ParallelPaperService(paper.sessions, settings())
    assert restarted.process_snapshot(confirmed) == dict.fromkeys(STRATEGIES, "DUPLICATE")
    assert restarted.pending_snapshot_ids() == []
    assert len(restarted.required_contracts(raw(243).captured_at)) >= 2
    # A range re-entry is evidence, not a new bespoke profit-taking policy.
    restarted.process_snapshot(capture(engine, raw(244, 25000)))
    assert all(doc["false_breakout_first_at"] for _, state, doc in trades(engine) if state == "OPEN")
    forced = raw(1461, 25000)  # 15:20:15, with real exact fresh bid/ask books.
    restarted.process_snapshot(capture(engine, forced))
    closed = trades(engine)
    assert all(state == "CLOSED" for _, state, _ in closed)
    for _, _, doc in closed:
        assert doc["exit_reason"] == "FORCED_CLOSE"
        assert doc["exit_quotes"]["short"]["side"] == "ask"
        assert doc["exit_quotes"]["long"]["side"] == "bid"
        assert doc["exit_debit"] == pytest.approx(doc["exit_quotes"]["short"]["fill_price"]
                                                  - doc["exit_quotes"]["long"]["fill_price"])
        assert doc["holding_seconds"] > 0
        assert "mfe" in doc and "mae" in doc
    summary = report(paper.sessions, forced.captured_at)["strategies"]
    assert all(summary[name]["closed_trades"] == 1 for name in ("ORB_15", "ORB_30", "ORB_60"))
    assert summary["EMA_PULLBACK"]["gross_pnl"] == 0
    with Session(engine) as session:
        cursor = session.get(ActsPaperCursor, 1)
        assert cursor is not None and cursor.head_hash == "GENESIS" and cursor.lock_version == 0
        for table in (ActsPaperTrade, ActsPaperEvent, ScalperTrade, ScalperEvent):
            assert session.scalar(select(func.count()).select_from(table)) == 0
    verify_journals(paper.sessions)


def test_daily_limits_are_scoped_to_only_one_strategy(engine):
    paper = service(engine, parallel_paper_config_json=json.dumps({
        "risk": {"ORB_15": {"max_trades_per_day": 0}}}))
    warm(engine, paper)
    statuses = paper.process_snapshot(capture(engine, raw(242, 25030)))
    assert statuses["ORB_15"] == "ENTRY_REJECTED"
    assert statuses["ORB_30"] == statuses["ORB_60"] == "PENDING_PAPER_ENTRY"
    latest = report(paper.sessions, raw(242).captured_at)["strategies"]["ORB_15"]["latest"]
    assert "MAX_TRADES_PER_DAY_REACHED" in latest["blockers"]
    assert {name for name, _, _ in trades(engine)} == {"ORB_30", "ORB_60"}


def test_one_strategy_maximum_losses_do_not_block_other_strategies(engine):
    paper = service(engine, parallel_paper_config_json=json.dumps({
        "risk": {"ORB_15": {"max_consecutive_losses": 1}}}))
    observation = raw(0)
    historical_id = capture(engine, observation)
    candidate = build_candidates(observation, _construction_signal(observation, "BULL"),
                                 paper.configs["ORB_15"]).candidates[0]
    trade_id = digest(["closed-loss", historical_id])
    doc = json_value({"strategy_id": "ORB_15", "strategy_version": VERSIONS["ORB_15"],
                      "trade_id": trade_id, "policy_hash": paper.hashes["ORB_15"],
                      "policy": paper.policies["ORB_15"],
                      "decision": {"timestamp": observation.captured_at.isoformat(),
                                   "direction": "BULL", "thesis": {}},
                      "candidate": candidate, "entry_credit": candidate.executable_credit,
                      "entry_timestamp": observation.captured_at,
                      "exit_timestamp": observation.captured_at + timedelta(minutes=5),
                      "final_outcome": {"gross_rupees": -1000}})
    with paper.sessions.begin() as session:
        cursor = session.get(ParallelPaperCursor, "ORB_15")
        assert cursor is not None
        row = ParallelPaperTrade(id=trade_id, strategy_id="ORB_15", strategy_version=VERSIONS["ORB_15"],
                                 decision_snapshot_id=historical_id, state="CLOSED", document=canonical(doc))
        session.add(row)
        session.flush()
        paper._emit(session, cursor, historical_id, observation, "EXIT", row)
    warm(engine, paper)
    statuses = paper.process_snapshot(capture(engine, raw(242, 25030)))
    assert statuses["ORB_15"] == "ENTRY_REJECTED"
    assert statuses["ORB_30"] == "PENDING_PAPER_ENTRY"
    with Session(engine) as session:
        cursor = session.get(ParallelPaperCursor, "ORB_15")
        assert "MAX_CONSECUTIVE_LOSSES_REACHED" in json.loads(cursor.latest_json)["blockers"]
    verify_journals(paper.sessions)


def test_failure_rolls_back_one_strategy_and_others_commit_then_retry_idempotently(engine, monkeypatch):
    paper = service(engine)
    warm(engine, paper)
    confirmed = capture(engine, raw(242, 25030))
    original = paper._emit

    def failing(session, cursor, snapshot_id, observation, kind, trade=None, **details):
        original(session, cursor, snapshot_id, observation, kind, trade, **details)
        if cursor.strategy_id == "ORB_15" and kind == "OBSERVATION":
            raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(paper, "_emit", failing)
    statuses = paper.process_snapshot(confirmed)
    assert statuses["ORB_15"] == "FAILED"
    assert statuses["ORB_30"] == "PENDING_PAPER_ENTRY"
    assert {name for name, _, _ in trades(engine)} == {"ORB_30", "ORB_60"}
    verify_journals(paper.sessions)
    monkeypatch.setattr(paper, "_emit", original)
    retried = paper.process_snapshot(confirmed)
    assert retried["ORB_15"] == "PENDING_PAPER_ENTRY"
    assert retried["ORB_30"] == "DUPLICATE"
    verify_journals(paper.sessions)


def test_missing_hedge_never_fabricates_exit_or_reopens_unresolved(engine):
    paper = service(engine)
    warm(engine, paper)
    paper.process_snapshot(capture(engine, raw(242, 25030)))
    paper.process_snapshot(capture(engine, raw(243, 25030)))
    selected = ScalperCandidate.model_validate(trades(engine)[0][2]["candidate"])
    observation = raw(244, 25030)
    broken = observation.model_copy(update={"quotes": [
        quote for quote in observation.quotes
        if quote.instrument_token != selected.long_leg.instrument_token]})
    paper.process_snapshot(capture(engine, broken))
    before = trades(engine)
    assert all(state == "UNRESOLVED_EXPOSURE" for _, state, _ in before)
    paper.process_snapshot(capture(engine, raw(1461, 25030)))
    assert trades(engine) == before
    assert all(doc.get("exit_quotes") is None for _, _, doc in before)
    verify_journals(paper.sessions)


def test_fixed_option_basket_volume_identity_timestamps_and_causality(engine):
    paper = service(engine)
    first = raw(0)
    capture(engine, first)
    later = raw(1).model_copy(update={"atm_strike": 25050})
    capture(engine, later)
    points, evidence = paper.market.series(later.captured_at, 30)
    assert len(points) == 2
    assert points[1].volume - points[0].volume == 20
    assert all(leg["strike"] == 25000 for leg in evidence["basket"])
    wrong = raw(2).model_copy(update={"quotes": [
        quote.model_copy(update={"instrument_token": "WRONG_TOKEN"})
        if quote.strike == 25000 and quote.option_type == "PE" else quote
        for quote in raw(2).quotes]})
    capture(engine, wrong)
    points, _ = paper.market.series(wrong.captured_at, 30)
    assert points[-1].volume is None and points[-1].volume_reason == "FIXED_OPTION_BASKET_MISSING"
    future = raw(3).model_copy(update={"quotes": [quote.model_copy(update={
        "source_market_timestamp": raw(3).captured_at + timedelta(seconds=1)}) for quote in raw(3).quotes]})
    capture(engine, future)
    points, _ = paper.market.series(future.captured_at, 30)
    assert points[-1].volume is None
    assert points[-1].volume_reason == "OPTION_VOLUME_SOURCE_TIMESTAMP_INVALID"
    historical, _ = paper.market.series(later.captured_at, 30)
    assert len(historical) == 2  # Later records cannot leak into a prior calculation.


def test_journal_mutation_and_tampered_restart_are_refused(engine):
    paper = service(engine)
    paper.process_snapshot(capture(engine, raw(0)))
    with paper.sessions.begin() as session:
        event = session.scalar(select(ParallelPaperEvent).limit(1))
        with pytest.raises(ValueError, match="EVIDENCE_IS_IMMUTABLE"):
            event.payload = "{}"
            session.flush()
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE parallel_paper_cursors SET head_hash='tampered' WHERE strategy_id='ORB_15'")
    with pytest.raises(RuntimeError, match="JOURNAL_INVALID:ORB_15"):
        ParallelPaperService(paper.sessions, settings())


def test_report_metrics_handle_wins_losses_drawdown_and_unknown_exposure():
    rows = []
    for index, pnl in enumerate((1000, -500, -800, 200)):
        at = START + timedelta(minutes=index * 10)
        rows.append(("CLOSED", {"entry_timestamp": at.isoformat(),
                                "exit_timestamp": (at + timedelta(minutes=5)).isoformat(),
                                "final_outcome": {"gross_rupees": pnl, "net_rupees": None},
                                "mae": -5, "mfe": 10}))
    summary = statistics(rows, START)
    assert summary["gross_pnl"] == -100
    assert summary["win_rate"] == .5
    assert summary["max_drawdown"] == 1300
    assert summary["average_pnl"] == -25
    assert summary["profit_factor"] == pytest.approx(1200 / 1300)
    assert statistics([("UNRESOLVED_EXPOSURE", {})], START)["unrealized_pnl"] is None


def test_restarted_prior_session_trade_realized_today_counts_for_own_daily_risk(engine):
    paper = service(engine, parallel_paper_config_json=json.dumps({
        "risk": {"ORB_15": {"daily_loss_limit": 1000}}}))
    observation = raw(0)
    candidate = build_candidates(observation, _construction_signal(observation, "BULL"),
                                 paper.configs["ORB_15"]).candidates[0]
    yesterday = START - timedelta(days=1)
    doc = json_value({"decision": {"timestamp": yesterday.isoformat(), "direction": "BULL", "thesis": {}},
                      "candidate": candidate, "entry_timestamp": yesterday,
                      "entry_credit": candidate.executable_credit, "exit_timestamp": START - timedelta(seconds=15),
                      "final_outcome": {"gross_rupees": -1001}})
    row = ParallelPaperTrade(id="prior-session", strategy_id="ORB_15", strategy_version=VERSIONS["ORB_15"],
                             decision_snapshot_id=1, state="CLOSED", document=canonical(doc))
    blocked = paper._risk([row], paper.configs["ORB_15"], START, False)
    assert "HARD_DAILY_LOSS_REACHED" in blocked
    assert "ANY_EXIT_COOLDOWN_ACTIVE" in blocked
    assert paper._risk([], paper.configs["ORB_30"], START, False) == []
    summary = statistics([("CLOSED", json.loads(row.document))], START)
    assert summary["trades_today"] == 0
    assert summary["realized_pnl_today"] == -1001


def test_status_has_no_broker_or_strategy_execution_path(engine, monkeypatch, capsys):
    paper = service(engine)
    monkeypatch.setattr(worker, "get_settings", lambda: settings().model_copy(update={
        "database_url": SecretStr(str(engine.url))}))
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
    monkeypatch.setattr(worker, "make_snapshot_builder", lambda *_args, **_kw: (_ for _ in ()).throw(
        AssertionError("status attempted broker access")))
    before = report(paper.sessions, START)
    assert worker.main(["--mode", "PAPER", "--status"]) == 0
    assert set(json.loads(capsys.readouterr().out)["strategies"]) == {*STRATEGIES, "ACTS_V2"}
    assert report(paper.sessions, START) == before


def test_worker_is_disabled_by_default_and_refuses_live_mode_and_order_objects(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: settings())
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
    monkeypatch.setattr(worker, "build_engine", lambda *_: (_ for _ in ()).throw(
        AssertionError("disabled worker touched database")))
    assert worker.main(["--mode", "PAPER"]) == 0
    with pytest.raises(SystemExit):
        worker.main(["--mode", "LIVE"])

    class OrderCapable:
        def __call__(self):
            raise AssertionError("not market data")

        def place_order(self):
            raise AssertionError("live order invoked")

    with pytest.raises(RuntimeError, match="ORDER_CAPABILITY_FORBIDDEN"):
        worker.paper_only_builder(OrderCapable())


def test_strategy_configuration_hashes_do_not_contaminate_other_experiments(engine):
    paper = service(engine)
    changed = ParallelPaperService(paper.sessions, settings(parallel_paper_config_json=json.dumps({
        "rsi_bull": 60, "risk": {"ORB_15": {"max_trades_per_day": 8}}})))
    assert changed.hashes["ORB_15"] != paper.hashes["ORB_15"]
    assert changed.hashes["SUPERTREND_RSI_VOLUME"] != paper.hashes["SUPERTREND_RSI_VOLUME"]
    assert all(changed.hashes[name] == paper.hashes[name] for name in ("ORB_30", "ORB_60", "EMA_PULLBACK"))


def test_parallel_risk_defaults_overrides_and_policy_hashes(engine):
    paper = service(engine)
    previous = ParallelPaperService(paper.sessions, settings(parallel_paper_config_json=json.dumps({
        "risk": {name: {"max_consecutive_losses": 3} for name in STRATEGIES}})))
    overridden = ParallelPaperService(paper.sessions, settings(parallel_paper_config_json=json.dumps({
        "risk": {"ORB_15": {"max_consecutive_losses": 3}}})))
    offset_changed = ParallelPaperService(paper.sessions, settings(parallel_paper_capture_offset_seconds=14))
    for name in STRATEGIES:
        config = paper.configs[name]
        assert config.max_consecutive_losses == 6
        assert config.max_trades_per_day == 6
        assert config.max_open_positions == 1
        assert config.hard_daily_loss == 50000
        assert config.cooldown_after_exit_seconds == 60
        assert config.cooldown_after_loss_seconds == 180
        old_payload = previous.configs[name].payload()
        assert config.payload() == {**old_payload, "max_consecutive_losses": 6}
        assert previous.hashes[name] != paper.hashes[name]
        assert overridden.hashes[name] == (previous if name == "ORB_15" else paper).hashes[name]
    assert offset_changed.hashes == paper.hashes
    assert ScalperConfig.from_settings(settings()).max_consecutive_losses == 3


@pytest.mark.parametrize("offset", [-1, 15, 60, 5.5])
def test_invalid_capture_offsets_are_rejected(offset):
    with pytest.raises(ValidationError, match="parallel_paper_capture_offset_seconds"):
        settings(parallel_paper_capture_offset_seconds=offset)


@pytest.mark.parametrize("offset", [None, 0, 14])
def test_worker_keeps_capturing_and_processing_healthy_strategies_when_one_fails(engine, monkeypatch, offset):
    calls = []
    overrides = {} if offset is None else {"parallel_paper_capture_offset_seconds": offset}
    configuration = settings(parallel_paper_enabled=True, **overrides).model_copy(update={
        "database_url": SecretStr(str(engine.url))})
    monkeypatch.setattr(worker, "get_settings", lambda: configuration)
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
    monkeypatch.setattr(worker, "make_snapshot_builder", lambda *_args, **_kwargs: lambda: None)

    class FakeService:
        experiment = SimpleNamespace(bar_minutes=3)
        market = SimpleNamespace(basket=lambda *_: [])

        def __init__(self, *_):
            pass

        def required_contracts(self):
            return []

        def pending_snapshot_ids(self):
            return [1, 2]

        def process_snapshot(self, identity, *, skip_strategies=None):
            calls.append((identity, set(skip_strategies or ())))
            return {name: "FAILED" if name == "ORB_15" else "NO_SETUP" for name in STRATEGIES}

    class FakeCollector:
        def __init__(self, repository, builder, **kwargs):
            assert repository.mode == CAPTURE_MODE
            assert kwargs["start"].isoformat() == "09:15:00"

        def capture_once(self, now):
            return SimpleNamespace(snapshot_id=3)

    class FakeScheduler:
        def __init__(self, **_):
            pass

        def add_job(self, job, trigger, **kwargs):
            expected_offset = 5 if offset is None else offset
            assert str(trigger.fields[-1]) == ",".join(str(expected_offset + n * 15) for n in range(4))
            assert kwargs["max_instances"] == 1 and kwargs["coalesce"] is True
            first = trigger.get_next_fire_time(None, START)
            assert first == START + timedelta(seconds=expected_offset)
            current = first
            for _ in range(8):
                following = trigger.get_next_fire_time(current, current)
                assert following - current == timedelta(seconds=15)
                current = following
            self.job = job

        def start(self):
            self.job()
            raise KeyboardInterrupt

    monkeypatch.setattr(worker, "ParallelPaperService", FakeService)
    monkeypatch.setattr(worker, "ResearchSnapshotCollector", FakeCollector)
    monkeypatch.setattr(worker, "BlockingScheduler", FakeScheduler)
    assert worker.main(["--mode", "PAPER"]) == 0
    assert calls == [(1, set()), (2, {"ORB_15"}), (3, {"ORB_15"})]


def test_postgres16_independent_positions_and_read_only_report():
    value = os.getenv("PHASE15_POSTGRES_TEST_URL")
    if not value:
        pytest.skip("PHASE15_POSTGRES_TEST_URL must point to disposable PostgreSQL 16")
    base = make_url(value)
    admin = create_engine(base)
    schema = f"parallel_execution_{uuid4().hex}"
    target = create_engine(base.update_query_dict({"options": f"-csearch_path={schema}"}))
    try:
        with admin.begin() as connection:
            assert connection.scalar(text("SHOW server_version")).startswith("16.")
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        Base.metadata.create_all(target)
        paper = service(target)
        warm(target, paper)
        statuses = paper.process_snapshot(capture(target, raw(242, 25030)))
        assert statuses["ORB_15"] == statuses["ORB_30"] == statuses["ORB_60"] == "PENDING_PAPER_ENTRY"
        paper.process_snapshot(capture(target, raw(243, 25030)))
        assert len(trades(target)) == 3 and all(state == "OPEN" for _, state, _ in trades(target))
        before = report(paper.sessions, raw(243).captured_at)
        assert before["strategies"]["ORB_15"]["trades_today"] == 1
        assert report(paper.sessions, raw(243).captured_at) == before
        paper.process_snapshot(capture(target, raw(1461, 25000)))
        assert all(state == "CLOSED" for _, state, _ in trades(target))
        verify_journals(paper.sessions)
    finally:
        target.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
