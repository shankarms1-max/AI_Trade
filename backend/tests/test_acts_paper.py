"""ACTS paper journal, causal fills, isolation and restart behavior."""
from dataclasses import replace
from datetime import datetime, time, timedelta
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.acts_paper.alpha import ACTS_ALPHA_SOURCE, alpha_bucket
from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.acts_paper.service import ActsPaperService, verify_acts_paper_journal
from app.alpha.repository import AlphaRepository
from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.db.base import Base
from app.db.repositories import SnapshotRepository
from app.db.session import build_session_factory
from app.paper.models import PaperEvent, PaperTrade
from app.research.manifest import canonical, digest, json_value
from app.scalper.models import ScalperEvent, ScalperTrade
from app.scalper.paper import executable_pair
from app.scalper.research_capture import ACTS_PAPER_MODE, ResearchSnapshotRepository
from app.scalper_lab import acts
from app.scalper_lab.alpha_bridge import load_alpha_file
from app.scalper_lab.context import ResearchObservation
from app.scalper_lab.engine import LabTrade, Ledger, _candidate, _risk_blockers
from tests.test_scalper import settings
from tests.test_scalper_acts import acts_context, alpha_for, qualified_rows
from tests.test_scalper_alpha_bridge import _record
from tests.test_scalper_alpha_file import _csv
from worker import acts_paper_worker as worker


def _service(engine):
    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(ActsPaperCursor(id=1, sequence=0, head_hash="GENESIS",
                                    lock_version=0, watch_json="{}", latest_json="{}"))
    return ActsPaperService(sessions, settings(acts_paper_enabled=True))


def _capture(engine, raw):
    return ResearchSnapshotRepository(engine, mode=ACTS_PAPER_MODE).save(raw).snapshot_id


def _persist_alpha(engine, row, classification="BULL", *, calculated_shift=0):
    raw = row.snapshot
    market = MarketSnapshot(
        timestamp_ist=raw.captured_at, nifty_spot=raw.nifty_spot,
        nifty_future=raw.nifty_future,
        future_instrument_id=raw.future_instrument_id,
        future_expiry=raw.future_expiry, atm_strike=raw.atm_strike,
        expiry=raw.expiry, lot_size=raw.lot_size, source=ACTS_ALPHA_SOURCE,
        request_started_at=raw.request_started_at,
        response_received_at=raw.response_received_at,
        options=[OptionContractSnapshot.model_validate(quote.model_dump())
                 for quote in raw.quotes])
    sessions = build_session_factory(engine)
    raw_id = SnapshotRepository(sessions).save_market_snapshot(
        market, alpha_bucket(raw.captured_at)).snapshot_id
    alpha = alpha_for(row, classification,
                      calculated_shift=calculated_shift).model_copy(update={
                          "snapshot_id": raw_id})
    AlphaRepository(sessions).upsert(alpha)
    return alpha


def _trade_rows(engine):
    with Session(engine) as session:
        return [(row.state, json.loads(row.document)) for row in session.scalars(
            select(ActsPaperTrade).order_by(ActsPaperTrade.decision_snapshot_id)).all()]


