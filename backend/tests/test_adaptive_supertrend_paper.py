"""Independent AST next-observation fills, immutable evidence and recovery."""

from datetime import datetime, time, timedelta
import inspect
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text

from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.journal import verify
from app.adaptive_supertrend.models import AstCursor, AstTrade, OptimizerRun, SessionParameters
from app.adaptive_supertrend.report import report
from app.adaptive_supertrend.service import AstPaperService, freeze
from app.acts_paper.models import ActsPaperEvent, ActsPaperTrade
from app.db.session import build_session_factory
from app.parallel_paper.models import ParallelPaperEvent, ParallelPaperTrade
from app.research.manifest import digest
from app.scalper.models import ScalperEvent, ScalperTrade
from app.scalper.research_capture import ResearchSnapshotRepository
from tests.test_adaptive_supertrend import EXPIRY, START, bars, policy
from tests.test_scalper import settings, snapshot
from worker import adaptive_supertrend_worker as worker


def raw(index, price=25100):
    at = START + timedelta(seconds=index * 15)
    template = snapshot(0)
    return template.model_copy(
        update={
            "captured_at": at,
            "request_started_at": at - timedelta(seconds=1),
            "response_received_at": at,
            "source_market_timestamp": at,
            "nifty_spot": price,
            "nifty_future": price,
            "future_instrument_id": "FUT",
            "future_expiry": EXPIRY,
            "quotes": [quote.model_copy(update={"source_market_timestamp": at}) for quote in template.quotes],
        }
    )


def capture(engine, observation):
    from app.adaptive_supertrend.capture import save_capture

    return save_capture(build_session_factory(engine), observation.model_dump(exclude={"quotes"}))


