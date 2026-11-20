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
from app.research.manifest import canonical, digest
from app.scalper.models import ScalperEvent, ScalperTrade
from app.scalper.research_capture import AST_PAPER_MODE, ResearchSnapshotRepository
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
    return ResearchSnapshotRepository(engine, mode=AST_PAPER_MODE).save(observation).snapshot_id


def setup(engine, cfg=None, *, shared_source_offset_seconds=0):
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
        price = 25000 + (index // 20) * 3 + (index % 20) / 100
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


def enter(engine, service):
    first = capture(engine, raw(240))
    assert service.process_snapshot(first) == "PENDING_PAPER_ENTRY"
    assert rows(service)[0][1] == "PENDING_PAPER_ENTRY"
    assert "entry_credit" not in rows(service)[0][2]
    assert service.process_snapshot(first) == "DUPLICATE"
    following = capture(engine, raw(241))
    service.process_snapshot(following)
    assert rows(service)[0][1] == "OPEN"
    return first, following


def test_fill_mark_forced_exit_restart_idempotency_and_isolation(engine):
    service, value = setup(engine)
    first, following = enter(engine, service)
    row = rows(service)[0][2]
    assert row["strategy_version"] == "AST_V1" and row["strategy_id"] == "AST_V1" and row["lots"] == 1
    assert row["session_parameters"]["atr_period"] >= 5
    assert row["candidate"]["construction_method"] == "SHORT_LEG_FIRST_V1"
    assert (
        row["entry_credit"] == row["entry_quotes"]["short"]["fill_price"] - row["entry_quotes"]["long"]["fill_price"]
    )
    assert row["entry_quotes"]["short"]["side"] == "bid" and row["entry_quotes"]["long"]["side"] == "ask"
    service = AstPaperService(service.sessions, value)
    assert service.process_snapshot(following) == "DUPLICATE"
    service.process_snapshot(capture(engine, raw(242)))
    assert len(rows(service)) == 1
    mark = rows(service)[0][2]
    assert (
        mark["current_debit"] == mark["mark_quotes"]["short"]["fill_price"] - mark["mark_quotes"]["long"]["fill_price"]
    )
    assert mark["mark_quotes"]["short"]["side"] == "ask" and mark["mark_quotes"]["long"]["side"] == "bid"
    assert len(service.required_contracts()) == 2
    forced = raw(1461)
    service.process_snapshot(capture(engine, forced))
    doc = rows(service)[0][2]
    assert rows(service)[0][1] == "CLOSED" and doc["exit_reason"] == "FORCED_CLOSE"
    assert doc["net_pnl"] is None and doc["final_outcome"]["cost_completeness"] == "GROSS_ONLY"
    assert doc["policy_hash"] == digest(doc["policy"])
    verify(service.sessions, full=True)
    heads = report(service.sessions, forced.captured_at, policy())
    assert heads["trades_today"] == 1 and heads["realized_gross_pnl"] == doc["gross_pnl"]
    assert service.pending_snapshot_ids() == []
    with service.sessions() as session:
        for model in (
            ActsPaperEvent,
            ActsPaperTrade,
            ParallelPaperEvent,
            ParallelPaperTrade,
            ScalperEvent,
            ScalperTrade,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_explicit_none_keeps_exact_paper_books_risk_and_journal(engine):
    cfg = policy(regime={"mode": "NONE"})
    service, value = setup(engine, cfg)
    _, filled = enter(engine, service)
    document = rows(service)[0][2]
    assert document["policy"]["regime"] == {"mode": "NONE"}
    assert document["policy_hash"] == cfg.policy_hash
    assert document["entry_quotes"]["short"]["side"] == "bid"
    assert document["entry_quotes"]["long"]["side"] == "ask"
    assert document["entry_credit"] == (
        document["entry_quotes"]["short"]["fill_price"] - document["entry_quotes"]["long"]["fill_price"]
    )
    restarted = AstPaperService(service.sessions, value)
    assert restarted.process_snapshot(filled) == "DUPLICATE"
    restarted.process_snapshot(capture(engine, raw(1461)))
    document = rows(restarted)[0][2]
    assert rows(restarted)[0][1] == "CLOSED" and document["exit_reason"] == "FORCED_CLOSE"
    assert document["mark_quotes"]["short"]["side"] == "ask"
    assert document["mark_quotes"]["long"]["side"] == "bid"
    assert document["net_pnl"] is None
    assert cfg.execution == policy().execution
    verify(restarted.sessions, full=True)


def test_persistent_opportunity_consumed_after_closed_and_no_spam(engine):
    cfg = policy()
    execution = cfg.execution.model_copy(
        update={
            "safety": cfg.execution.safety.model_copy(
                update={
                    "label": "LEGACY_SAFETY_BASELINE",
                    "profit_capture_pct": 50,
                    "stop_credit_multiple": 1.5,
                    "trailing_activation_pct": 35,
                    "trailing_giveback_pct": 20,
                    "time_stop_minutes": 1,
                }
            )
        }
    )
    cfg = cfg.model_copy(update={"execution": execution})
    service, _ = setup(engine, cfg)
    enter(engine, service)
    for index in range(242, 252):
        service.process_snapshot(capture(engine, raw(index)))
    assert len(rows(service)) == 1 and rows(service)[0][1] == "CLOSED"
    assert rows(service)[0][2]["exit_reason"] == "LEGACY_TIME_STOP"
    with service.sessions() as session:
        assert "AST_OPPORTUNITY_CONSUMED" in json.loads(session.get(AstCursor, 1).latest_json)["blockers"]
    verify(service.sessions, full=True)


@pytest.mark.parametrize(
    "problem", ["missing", "stale", "timestamp_missing", "depth", "ltp_only", "token", "strike", "expiry"]
)
def test_pending_exact_execution_never_fabricates(engine, problem):
    service, _ = setup(engine)
    service.process_snapshot(capture(engine, raw(240)))
    doc = rows(service)[0][2]
    token = doc["candidate"]["long_leg"]["instrument_token"]
    following = raw(241)
    quotes = []
    for quote in following.quotes:
        if quote.instrument_token == token:
            if problem == "missing":
                continue
            update = {
                "stale": {"source_market_timestamp": following.captured_at - timedelta(minutes=2)},
                "timestamp_missing": {"source_market_timestamp": None},
                "depth": {"ask_quantity": 0},
                "ltp_only": {"bid": None, "ask": None},
                "token": {"instrument_token": "WRONG"},
                "strike": {"strike": quote.strike + 1},
                "expiry": {"expiry": quote.expiry + timedelta(days=1)},
            }[problem]
            quote = quote.model_copy(update=update)
        quotes.append(quote)
    # Mixed-expiry malformed capture is rejected by the existing domain model before persistence.
    if problem == "expiry":
        with pytest.raises(ValueError, match="EXPIRY_MISMATCH"):
            capture(engine, following.model_validate({**following.model_dump(), "quotes": quotes}))
        assert rows(service)[0][1] == "PENDING_PAPER_ENTRY"
        return
    service.process_snapshot(
        capture(engine, following.model_copy(update={"quotes": quotes, "source_market_timestamp": None}))
    )
    assert rows(service)[0][1] == "ENTRY_REJECTED" and "entry_credit" not in rows(service)[0][2]
    if problem in {"stale", "timestamp_missing"}:
        assert rows(service)[0][2]["rejection_reason"] == (
            "STALE_OR_FUTURE_QUOTE" if problem == "stale" else "MISSING_QUOTE_TIMESTAMP"
        )
    verify(service.sessions, full=True)


def test_missing_held_contract_unresolved_and_no_new_atm_substitution(engine):
    service, value = setup(engine)
    enter(engine, service)
    doc = rows(service)[0][2]
    token = doc["candidate"]["long_leg"]["instrument_token"]
    later = raw(242)
    service.process_snapshot(
        capture(engine, later.model_copy(update={"quotes": [q for q in later.quotes if q.instrument_token != token]}))
    )
    assert rows(service)[0][1] == "UNRESOLVED_EXPOSURE"
    assert rows(service)[0][2]["unresolved_reason"] == "MISSING_EXACT_CONTRACT"
    service = AstPaperService(service.sessions, value)
    service.process_snapshot(capture(engine, raw(243)))
    assert rows(service)[0][1] == "UNRESOLVED_EXPOSURE" and len(rows(service)) == 1
    verify(service.sessions, full=True)


def test_pending_ttl_and_fresh_policy_guards(engine):
    service, value = setup(engine)
    service.process_snapshot(capture(engine, raw(240)))
    changed = policy().model_copy(update={"regime": policy().regime.model_copy(update={"er_threshold": 0.1})})
    with pytest.raises(RuntimeError, match="ZERO_ACTIVE"):
        AstPaperService(service.sessions, value.model_copy(update={"ast_config_json": changed.model_dump_json()}))
    service.process_snapshot(capture(engine, raw(243)))
    assert rows(service)[0][2]["rejection_reason"] == "ENTRY_TTL_EXCEEDED"


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

    monkeypatch.setattr(worker, "make_live_snapshot_builder", forbidden)
    monkeypatch.setattr(worker, "AstPaperService", forbidden)
    assert worker.main(["--mode", "PAPER", "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["strategy_version"] == "AST_V1"
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
        connection.execute(text("UPDATE ast_paper_events SET payload='{}' WHERE sequence=1"))
    with pytest.raises(RuntimeError, match="JOURNAL_INVALID"):
        verify(service.sessions, full=True)


def test_execution_replay_deterministic_and_exit_policy_comparison(tmp_path):
    from app.adaptive_supertrend.replay import replay

    prior = bars(day=START - timedelta(days=1))
    source = tmp_path / "observations.jsonl"
    observations = [raw(index, 25000 + (index // 20) * 3 + (index % 20) / 100) for index in range(240)]
    observations += [raw(240), raw(241), raw(1461)]
    source.write_text("\n".join(row.model_dump_json() for row in observations) + "\n")
    original = source.read_bytes()
    first = replay(prior, source, policy())
    assert first == replay(prior, source, policy())
    assert first["result_layer"] == "EXECUTABLE_OPTION_REPLAY" and first["journal_valid"]
    assert first["gross_option_pnl"] is not None and first["net_option_pnl"] is None
    assert len(first["trades"]) == 1 and first["trades"][0]["exit_reason"] == "FORCED_CLOSE"
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "risk,expected",
    [
        ({"kill_switch": True}, "KILL_SWITCH_ACTIVE"),
        ({"max_trades_per_day": 1}, "MAX_TRADES_PER_DAY_REACHED"),
        ({"max_consecutive_losses": 1}, "MAX_CONSECUTIVE_LOSSES_REACHED"),
        ({"hard_daily_loss": 1}, "HARD_DAILY_LOSS_REACHED"),
        ({"cooldown_after_loss_seconds": 60}, "LOSS_COOLDOWN_ACTIVE"),
        ({"cooldown_after_exit_seconds": 60}, "ANY_EXIT_COOLDOWN_ACTIVE"),
    ],
)
def test_explicit_risk_guards_remain_enforced(engine, risk, expected):
    cfg = policy()
    cfg = cfg.model_copy(update={"execution": cfg.execution.model_copy(update=risk)})
    service, _ = setup(engine, cfg)
    at = raw(242).captured_at
    row = SimpleNamespace(
        id="prior",
        state="CLOSED",
        document=canonical(
            {
                "entry_timestamp": raw(240).captured_at.isoformat(),
                "exit_timestamp": raw(241).captured_at.isoformat(),
                "gross_pnl": -10,
            }
        ),
    )
    assert expected in service._risk([row], at, False)
    assert "MARKET_EVENT_BLOCKED" in service._risk([], at, True)
    assert "OUTSIDE_ENTRY_WINDOW" in service._risk([], at.replace(hour=15), False)


def test_unresolved_blocks_position_and_expiry_never_invents_exit(engine):
    service, _ = setup(engine)
    enter(engine, service)
    row = rows(service)[0]
    # Use the actual stored expiry, retain original contract identity and miss all expiry-window data.
    expiry = datetime.fromisoformat(row[2]["candidate"]["short_leg"]["expiry"]).date()
    later = raw(242)
    at = datetime.combine(expiry + timedelta(days=1), time(10), START.tzinfo)
    later = later.model_copy(
        update={"captured_at": at, "request_started_at": at, "response_received_at": at, "source_market_timestamp": at}
    )
    service.process_snapshot(capture(engine, later))
    doc = rows(service)[0][2]
    assert rows(service)[0][1] == "UNRESOLVED_EXPOSURE"
    assert "exit_debit" not in doc and "final_outcome" not in doc
    with service.sessions() as session:
        active = session.scalar(select(AstTrade))
        assert "MAX_OPEN_POSITIONS_REACHED" in service._risk([active], at, False)


def test_shared_timestamp_missing_allows_pending_fill_and_foreign_mode_still_rejected(engine):
    service, _ = setup(engine)
    service.process_snapshot(capture(engine, raw(240)))
    later = raw(241).model_copy(update={"source_market_timestamp": None})
    service.process_snapshot(capture(engine, later))
    assert rows(service)[0][1] == "OPEN"
    assert rows(service)[0][2]["entry_quotes"]["short"]["quote"]["source_market_timestamp"] is not None
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
        if statement.startswith("UPDATE ast_paper_cursor SET lock_version="):
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
    assert rows(service)[0][2]["rejection_reason"] == "AST_PENDING_THESIS_INVALID"
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
        if "FROM ast_paper_events" in statement:
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        AstPaperService(service.sessions, value)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert queries and all("LIMIT" in statement for statement in queries)


def test_emergency_kill_is_only_allowed_active_policy_tightening(engine):
    service, value = setup(engine)
    enter(engine, service)
    frozen_hash = rows(service)[0][2]["policy_hash"]
    cfg = policy()
    killed = cfg.model_copy(update={"execution": cfg.execution.model_copy(update={"kill_switch": True})})
    service = AstPaperService(service.sessions, value.model_copy(update={"ast_config_json": killed.model_dump_json()}))
    service.process_snapshot(capture(engine, raw(242)))
    doc = rows(service)[0][2]
    assert rows(service)[0][1] == "CLOSED" and doc["exit_reason"] == "KILL_SWITCH"
    assert doc["policy_hash"] == frozen_hash and doc["policy"]["execution"]["kill_switch"] is False
    assert doc["emergency_safety_override"]["applied_policy_hash"] == killed.policy_hash
    verify(service.sessions, full=True)


def test_previous_session_close_is_loaded_for_configured_overnight_cooldown(engine):
    cfg = policy()
    cfg = cfg.model_copy(
        update={"execution": cfg.execution.model_copy(update={"cooldown_after_loss_seconds": 100000})}
    )
    service, _ = setup(engine, cfg)
    enter(engine, service)
    service.process_snapshot(capture(engine, raw(1461)))
    tomorrow = START + timedelta(days=1)
    with service.sessions() as session:
        records = service._rows(session, tomorrow.replace(hour=10))
        assert len(records) == 1 and records[0].state == "CLOSED"


def test_explicit_dated_json_costs_produce_net_without_altering_fill_prices(engine):
    cfg = policy(
        costs={
            "cost_schedule_version": "SYNTHETIC_EXPLICIT_COSTS",
            "effective_from": "2026-11-01",
            "effective_to": "2026-11-30",
            "declared_complete": True,
            "brokerage_per_order": 20,
            "exchange_rate": 0,
            "stt_rate": 0,
            "gst_rate": 0,
            "stamp_rate": 0,
            "sebi_rate": 0,
            "slippage_points_per_leg": 0,
        }
    )
    service, _ = setup(engine, cfg)
    enter(engine, service)
    service.process_snapshot(capture(engine, raw(1461)))
    doc = rows(service)[0][2]
    assert doc["final_outcome"]["cost_completeness"] == "NET_COMPLETE"
    assert doc["net_pnl"] == doc["gross_pnl"] - 80
    assert (
        doc["entry_credit"] == doc["entry_quotes"]["short"]["fill_price"] - doc["entry_quotes"]["long"]["fill_price"]
    )
    assert service.costs.effective_from.isoformat() == "2026-11-01"
    verify(service.sessions, full=True)