def _seed_position(engine, *, pending=False):
    service = _service(engine)
    rows = qualified_rows(1, 70)
    raw = rows[-1].snapshot
    ctx = acts_context(rows)
    candidate, _ = _candidate(
        raw, {"strategy_id": acts.STRATEGY_ID, "direction": "BULL"},
        ctx, service.config)
    assert candidate is not None
    entry, failure = executable_pair(raw, candidate, service.config, entry=True)
    assert failure is None and entry is not None
    snapshot_id = _capture(engine, raw)
    decision = {"direction": "BULL", "timestamp": raw.captured_at.isoformat(),
                "thesis": {"wall": ctx["options"]["walls"]["put_support"],
                           "pullback_anchor": raw.nifty_spot}}
    doc = json_value({
        "strategy_version": acts.STRATEGY_ID,
        "policy_hash": service.policy_hash,
        "decision_timestamp": raw.captured_at,
        "decision_snapshot_id": snapshot_id,
        "direction": "BULL", "decision": decision, "candidate": candidate,
        "lot_size": raw.lot_size, "lots": 1,
        "entry_timestamp": None if pending else raw.captured_at,
        "entry_credit": None if pending else entry.value,
        "entry_spot": None if pending else raw.nifty_spot,
        "entry_quotes": None if pending else entry.evidence,
        "best_gross_points": 0.0})
    trade_id = digest([acts.STRATEGY_ID, snapshot_id, candidate.candidate_id])
    with service.sessions.begin() as session:
        trade = ActsPaperTrade(id=trade_id, decision_snapshot_id=snapshot_id,
                               state="PENDING_PAPER_ENTRY" if pending else "OPEN",
                               document=canonical(doc))
        session.add(trade)
        session.flush()
        cursor = session.get(ActsPaperCursor, 1)
        assert cursor is not None
        service._emit(session, cursor, "ACTS_PENDING_PAPER_ENTRY" if pending
                      else "ACTS_ENTRY", snapshot_id, raw, trade)
        cursor.snapshot_id = snapshot_id
        cursor.observed_at = raw.captured_at.isoformat()
    return service, raw, candidate, entry


def _next(raw, *, seconds=15, spot=None, short=None, long=None, candidate=None,
          missing=None):
    at = raw.captured_at + timedelta(seconds=seconds)
    quotes = []

    def identity(name):
        leg = getattr(candidate, name)
        return (leg.exchange, leg.instrument_token, leg.expiry, leg.strike,
                leg.option_type, leg.trading_symbol)

    for quote in raw.quotes:
        if candidate is not None and missing is not None and quote.identity == identity(missing):
            continue
        changes = {"source_market_timestamp": at}
        if candidate is not None and short is not None and quote.identity == identity(
                "short_leg"):
            changes.update(bid=short[0], ask=short[1])
        if candidate is not None and long is not None and quote.identity == identity(
                "long_leg"):
            changes.update(bid=long[0], ask=long[1])
        quotes.append(quote.model_copy(update=changes))
    return raw.model_copy(update={"captured_at": at,
                                  "request_started_at": at - timedelta(seconds=1),
                                  "response_received_at": at,
                                  "source_market_timestamp": at,
                                  "nifty_spot": raw.nifty_spot if spot is None else spot,
                                  "quotes": quotes})


def test_missing_alpha_no_setup_creates_no_paper_trade(engine):
    service = _service(engine)
    row = qualified_rows()[0]
    snapshot_id = _capture(engine, row.snapshot)

    assert service.process_snapshot(snapshot_id) == "NO_SETUP"
    assert service.process_snapshot(snapshot_id) == "DUPLICATE"
    assert _trade_rows(engine) == []
    status = service.status(row.snapshot.captured_at)
    assert status["latest_observation"]["alpha_state"] == "ALPHA_UNAVAILABLE"
    assert status["entries_remaining_today"] == 3
    verify_acts_paper_journal(service.sessions)


def test_acts_capture_and_history_are_isolated_from_other_15s_streams(engine):
    service = _service(engine)
    raw = qualified_rows()[0].snapshot
    acts_first = _capture(engine, raw)
    other = ResearchSnapshotRepository(engine).save(raw.model_copy(update={
        "nifty_spot": raw.nifty_spot + 100}))
    assert other.snapshot_id != acts_first
    later = _next(raw, spot=raw.nifty_spot + 1)
    snapshot_id = _capture(engine, later)

    ctx = service._context(ResearchObservation(snapshot_id, later))

    assert ctx["previous_spot"] == raw.nifty_spot
    prices = service.repository.price_history(
        since=raw.captured_at - timedelta(minutes=1),
        before=later.captured_at, capture_mode=ACTS_PAPER_MODE)
    assert len(prices) == 1
    assert prices[0].nifty_spot == raw.nifty_spot