def setup(engine, cfg=None, *, shared_source_offset_seconds=0, sign=1):
    cfg = cfg or policy()
    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(
            AstCursor(id=1, sequence=0, head_hash="GENESIS", lock_version=0, state_json="{}", latest_json="{}")
        )
    prior = START - timedelta(days=1)
    # Production freeze path and full grid, with genuine causal synthetic futures bars.
    freeze(sessions, bars(day=prior), START.date(), START.replace(hour=9, minute=0), cfg)
    value = settings(ast_config_json=cfg.model_dump_json())
    service = AstPaperService(sessions, value)
    for index in range(240):
        price = 25000 + sign * ((index // 20) * 3 + (index % 20) / 100)
        observation = raw(index, price)
        observation = observation.model_copy(
            update={
                "source_market_timestamp": (
                    observation.captured_at + timedelta(seconds=shared_source_offset_seconds)
                    if shared_source_offset_seconds is not None
                    else None
                )
            }
        )
        capture(engine, observation)
    return service, value


def rows(service):
    with service.sessions() as session:
        return [(row.id, row.state, json.loads(row.document)) for row in session.scalars(select(AstTrade))]


def enter(engine, service, price=25100):
    first = capture(engine, raw(240, price))
    assert service.process_snapshot(first) == "PENDING_PAPER_ENTRY"
    assert rows(service)[0][1] == "PENDING_PAPER_ENTRY"
    assert "entry_price" not in rows(service)[0][2]
    assert service.process_snapshot(first) == "DUPLICATE"
    following = capture(engine, raw(241, price + 1))
    service.process_snapshot(following)
    assert rows(service)[0][1] == "OPEN"
    return first, following


@pytest.mark.parametrize("sign", [1, -1])
def test_next_observation_marks_excursions_and_session_exit(engine, sign):
    cfg = policy(regime={"mode": "NONE"})
    if sign < 0:
        cfg = cfg.model_copy(
            update={"execution": cfg.execution.model_copy(update={"round_trip_cost_per_lot_inr": 12})}
        )
    service, value = setup(engine, cfg, sign=sign)
    price = 25000 + 100 * sign
    first, following = enter(engine, service, price)
    service = AstPaperService(service.sessions, value)
    assert service.process_snapshot(following) == "DUPLICATE"
    for index, change in [(242, -10), (243, 15), (1461, 5)]:
        service.process_snapshot(capture(engine, raw(index, price + 1 + sign * change)))
    trade_id, state, doc = rows(service)[0]
    assert state == "CLOSED" and doc["exit_reason"] == "SESSION_END"
    assert doc["side"] == ("LONG" if sign > 0 else "SHORT")
    assert doc["decision_snapshot_id"] == first and doc["entry_snapshot_id"] == following
    assert doc["price_basis"] == "OBSERVED_FUTURES_LTP" and doc["entry_price"] == price + 1
    assert doc["mae_points"] == -10 and doc["mfe_points"] == 15
    assert doc["gross_points"] == 5 and doc["gross_pnl"] == 325
    assert doc["net_pnl"] == (None if sign > 0 else 313)
    assert doc["policy_hash"] == digest(doc["policy"])
    assert doc["strategy_version"] == "AST_FUTURES_V1"
    verify(service.sessions, full=True)
    assert report(service.sessions, raw(1461).captured_at, cfg)["realized_gross_pnl"] == 325
    from app.adaptive_supertrend.models import LegacyAstCursor, LegacyAstTrade, LegacyAstEvent
    from app.scalper.models import ScalperOptionQuoteRecord

    with service.sessions() as session:
        for model in (
            LegacyAstCursor,
            LegacyAstEvent,
            LegacyAstTrade,
            ActsPaperTrade,
            ActsPaperEvent,
            ParallelPaperTrade,
            ParallelPaperEvent,
            ScalperTrade,
            ScalperEvent,
            ScalperOptionQuoteRecord,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "update", [{"future_instrument_id": "NEXT"}, {"future_expiry": EXPIRY + timedelta(days=1)}, {"nifty_future": None}]
)
def test_pending_requires_exact_next_observed_futures_price(engine, update):
    service, _ = setup(engine)
    service.process_snapshot(capture(engine, raw(240)))
    service.process_snapshot(capture(engine, raw(241).model_copy(update=update)))
    _, state, doc = rows(service)[0]
    assert state == "ENTRY_REJECTED" and "entry_price" not in doc


def test_missed_session_exit_remains_unpriced(engine):
    service, _ = setup(engine)
    enter(engine, service)
    service.process_snapshot(capture(engine, raw(240 + 24 * 60 * 4)))
    _, state, doc = rows(service)[0]
    assert state == "UNRESOLVED_EXPOSURE" and doc["unresolved_reason"] == "MISSED_SESSION_EXIT"
    assert doc["gross_mark_rupees"] is None and "exit_price" not in doc
    verify(service.sessions, full=True)


def test_completed_reversal_exits_on_following_observation_after_restart(engine):
    service, value = setup(engine, policy(regime={"mode": "NONE"}))
    enter(engine, service)
    # A decline within the unfinished 10:15 bar cannot trigger a reversal.
    for index in range(242, 260):
        service.process_snapshot(capture(engine, raw(index, 24800)))
        assert rows(service)[0][1] == "OPEN"
    service.process_snapshot(capture(engine, raw(260, 24799)))
    _, state, doc = rows(service)[0]
    assert state == "PENDING_PAPER_EXIT" and "exit_price" not in doc
    assert doc["reversal_bar_at"] == raw(260).captured_at.isoformat()
    service = AstPaperService(service.sessions, value)
    service.process_snapshot(capture(engine, raw(261, 24790)))
    _, state, doc = rows(service)[0]
    assert state == "CLOSED" and doc["exit_reason"] == "COMPLETED_5M_REVERSAL"
    assert doc["exit_price"] == 24790 and doc["gross_points"] == 24790 - doc["entry_price"]
    verify(service.sessions, full=True)


def test_cannot_skip_next_bookless_observation_or_switch_active_policy(engine):
    service, value = setup(engine)
    service.process_snapshot(capture(engine, raw(240)))
    with pytest.raises(RuntimeError, match="ZERO_ACTIVE"):
        AstPaperService(
            service.sessions,
            value.model_copy(update={"ast_config_json": policy(regime={"mode": "NONE"}).model_dump_json()}),
        )
    first = capture(engine, raw(241).model_copy(update={"nifty_future": None}))
    later = capture(engine, raw(242))
    with pytest.raises(RuntimeError, match="EARLIER_OBSERVATION"):
        service.process_snapshot(later)
    assert rows(service)[0][1] == "PENDING_PAPER_ENTRY"
    service.process_snapshot(first)
    assert rows(service)[0][1] == "ENTRY_REJECTED"


def test_atomic_rollback_and_restart_catchup(engine, monkeypatch):
    import app.adaptive_supertrend.service as module

    service, value = setup(engine)
    first = capture(engine, raw(240))
    original = module.emit

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("TEST_STORAGE_FAILURE")

    monkeypatch.setattr(module, "emit", fail)
    with pytest.raises(RuntimeError, match="STORAGE_FAILURE"):
        service.process_snapshot(first)
    assert rows(service) == []
    with service.sessions() as session:
        assert session.get(AstCursor, 1).sequence == 0
    monkeypatch.setattr(module, "emit", original)
    service = AstPaperService(service.sessions, value)
    assert len(service.pending_snapshot_ids()) == 200
    assert first in service.pending_snapshot_ids(limit=300)
    assert service.process_snapshot(first) == "PENDING_PAPER_ENTRY"
    verify(service.sessions, full=True)


def test_readonly_status_disabled_default_no_network_and_paper_only(engine, monkeypatch, capsys):
    service, value = setup(engine)
    before = report(service.sessions, START, policy())
    monkeypatch.setattr(
        worker, "get_settings", lambda: value.model_copy(update={"database_url": SecretStr(str(engine.url))})
    )

    def forbidden(*args, **kwargs):
        pytest.fail("NETWORK OR SERVICE CONSTRUCTION")

    monkeypatch.setattr(worker, "make_reader", forbidden)
    monkeypatch.setattr(worker, "AstPaperService", forbidden)
    assert worker.main(["--mode", "PAPER", "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["strategy_version"] == "AST_FUTURES_V1"
    assert report(service.sessions, START, policy()) == before
    assert worker.main(["--mode", "PAPER"]) == 0
    with pytest.raises(SystemExit):
        worker.main(["--mode", "LIVE"])
    with pytest.raises(RuntimeError, match="CAPABILITY"):
        worker.paper_only_builder(SimpleNamespace(place_order=lambda: None))
    worker.paper_only_builder(lambda: None)
    assert worker.seconds(10) == "10,25,40,55"
    assert "place_order" not in inspect.getsource(worker)


def test_daily_freeze_immutable_no_intraday_reoptimization(engine):
    service, _ = setup(engine)
    with service.sessions() as session:
        frozen = json.loads(session.get(SessionParameters, START.date()).document)
    assert freeze(service.sessions, [], START.date(), START.replace(hour=12), policy()) == frozen
    assert frozen["data_cutoff"] < frozen["selected_at"]
    with pytest.raises(Exception, match="IMMUTABLE"):
        with service.sessions.begin() as session:
            session.get(SessionParameters, START.date()).document = "{}"
    changed = policy().model_copy(update={"regime": policy().regime.model_copy(update={"er_threshold": 0.1})})
    with pytest.raises(RuntimeError, match="DIFFERENT_POLICY"):
        freeze(service.sessions, [], START.date(), START.replace(hour=12), changed)
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OptimizerRun)) == 1


def test_policy_missing_capture_continues_without_entry(engine):
    from app.db.session import build_session_factory

    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(AstCursor(id=1))
    service = AstPaperService(sessions, settings(ast_config_json="{}"))
    service.process_snapshot(capture(engine, raw(80)))
    result = report(sessions, raw(80).captured_at, AstPolicy())
    assert result["status"] == "AST_POLICY_UNCONFIGURED"
    assert result["latest"]["blockers"][0] == "AST_POLICY_UNCONFIGURED"
    assert rows(service) == []


def test_journal_tamper_detection(engine):
    service, _ = setup(engine)
    enter(engine, service)
    # create_all fixture deliberately has no DB trigger; full verifier still detects raw corruption.
    with engine.begin() as connection:
        connection.execute(text("UPDATE ast_futures_paper_events SET payload='{}' WHERE sequence=1"))
    with pytest.raises(RuntimeError, match="JOURNAL_INVALID"):
        verify(service.sessions, full=True)


def test_shared_timestamp_missing_allows_pending_fill_and_foreign_mode_still_rejected(engine):
    service, _ = setup(engine)
    service.process_snapshot(capture(engine, raw(240)))
    later = raw(241).model_copy(update={"source_market_timestamp": None})
    service.process_snapshot(capture(engine, later))
    assert rows(service)[0][1] == "OPEN"
    assert rows(service)[0][2]["entry_price"] == later.nifty_future
    verify(service.sessions, full=True)
    foreign = ResearchSnapshotRepository(engine).save(raw(242))
    with pytest.raises(ValueError, match="FOREIGN_CAPTURE_MODE"):
        service.process_snapshot(foreign.snapshot_id)


def test_freeze_refuses_current_day_or_future_prices(engine):
    service, _ = setup(engine)
    next_day = START.date() + timedelta(days=1)
    at = datetime.combine(next_day, time(9), START.tzinfo)
    with pytest.raises(ValueError, match="CUTOFF"):
        freeze(service.sessions, bars(day=at.replace(minute=15)), next_day, at, policy())


def test_cursor_compare_and_swap_refuses_lost_lock_without_partial_evidence(engine):
    from sqlalchemy import event

    service, _ = setup(engine)
    first = capture(engine, raw(240))

    def lose_lock(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE ast_futures_paper_cursor SET lock_version="):
            statement += " AND 1=0"
        return statement, parameters

    event.listen(engine, "before_cursor_execute", lose_lock, retval=True)
    try:
        with pytest.raises(RuntimeError, match="CONCURRENT_WRITER"):
            service.process_snapshot(first)
        assert rows(service) == []
    finally:
        event.remove(engine, "before_cursor_execute", lose_lock)
    assert service.process_snapshot(first) == "PENDING_PAPER_ENTRY"
    verify(service.sessions, full=True)


@pytest.mark.parametrize("shared_source_offset_seconds", [None, -300, 300])
def test_shared_option_aggregate_never_determines_futures_signal_or_fill(engine, shared_source_offset_seconds):
    from app.adaptive_supertrend.market import history

    service, _ = setup(engine, shared_source_offset_seconds=shared_source_offset_seconds)
    data = history(service.sessions, raw(239).captured_at, 1, 45, ("FUT", EXPIRY))
    assert len(data) == 11 and all(bar.valid for bar in data)

    def observation(index):
        value = raw(index)
        return value.model_copy(
            update={
                "source_market_timestamp": (
                    value.captured_at + timedelta(seconds=shared_source_offset_seconds)
                    if shared_source_offset_seconds is not None
                    else None
                )
            }
        )

    first = observation(240)
    first_id = capture(engine, first)
    assert service.process_snapshot(first_id) == "PENDING_PAPER_ENTRY"
    doc = rows(service)[0][2]
    features = doc["strategy_features"]
    assert features["futures_source_timestamp"] is None
    assert features["futures_source_timestamp_status"] == "UNAVAILABLE_IN_SHARED_CAPTURE_SCHEMA"
    assert features["futures_observation_time"] == first.captured_at.isoformat()
    assert features["futures_observation_time_basis"] == "BROKER_RESPONSE_CAPTURE_TIME"
    assert features["snapshot_request_started_at"] == first.request_started_at.isoformat()
    assert features["snapshot_response_received_at"] == first.response_received_at.isoformat()
    assert "capture_source_timestamp" not in features
    status = report(service.sessions, first.captured_at, policy())
    assert status["latest"]["features"] == features
    from app.adaptive_supertrend.models import AstEvent

    with service.sessions() as session:
        event = session.scalar(select(AstEvent).where(AstEvent.event_type == "OBSERVATION"))
        assert json.loads(event.payload)["decision"]["features"] == features

    service.process_snapshot(capture(engine, observation(241)))
    assert rows(service)[0][1] == "OPEN"
    assert service.process_snapshot(first_id) == "DUPLICATE"
    verify(service.sessions, full=True)


def test_history_uses_capture_order_and_excludes_future_observations(engine):
    from app.adaptive_supertrend.market import history, pending_ids

    sessions = build_session_factory(engine)
    ids = {}
    # Insertion ids deliberately disagree with observation order.
    for index in reversed(range(20)):
        observation = raw(index, 25000 + index).model_copy(update={"source_market_timestamp": None})
        ids[index] = capture(engine, observation)
    at = START + timedelta(minutes=5)
    before = history(sessions, at, 1, 45, ("FUT", EXPIRY))
    assert len(before) == 1 and before[0].valid
    assert before[0].open == 25000 and before[0].close == 25019
    capture(engine, raw(40, 99999).model_copy(update={"source_market_timestamp": None}))
    assert history(sessions, at, 1, 45, ("FUT", EXPIRY)) == before
    assert all(bar.end <= at for bar in before)
    assert pending_ids(sessions, None)[:20] == [ids[index] for index in range(20)]


def test_history_contract_roll_still_invalidates_the_boundary(engine):
    from app.adaptive_supertrend.market import history

    sessions = build_session_factory(engine)
    for index in range(40):
        observation = raw(index).model_copy(
            update={"source_market_timestamp": None, "future_instrument_id": "FUT" if index < 20 else "NEXT"}
        )
        capture(engine, observation)
    data = history(sessions, START + timedelta(minutes=10), 1, 45)
    assert data[0].valid and not data[1].valid
    assert data[1].reason == "AST_FUTURES_CONTRACT_BOUNDARY"
    assert not history(sessions, START + timedelta(minutes=10), 1, 45, ("FUT", EXPIRY))[-1].valid


@pytest.mark.parametrize(
    "future_fields", [{"nifty_future": None}, {"future_instrument_id": None}, {"future_expiry": None}]
)
def test_futures_identity_expiry_and_price_remain_required_without_aggregate_timestamp(engine, future_fields):
    from app.adaptive_supertrend.market import history

    sessions = build_session_factory(engine)
    for index in range(20):
        capture(engine, raw(index).model_copy(update={"source_market_timestamp": None, **future_fields}))
    assert not history(sessions, START + timedelta(minutes=5), 1, 45)[0].valid


def test_history_does_not_use_a_response_recorded_after_capture(engine):
    from app.adaptive_supertrend.market import history

    sessions = build_session_factory(engine)
    for index in range(20):
        observation = raw(index)
        if index == 10:
            observation = observation.model_copy(
                update={"response_received_at": observation.captured_at + timedelta(seconds=1)}
            )
        capture(engine, observation)
    assert not history(sessions, START + timedelta(minutes=5), 1, 45)[0].valid


def test_pending_entry_cannot_fill_before_its_recorded_response(engine):
    service, _ = setup(engine)
    service.process_snapshot(capture(engine, raw(240)))
    later = raw(241).model_copy(update={"response_received_at": raw(241).captured_at + timedelta(seconds=1)})
    service.process_snapshot(capture(engine, later))
    assert rows(service)[0][1] == "ENTRY_REJECTED"
    assert rows(service)[0][2]["rejection_reason"] == "FUTURES_OBSERVATION_UNAVAILABLE"
    status = report(service.sessions, later.captured_at, policy())
    assert "AST_OBSERVATION_TIMING_INVALID" in status["latest"]["blockers"]


def test_live_freeze_cannot_finish_after_session_start(engine):
    service, _ = setup(engine)
    day = START.date() + timedelta(days=1)
    selected_at = datetime.combine(day, time(9), START.tzinfo)
    with pytest.raises(RuntimeError, match="COMPLETED_AFTER_CUTOFF"):
        freeze(service.sessions, bars(), day, selected_at, policy(), clock=lambda: selected_at.replace(minute=16))
    with service.sessions() as session:
        assert session.get(SessionParameters, day) is None


def test_immutable_event_orm_and_bounded_startup_queries(engine):
    from sqlalchemy import event
    from app.adaptive_supertrend.models import AstEvent

    service, value = setup(engine)
    enter(engine, service)
    with pytest.raises(Exception, match="IMMUTABLE"):
        with service.sessions.begin() as session:
            session.scalar(select(AstEvent).limit(1)).payload = "{}"
    queries = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if "FROM ast_futures_paper_events" in statement:
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        AstPaperService(service.sessions, value)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert queries and all("LIMIT" in statement for statement in queries)


@pytest.mark.parametrize("wrapped", [True, False])
def test_native_scalar_capture_without_option_books(engine, monkeypatch, wrapped):
    from app.adaptive_supertrend import capture as module
    from app.scalper.models import ScalperMarketSnapshotRecord as Snapshot, ScalperOptionQuoteRecord as Quote

    class ReadOnlyFixture:
        def quotes(self, **kwargs):
            assert kwargs["quote_type"] == "ltp"
            return {"data": [{"ltp": "25000"}]}

        def expiries(self, **kwargs):
            return {"expiries": ["2099-12-01"]}

        def option_chain(self, **kwargs):
            header = {
                "common_data": {"mktLot": "123"},
                "future": {"symbol": "NIFTY-TEST-FUT", "expiry": "2099-12-24", "ltp": "25015.25"},
            }
            return {"data": header} if wrapped else {**header, "data": None}

    monkeypatch.setattr(module, "create_market_data_client", lambda settings: ReadOnlyFixture())
    fields = module.make_reader(settings())()
    sessions = build_session_factory(engine)
    first = module.save_capture(sessions, fields)
    assert module.save_capture(sessions, fields) == first
    with sessions() as session:
        observation = session.get(Snapshot, first)
        assert observation.future_instrument_id == "NIFTY-TEST-FUT" and str(observation.future_expiry) == "2099-12-24"
        assert observation.nifty_future == 25015.25 and observation.source_market_timestamp is None
        assert observation.request_started_at <= observation.response_received_at == observation.captured_at
        assert observation.lot_size == 123  # raw header metadata only
        assert session.scalar(select(func.count()).select_from(Quote)) == 0


def test_retired_native_capture_is_history_only(engine):
    from app.adaptive_supertrend.market import history

    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(AstCursor(id=1))
    repo = ResearchSnapshotRepository(engine, mode="AST_PAPER")
    for index in range(20):
        saved = repo.save(raw(index))
    service = AstPaperService(sessions, settings(ast_config_json=policy().model_dump_json()))
    with pytest.raises(ValueError, match="FOREIGN_CAPTURE_MODE"):
        service.process_snapshot(saved.snapshot_id)
    assert not service.pending_snapshot_ids()
    assert history(sessions, START + timedelta(minutes=5), 1, 45)[0].valid


def test_invalid_forced_exit_and_unresolved_mark_are_unpriced(engine):
    service, _ = setup(engine)
    enter(engine, service)
    obs = raw(1460).model_copy(update={"nifty_future": None})
    service.process_snapshot(capture(engine, obs))
    _, state, doc = rows(service)[0]
    assert state == "UNRESOLVED_EXPOSURE"
    assert doc["unresolved_reason"] == "FUTURES_OBSERVATION_UNAVAILABLE"
    assert doc["mark_price"] is None and "exit_price" not in doc
    assert report(service.sessions, obs.captured_at)["unrealized_gross_pnl"] is None
    verify(service.sessions, full=True)