def test_actsv1_local_alpha_reaches_one_paper_entry_without_old_journals(engine):
    rows = qualified_rows(1, 74)
    peak = rows[64].snapshot.nifty_spot
    values = {65: peak - 2, 66: peak - 3, 67: peak - 4,
              68: peak - 5, 69: peak - 6, 70: peak - 6,
              71: peak - 3, 72: peak - 1, 73: peak + 1}
    for index, value in values.items():
        rows[index] = replace(rows[index], snapshot=rows[index].snapshot.model_copy(
            update={"nifty_spot": value}))
    service = _service(engine)
    statuses = []
    for index, row in enumerate(rows):
        if index == 65:
            _persist_alpha(engine, row, "BULL")
        snapshot_id = _capture(engine, row.snapshot)
        statuses.append(service.process_snapshot(snapshot_id))
    trades = _trade_rows(engine)
    assert "WATCH" in statuses
    assert "PENDING_PAPER_ENTRY" in statuses
    assert len(trades) == 1
    assert trades[0][0] == "OPEN"
    doc = trades[0][1]
    assert doc["strategy_version"] == acts.STRATEGY_ID
    assert doc["lots"] == 1
    assert doc["entry_timestamp"] > doc["decision_timestamp"]
    assert doc["entry_quotes"]["short"]["side"] == "bid"
    assert doc["entry_quotes"]["long"]["side"] == "ask"
    assert doc["entry_credit"] == (
        doc["entry_quotes"]["short"]["fill_price"]
        - doc["entry_quotes"]["long"]["fill_price"])
    assert service.process_snapshot(doc["entry_snapshot_id"]) == "DUPLICATE"
    assert len(_trade_rows(engine)) == 1
    status = service.status(rows[-1].snapshot.captured_at)
    assert len(status["today_acts_trades"]) == 1
    assert status["today_acts_trades"][0]["state"] == "OPEN"
    with Session(engine) as session:
        for table in (PaperTrade, PaperEvent, ScalperTrade, ScalperEvent):
            assert session.scalar(select(func.count()).select_from(table)) == 0
    verify_acts_paper_journal(service.sessions)
    restarted = ActsPaperService(service.sessions, settings(acts_paper_enabled=True))
    assert restarted.status(rows[-1].snapshot.captured_at)["current_state"] == "OPEN"


def test_local_alpha_absence_and_arrival_do_not_crash(engine):
    service = _service(engine)
    rows = qualified_rows(1, 2)
    first = _capture(engine, rows[0].snapshot)
    assert service.process_snapshot(first) == "NO_SETUP"
    _persist_alpha(engine, rows[1], "BULL")
    second = _capture(engine, rows[1].snapshot)
    assert service.process_snapshot(second) == "NO_SETUP"
    status = service.status(rows[1].snapshot.captured_at)
    assert status["latest_observation"]["alpha_state"] == "BULL"
    verify_acts_paper_journal(service.sessions)


def test_local_alpha_causal_freshness_and_no_phase14_connection(engine, monkeypatch):
    service = _service(engine)
    row = qualified_rows()[0]
    _persist_alpha(engine, row, calculated_shift=30)
    monkeypatch.setattr("app.scalper_lab.alpha_bridge.create_engine",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("external database contacted")))

    too_early = service._alpha(ResearchObservation(
        2, _next(row.snapshot, seconds=15)))
    eligible = service._alpha(ResearchObservation(
        3, _next(row.snapshot, seconds=45)))
    stale = service._alpha(ResearchObservation(
        4, _next(row.snapshot, seconds=181)))

    assert too_early.alpha is None
    assert eligible.alpha is not None
    assert eligible.alpha.calculation_mode.value == "LIVE_ORIGINAL"
    assert eligible.alpha.price_source == "FUTURE"
    assert stale.alpha is None


def test_historical_replay_alpha_file_support_is_unchanged(tmp_path):
    row = qualified_rows()[0]
    path = tmp_path / "alpha.csv"
    _csv(path, [_record(alpha_for(row, "BULL"))])

    observations, diagnostics = load_alpha_file([row], path)

    assert diagnostics["alpha_source"] == "FILE"
    assert diagnostics["matched_observations"] == 1
    assert observations[0].alpha is not None


@pytest.mark.parametrize("reason", ["PROFIT_CAPTURE", "SPREAD_STOP",
                                    "TIME_STOP", "FORCED_CLOSE"])
def test_frozen_actsv1_exits_use_exact_executable_debit(engine, reason):
    service, raw, candidate, entry = _seed_position(engine)
    seconds = (30 * 60 if reason == "TIME_STOP" else
               int((datetime.combine(raw.captured_at.date(), time(15, 20),
                                     tzinfo=raw.captured_at.tzinfo)
                    - raw.captured_at).total_seconds())
               if reason == "FORCED_CLOSE" else 15)
    if reason in {"PROFIT_CAPTURE", "SPREAD_STOP"}:
        factor = .4 if reason == "PROFIT_CAPTURE" else 1.6
        close = 10 + entry.value * factor
        later = _next(raw, seconds=seconds, candidate=candidate,
                      short=(close - .05, close), long=(10, 10.05))
    else:
        later = _next(raw, seconds=seconds)
    snapshot_id = _capture(engine, later)

    service.process_snapshot(snapshot_id)

    state, doc = _trade_rows(engine)[0]
    assert state == "CLOSED"
    assert doc["exit_reason"] == reason
    assert doc["exit_quotes"]["short"]["side"] == "ask"
    assert doc["exit_quotes"]["long"]["side"] == "bid"
    assert doc["exit_debit"] == (
        doc["exit_quotes"]["short"]["fill_price"]
        - doc["exit_quotes"]["long"]["fill_price"])
    assert doc["gross_pnl"] == pytest.approx(
        (entry.value - doc["exit_debit"]) * raw.lot_size)
    assert "SIGNAL_FAILURE" not in doc["exit_reason"]
    verify_acts_paper_journal(service.sessions)


def test_one_fast_flip_only_marks_and_does_not_exit(engine):
    service, raw, candidate, _ = _seed_position(engine)
    later = _next(raw, spot=raw.nifty_spot - 1)
    service.process_snapshot(_capture(engine, later))

    state, doc = _trade_rows(engine)[0]
    assert state == "OPEN"
    assert doc["current_debit"] is not None
    assert doc.get("exit_reason") is None
    with Session(engine) as session:
        types = [row.event_type for row in session.scalars(select(ActsPaperEvent)).all()]
    assert "ACTS_MARK" in types and "ACTS_EXIT" not in types
    verify_acts_paper_journal(service.sessions)


def test_open_acts_trade_blocks_another_entry(engine, monkeypatch):
    service, raw, candidate, _ = _seed_position(engine)
    decision = {"status": "ENTER", "direction": "BULL", "trigger": "TEST_ONLY",
                "regime": "BULL", "alpha": {"classification": "BULL"},
                "positioning": {}, "blockers": [], "thesis": {
                    "wall": {"strike": raw.nifty_spot}}}
    monkeypatch.setattr(acts, "decide", lambda *_: dict(decision))
    monkeypatch.setattr("app.acts_paper.service._candidate",
                        lambda *_: (candidate, {}))
    result = service.process_snapshot(_capture(engine, _next(raw)))

    assert result == "ENTRY_REJECTED"
    rows = _trade_rows(engine)
    assert [state for state, _ in rows] == ["OPEN", "ENTRY_REJECTED"]
    assert "MAX_OPEN_POSITIONS_REACHED" in rows[-1][1]["decision"]["blockers"]
    verify_acts_paper_journal(service.sessions)


def test_three_entries_per_day_remain_capped(engine):
    service, raw, candidate, _ = _seed_position(engine)
    trades = []
    for index in range(3):
        at = raw.captured_at + timedelta(minutes=index * 30)
        trades.append(LabTrade(acts.STRATEGY_ID, candidate,
                               {"timestamp": at.isoformat(), "direction": "BULL"},
                               index + 1, state="CLOSED", entry_timestamp=at,
                               exit_timestamp=at + timedelta(minutes=15),
                               outcome={"gross_rupees": 100.0}))
    blockers = _risk_blockers(Ledger(trades=trades), service.config,
                              raw.captured_at + timedelta(hours=2), False)
    assert "MAX_TRADES_PER_DAY_REACHED" in blockers


def test_same_observations_replay_to_same_journal_and_pnl(engine, tmp_path):
    first, raw, candidate, entry = _seed_position(engine)
    second_engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'second.db'}")
    try:
        Base.metadata.create_all(second_engine)
        second, second_raw, second_candidate, second_entry = _seed_position(second_engine)
        assert second_raw == raw and second_candidate == candidate
        assert second_entry == entry
        close = 10 + entry.value * .4
        later = _next(raw, candidate=candidate,
                      short=(close - .05, close), long=(10, 10.05))
        for database, service in ((engine, first), (second_engine, second)):
            service.process_snapshot(_capture(database, later))
            verify_acts_paper_journal(service.sessions)
        assert _trade_rows(engine) == _trade_rows(second_engine)
        with Session(engine) as session:
            hashes = [(item.previous_hash, item.event_hash, item.payload)
                      for item in session.scalars(select(ActsPaperEvent).order_by(
                          ActsPaperEvent.sequence))]
        with Session(second_engine) as session:
            replayed = [(item.previous_hash, item.event_hash, item.payload)
                        for item in session.scalars(select(ActsPaperEvent).order_by(
                            ActsPaperEvent.sequence))]
        assert hashes == replayed
    finally:
        second_engine.dispose()


@pytest.mark.parametrize("reason", ["SUSTAINED_STRUCTURE_BREAK",
                                    "SUSTAINED_STRONG_ALPHA_REVERSAL"])
def test_confirmed_thesis_reversal_persists_across_observations(
        engine, reason):
    service, raw, candidate, _ = _seed_position(engine)
    if reason == "SUSTAINED_STRONG_ALPHA_REVERSAL":
        source = qualified_rows(1, 70)[-1]
        first_at = raw.captured_at + timedelta(seconds=15)
        alpha_row = replace(source, snapshot=source.snapshot.model_copy(update={
            "captured_at": first_at}))
        _persist_alpha(engine, alpha_row, "STRONG_BEAR")
    wall = _trade_rows(engine)[0][1]["decision"]["thesis"]["wall"]
    for index in range(1, 6):
        later = _next(raw, seconds=index * 15, candidate=candidate)
        if reason == "SUSTAINED_STRUCTURE_BREAK":
            broken = wall["strike"] - 11
            later = later.model_copy(update={"nifty_spot": broken,
                                             "nifty_future": broken})
        service.process_snapshot(_capture(engine, later))
        state, doc = _trade_rows(engine)[0]
        assert state == ("OPEN" if index < 5 else "CLOSED")
    assert doc["exit_reason"] == reason
    verify_acts_paper_journal(service.sessions)


@pytest.mark.parametrize("pending", [False, True])
def test_missing_exact_hedge_never_fabricates_a_fill(engine, pending):
    service, raw, candidate, _ = _seed_position(engine, pending=pending)
    later = _next(raw, candidate=candidate, missing="long_leg")
    service.process_snapshot(_capture(engine, later))

    state, doc = _trade_rows(engine)[0]
    assert state == ("ENTRY_REJECTED" if pending else "UNRESOLVED_EXPOSURE")
    assert doc["rejection_reason" if pending else "unresolved_reason"] == (
        "MISSING_EXACT_CONTRACT")
    assert doc.get("exit_quotes") is None
    assert doc.get("final_outcome") is None
    verify_acts_paper_journal(service.sessions)


def test_restart_restores_open_contracts_and_rejects_tampered_journal(engine):
    service, raw, candidate, _ = _seed_position(engine)
    restarted = ActsPaperService(service.sessions,
                                 settings(acts_paper_enabled=True))
    held = restarted.required_contracts()
    assert {item.instrument_token for item in held} == {
        candidate.short_leg.instrument_token, candidate.long_leg.instrument_token}
    assert restarted.status(raw.captured_at)["current_state"] == "OPEN"
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE acts_paper_events SET payload = '{}' WHERE sequence = 1")
    with pytest.raises(RuntimeError, match="ACTS_PAPER_JOURNAL_INVALID"):
        ActsPaperService(service.sessions, settings(acts_paper_enabled=True))


def test_restart_rejects_cursor_detached_from_last_observation(engine):
    service = _service(engine)
    snapshot_id = _capture(engine, qualified_rows()[0].snapshot)
    service.process_snapshot(snapshot_id)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE acts_paper_cursor SET latest_json = '{}' WHERE id = 1")
    with pytest.raises(RuntimeError, match="ACTS_PAPER_CURSOR_PROJECTION_INVALID"):
        ActsPaperService(service.sessions, settings(acts_paper_enabled=True))


def test_paper_worker_disabled_independently_of_live_scalper(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: settings().model_copy(
        update={"acts_paper_enabled": False, "scalper_enabled": True}))
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
    monkeypatch.setattr(worker, "build_engine", lambda *_: (_ for _ in ()).throw(
        AssertionError("disabled worker touched the database")))
    assert worker.main(["--mode", "PAPER"]) == 0


def test_paper_worker_uses_market_data_only_and_fifteen_second_schedule(monkeypatch):
    calls = []

    class FakeEngine:
        def dispose(self):
            calls.append("dispose")

    class FakeService:
        def __init__(self, *_args):
            pass

        def required_contracts(self):
            return []

        def process_snapshot(self, snapshot_id):
            calls.append(("process", snapshot_id))

    class FakeLocalAlpha:
        def __init__(self, *_args):
            pass

        def observe(self, market):
            calls.append(("alpha", market))

    class FakeCollector:
        def __init__(self, _repository, builder, **kwargs):
            self.builder = builder
            calls.append(("window", kwargs["start"], kwargs["end"]))

        def capture_once(self, _now):
            self.builder()
            return SimpleNamespace(snapshot_id=123, duplicate=False)

    class FakeScheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, run, trigger, **_kwargs):
            first = trigger.get_next_fire_time(
                None, datetime(2099, 10, 1, 9, 14, 59,
                               tzinfo=worker.IST))
            assert first == datetime(2099, 10, 1, 9, 15,
                                     tzinfo=worker.IST)
            self.run = run

        def start(self):
            self.run()

    monkeypatch.setattr(worker, "get_settings", lambda: settings().model_copy(
        update={"acts_paper_enabled": True,
                "database_url": SecretStr("sqlite:///paper-test")}))
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
    monkeypatch.setattr(worker, "build_engine", lambda *_: FakeEngine())
    monkeypatch.setattr(worker, "build_session_factory", lambda *_: None)
    monkeypatch.setattr(worker, "ActsPaperService", FakeService)
    monkeypatch.setattr(worker, "LocalActsAlpha", FakeLocalAlpha)
    monkeypatch.setattr(worker, "ResearchSnapshotCollector", FakeCollector)
    monkeypatch.setattr(worker, "ResearchSnapshotRepository", lambda *_args,
                        **_kwargs: None)
    monkeypatch.setattr(worker, "make_live_snapshot_builder", lambda *_args,
                        **_kwargs: lambda: SimpleNamespace(source="KOTAK_NEO"))
    monkeypatch.setattr(worker, "BlockingScheduler", FakeScheduler)

    assert worker.main(["--mode", "PAPER"]) == 0
    assert ("window", time(9, 15), time(15, 20)) in calls
    assert any(isinstance(item, tuple) and item[0] == "alpha" for item in calls)
    assert calls.index(("process", 123)) > next(
        index for index, item in enumerate(calls)
        if isinstance(item, tuple) and item[0] == "alpha")
    assert ("process", 123) in calls
    assert calls[-1] == "dispose"


def test_paper_worker_refuses_order_capable_builder():
    class OrderCapable:
        def __call__(self):
            return None

        def place_order(self):
            raise AssertionError("must remain unreachable")

    with pytest.raises(RuntimeError, match="ACTS_PAPER_ORDER_CAPABILITY_FORBIDDEN"):
        worker._paper_only_builder(OrderCapable())
