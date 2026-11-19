"""Database execution, restart, safety and version separation for opt-in V3."""

from copy import deepcopy
from datetime import datetime
import json
import socket

import pytest
from app.acts_paper import acts_v2, acts_v3
from app.acts_paper.models import ActsPaperCursor, ActsPaperTrade
from app.acts_paper.service import ActsPaperService, verify_acts_paper_journal
from app.acts_paper.service_v3 import ActsV3PaperService
from app.db.session import build_session_factory
from app.paper import exit_book, mirror_exit
from app.parallel_paper.monitor import summary
from app.parallel_paper.report import report
from app.parallel_paper.trade_monitor import trades
from app.research.manifest import digest
from app.scalper.models import ScalperCandidate
from app.scalper_lab.engine import LabTrade, Ledger, _risk_blockers
from tests.test_acts_paper import _capture, _next, _seed_position
from tests.test_acts_paper_expiry import records
from tests.test_acts_paper_trade_cap import closed, qualified
from tests.test_acts_paper_v2 import _decision
from tests.test_mirror_exit_v1 import mark_raw, open_acts, stored
from tests.test_scalper import settings
from tests.test_scalper_acts import qualified_rows


def v3_settings(**overrides):
    return settings(acts_paper_enabled=True, acts_paper_strategy_version="ACTS_V3", **overrides)


def service(engine, **overrides):
    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(
            ActsPaperCursor(id=1, sequence=0, head_hash="GENESIS", lock_version=0, watch_json="{}", latest_json="{}")
        )
    return ActsV3PaperService(sessions, v3_settings(**overrides))


def pending(paper, engine):
    for row in qualified_rows(1, 70, "NEUTRAL"):
        snapshot_id = _capture(engine, row.snapshot)
    assert paper.process_snapshot(snapshot_id) == "PENDING_PAPER_ENTRY"
    doc = stored(paper, ActsPaperTrade)[1]
    return row.snapshot, ScalperCandidate.model_validate(doc["candidate"])


def opened(engine):
    paper = service(engine)
    raw, candidate = pending(paper, engine)
    observation = _next(raw, candidate=candidate)
    call = next(quote for quote in observation.quotes if quote.option_type == "CE")
    observation = observation.model_copy(
        update={
            "quotes": [
                *observation.quotes,
                call.model_copy(
                    update={
                        "strike": 25650,
                        "instrument_token": "25650CE",
                        "trading_symbol": "NIFTY25650CE",
                        "bid": 15,
                        "ask": 16,
                        "ltp": 15.5,
                    }
                ),
            ]
        }
    )
    paper.process_snapshot(_capture(engine, observation))
    assert stored(paper, ActsPaperTrade)[0] == "OPEN"
    return paper, observation, candidate


def signal_context(paper, monkeypatch, direction=-1):
    ctx = _decision(direction)[0]

    def context(observation):
        result = deepcopy(ctx)
        raw = observation.snapshot
        result.update(
            timestamp=raw.captured_at.isoformat(),
            snapshot_id=observation.snapshot_id,
            future_instrument_id=raw.future_instrument_id,
            future_expiry=raw.future_expiry,
            expiry_date=raw.expiry,
        )
        return result

    monkeypatch.setattr(paper, "_context", context)


def fixed_score(monkeypatch, score):
    original = acts_v2._score

    def fixed(*args):
        _, components, evidence = original(*args)
        return score, components, evidence

    monkeypatch.setattr(acts_v2, "_score", fixed)


def test_v3_real_next_observation_execution_policy_and_restart(engine, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("paper service must not call network or broker adapters")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    paper = service(engine)
    raw, candidate = pending(paper, engine)
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "PENDING_PAPER_ENTRY" and doc.get("entry_timestamp") is None
    assert candidate.spread_width == 400 and paper.config.allowed_widths == (400,)
    assert paper._construction_strategy() == "ACTS_V2"
    assert doc["strategy_version"] == "ACTS_V3"
    assert doc["opportunity_cycle_policy_version"] == acts_v3.POLICY_VERSION
    assert doc["cycle_id"] and doc["cycle_state"] == "ENTERED"
    assert doc["policy_hash"] == digest(doc["policy"]) == paper.policy_hash
    assert doc["policy"]["opportunity_cycle_policy"] == acts_v3.PARAMETERS
    assert doc["policy"]["exit_policy"] == mirror_exit.acts_policy(paper.config)
    assert doc["policy"]["exit_book_policy"] == exit_book.policy()
    v2 = ActsPaperService(paper.sessions, settings())
    assert paper.policy_hash != v2.policy_hash
    assert paper.config == v2.config and paper.v2_config == v2.v2_config
    assert v2.v2_config.tradeable_score == 55
    paper = ActsV3PaperService(paper.sessions, v3_settings())
    before = records(paper.sessions)
    assert paper.process_snapshot(doc["decision_snapshot_id"]) == "DUPLICATE"
    assert records(paper.sessions) == before
    following = _next(raw, candidate=candidate)
    paper.process_snapshot(_capture(engine, following))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "OPEN" and doc["entry_timestamp"] == following.captured_at.isoformat()
    assert doc["entry_quotes"]["short"]["side"] == "bid"
    assert doc["entry_quotes"]["long"]["side"] == "ask"
    assert doc["entry_credit"] == pytest.approx(
        doc["entry_quotes"]["short"]["fill_price"] - doc["entry_quotes"]["long"]["fill_price"]
    )
    restart = ActsV3PaperService(paper.sessions, v3_settings())
    assert stored(restart, ActsPaperTrade) == (state, doc)
    assert restart.required_contracts(following.captured_at)
    assert restart.status(following.captured_at)["opportunity_cycle"]["consumed_directions"] == ["BULL"]
    verify_acts_paper_journal(paper.sessions)


def test_v3_does_not_raise_mirror_exit_floor_or_grant_reverse_entry(engine, monkeypatch):
    paper, raw, candidate = opened(engine)
    fixed_score(monkeypatch, 59)
    signal_context(paper, monkeypatch)
    credit = stored(paper, ActsPaperTrade)[1]["entry_credit"]
    observation = mark_raw(raw, candidate, 15, credit)
    paper.process_snapshot(_capture(engine, observation))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "CLOSED" and doc["exit_reason"] == mirror_exit.ACTS_REASON
    assert doc["mirror_exit_latest"]["opportunity_score"] == 59
    assert doc["exit_quotes"]["short"]["side"] == "ask"
    assert doc["exit_quotes"]["long"]["side"] == "bid"
    assert doc["cycle_state"] == "CONSUMED" and doc["previous_direction"] == "BULL"
    assert doc["previous_exit_timestamp"] == observation.captured_at.isoformat()
    assert doc["opportunity_cycle_entry"]["rearm_reason"] == "FIRST_OPPORTUNITY"
    assert len(records(paper.sessions)[0]) == 1
    status = paper.status(observation.captured_at)
    assert "MIRROR_EXIT_OBSERVATION_NO_REENTRY" in status["latest_observation"]["hard_blockers"]
    assert "V3_SCORE_FLOOR" in status["latest_observation"]["hard_blockers"]
    verify_acts_paper_journal(paper.sessions)


def test_persisted_reversal_confirmation_survives_restart_and_obeys_exit_cooldown(engine, monkeypatch):
    paper, raw, candidate = opened(engine)
    fixed_score(monkeypatch, 85)
    signal_context(paper, monkeypatch)
    credit = stored(paper, ActsPaperTrade)[1]["entry_credit"]
    for seconds in (15, 30, 45, 60):
        observation = mark_raw(raw, candidate, seconds, credit)
        assert paper.process_snapshot(_capture(engine, observation)) == "NO_SETUP"
        status = paper.status(observation.captured_at)
        assert status["opportunity_cycle"]["reversal_confirmation_seconds"] == seconds - 15
        assert len(records(paper.sessions)[0]) == 1
        verify_acts_paper_journal(paper.sessions)
        paper = ActsV3PaperService(paper.sessions, v3_settings())
        signal_context(paper, monkeypatch)
    observation = mark_raw(raw, candidate, 75, credit)
    assert paper.process_snapshot(_capture(engine, observation)) == "PENDING_PAPER_ENTRY"
    docs = [json.loads(item[2]) for item in records(paper.sessions)[0]]
    opposite = next(doc for doc in docs if doc["direction"] == "BEAR")
    assert opposite["rearm_reason"] == "EXCEPTIONAL_REVERSAL_CONFIRMATION"
    assert opposite["reversal_confirmation_seconds"] == 60
    assert opposite["previous_exit_reason"] == mirror_exit.ACTS_REASON
    assert opposite["cycle_id"] != next(doc for doc in docs if doc["direction"] == "BULL")["cycle_id"]
    verify_acts_paper_journal(paper.sessions)


@pytest.mark.parametrize("state", ["PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE"])
def test_v3_refuses_active_prior_version_without_mutating_history(engine, state):
    paper, raw, candidate = open_acts(engine)
    if state == "UNRESOLVED_EXPOSURE":
        paper.process_snapshot(_capture(engine, _next(raw, candidate=candidate, missing="long_leg")))
    elif state == "PENDING_PAPER_ENTRY":
        # Close normally, then obtain a genuine next-session pending decision.
        forced = raw.captured_at.replace(hour=15, minute=20, second=0)
        paper.process_snapshot(
            _capture(engine, mark_raw(raw, candidate, int((forced - raw.captured_at).total_seconds()), 40))
        )
        next_day = _next(raw, seconds=86400)
        ctx = deepcopy(_decision()[0])
        ctx["timestamp"] = next_day.captured_at.isoformat()
        paper._context = lambda _: deepcopy(ctx)
        assert paper.process_snapshot(_capture(engine, next_day)) == "PENDING_PAPER_ENTRY"
    assert any(row[1] == state for row in records(paper.sessions)[0])
    before = records(paper.sessions)
    with pytest.raises(RuntimeError, match="ZERO_PRIOR_VERSION_EXPOSURE"):
        ActsV3PaperService(paper.sessions, v3_settings())
    assert records(paper.sessions) == before
    verify_acts_paper_journal(paper.sessions)


def test_v3_refuses_even_expired_v1_unresolved_exposure(engine):
    paper, _, _, _ = _seed_position(engine)
    before = records(paper.sessions)
    with pytest.raises(RuntimeError, match="ZERO_PRIOR_VERSION_EXPOSURE"):
        ActsV3PaperService(paper.sessions, v3_settings())
    assert records(paper.sessions) == before


def test_v3_active_policy_upgrade_refuses_without_mutation(engine):
    paper, _, _ = opened(engine)
    before = records(paper.sessions)
    with pytest.raises(RuntimeError, match="ZERO_ACTIVE_TRADES"):
        ActsV3PaperService(paper.sessions, v3_settings(scalper_max_trades_per_day=5))
    assert records(paper.sessions) == before
    verify_acts_paper_journal(paper.sessions)


def test_v3_missing_exact_hedge_remains_unresolved_and_blocks_entries(engine):
    paper, raw, candidate = opened(engine)
    absent = _next(raw, candidate=candidate, missing="long_leg")
    assert paper.process_snapshot(_capture(engine, absent)) == "NO_SETUP"
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "UNRESOLVED_EXPOSURE" and doc["unresolved_reason"] == "MISSING_EXACT_CONTRACT"
    assert doc.get("exit_debit") is None and doc.get("final_outcome") is None
    assert "MAX_OPEN_POSITIONS_REACHED" in paper.status(absent.captured_at)["latest_observation"]["hard_blockers"]
    restarted = ActsV3PaperService(paper.sessions, v3_settings())
    assert stored(restarted, ActsPaperTrade) == (state, doc)
    verify_acts_paper_journal(paper.sessions)


def test_v3_unfilled_expired_reservation_does_not_consume_opportunity(engine):
    paper = service(engine)
    raw, candidate = pending(paper, engine)
    delayed = _next(raw, seconds=paper.config.entry_ttl_seconds + 15, candidate=candidate)
    paper.process_snapshot(_capture(engine, delayed))
    rows = records(paper.sessions)[0]
    rejected = next(json.loads(doc) for _, state, doc in rows if state == "ENTRY_REJECTED")
    assert rejected["rejection_reason"] == "ENTRY_TTL_EXCEEDED"
    assert rejected["cycle_state"] == "ARMED"
    assert paper.status(delayed.captured_at)["opportunity_cycle"]["consumed_directions"] == []
    verify_acts_paper_journal(paper.sessions)


def test_v3_forced_exit_is_still_1520_and_never_synthesizes_price(engine):
    paper, raw, candidate = opened(engine)
    forced = raw.captured_at.replace(hour=15, minute=20, second=0)
    observation = mark_raw(raw, candidate, int((forced - raw.captured_at).total_seconds()), 40)
    paper.process_snapshot(_capture(engine, observation))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "CLOSED" and doc["exit_reason"] == "FORCED_CLOSE"
    assert doc["exit_timestamp"] == forced.isoformat()
    assert doc["exit_debit"] == pytest.approx(40)
    assert doc["previous_exit_reason"] == "FORCED_CLOSE"
    verify_acts_paper_journal(paper.sessions)


def test_v3_stop_cannot_retry_plain_same_direction_after_loss_cooldown(engine, monkeypatch):
    paper, raw, candidate = opened(engine)
    signal_context(paper, monkeypatch, direction=1)
    credit = stored(paper, ActsPaperTrade)[1]["entry_credit"]
    stopped = mark_raw(raw, candidate, 15, credit * paper.config.stop_credit_multiple + 1)
    paper.process_snapshot(_capture(engine, stopped))
    assert stored(paper, ActsPaperTrade)[0] == "CLOSED"
    assert stored(paper, ActsPaperTrade)[1]["exit_reason"] in {"SPREAD_STOP", "HARD_MAX_LOSS"}
    following = mark_raw(raw, candidate, 615, credit)
    assert paper.process_snapshot(_capture(engine, following)) == "NO_SETUP"
    latest = paper.status(following.captured_at)["latest_observation"]
    assert "CONSUMED_CYCLE" in latest["hard_blockers"]
    assert "LOSS_COOLDOWN_ACTIVE" not in latest["hard_blockers"]
    assert len(records(paper.sessions)[0]) == 1
    verify_acts_paper_journal(paper.sessions)


def test_v3_failed_cycle_event_rolls_back_fill_and_cursor_atomically(engine, monkeypatch):
    paper = service(engine)
    raw, candidate = pending(paper, engine)
    before = records(paper.sessions)
    original = paper._emit

    def fail(session, cursor, kind, *args, **kwargs):
        if kind == "ACTS_OPPORTUNITY_CYCLE":
            raise RuntimeError("injected journal failure")
        return original(session, cursor, kind, *args, **kwargs)

    monkeypatch.setattr(paper, "_emit", fail)
    snapshot_id = _capture(engine, _next(raw, candidate=candidate))
    with pytest.raises(RuntimeError, match="injected journal failure"):
        paper.process_snapshot(snapshot_id)
    assert records(paper.sessions) == before
    verify_acts_paper_journal(paper.sessions)
    restarted = ActsV3PaperService(paper.sessions, v3_settings())
    restarted.process_snapshot(snapshot_id)
    assert stored(restarted, ActsPaperTrade)[0] == "OPEN"
    verify_acts_paper_journal(paper.sessions)


def test_v3_worker_selects_version_explicitly_and_remains_paper_only(engine, monkeypatch, capsys):
    from pydantic import SecretStr
    from worker import acts_paper_worker as worker

    paper = service(engine)
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: v3_settings().model_copy(update={"database_url": SecretStr("sqlite:///disposable")}),
    )
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
    monkeypatch.setattr(worker, "build_engine", lambda *_: engine)
    monkeypatch.setattr(worker, "build_session_factory", lambda *_: paper.sessions)

    def forbidden(*args, **kwargs):
        raise AssertionError("V3 status must not capture or instantiate V2")

    monkeypatch.setattr(worker, "ActsPaperService", forbidden)
    monkeypatch.setattr(worker, "make_live_snapshot_builder", forbidden)
    assert worker.main(["--mode", "PAPER", "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["strategy_version"] == "ACTS_V3"
    with pytest.raises(SystemExit):
        worker.main(["--mode", "LIVE"])


@pytest.mark.parametrize("completed", range(7))
def test_v3_six_trade_emergency_ceiling_is_not_reset_by_new_cycles(engine, completed):
    paper = service(engine)
    ctx, candidate, rejected = qualified(paper)
    at = datetime.fromisoformat(ctx["timestamp"])
    ledger = Ledger(trades=[closed(candidate, at, index) for index in range(completed)])
    risk = _risk_blockers(ledger, paper.config, at, False)
    decision = acts_v3.decide(ctx, {}, candidate, rejected, risk, paper.v2_config)
    assert decision["status"] == ("ENTER" if completed < 6 else "NO_SETUP")
    assert ("MAX_TRADES_PER_DAY_REACHED" in decision["hard_blockers"]) == (completed == 6)
    assert paper.policy["config"]["max_trades_per_day"] == 6


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("PENDING_ENTRY", "MAX_OPEN_POSITIONS_REACHED"),
        ("OPEN", "MAX_OPEN_POSITIONS_REACHED"),
        ("UNRESOLVED", "MAX_OPEN_POSITIONS_REACHED"),
        ("exit", "ANY_EXIT_COOLDOWN_ACTIVE"),
        ("loss", "LOSS_COOLDOWN_ACTIVE"),
        ("streak", "MAX_CONSECUTIVE_LOSSES_REACHED"),
        ("daily", "HARD_DAILY_LOSS_REACHED"),
    ],
)
def test_v3_real_portfolio_risk_gates_still_block_entry(engine, kind, reason):
    paper = service(engine)
    ctx, candidate, rejected = qualified(paper)
    at = datetime.fromisoformat(ctx["timestamp"])
    if kind in {"PENDING_ENTRY", "OPEN", "UNRESOLVED"}:
        ledger = Ledger(trades=[LabTrade("ACTS_V3", candidate, {"timestamp": at.isoformat()}, 1, state=kind)])
    elif kind == "streak":
        ledger = Ledger(
            trades=[closed(candidate, at, index, -1) for index in range(paper.config.max_consecutive_losses)]
        )
    else:
        pnl = -paper.config.hard_daily_loss if kind == "daily" else -1 if kind == "loss" else 1
        ledger = Ledger(trades=[closed(candidate, at, 1, pnl, recent=kind != "daily")])
    blockers = _risk_blockers(ledger, paper.config, at, False)
    assert reason in blockers
    decision = acts_v3.decide(ctx, {}, candidate, rejected, blockers, paper.v2_config)
    assert decision["status"] != "ENTER" and reason in decision["hard_blockers"]


@pytest.mark.parametrize("configured,expected", [(0, 0), (2, 2), (6, 6), (10, 6)])
def test_v3_ceiling_does_not_raise_a_lower_configured_cap(engine, configured, expected):
    paper = service(engine, scalper_max_trades_per_day=configured)
    assert paper.config.max_trades_per_day == expected
    assert paper.policy["config"]["max_trades_per_day"] == expected


def test_closed_v2_evidence_is_untouched_and_reports_separate_versions(engine, monkeypatch):
    v2, raw, candidate = open_acts(engine)
    forced = raw.captured_at.replace(hour=15, minute=20, second=0)
    v2.process_snapshot(
        _capture(engine, mark_raw(raw, candidate, int((forced - raw.captured_at).total_seconds()), 40))
    )
    before_rows, before_events = records(v2.sessions)
    paper = ActsV3PaperService(v2.sessions, v3_settings())
    signal_context(paper, monkeypatch, direction=1)
    following = _next(raw, seconds=86400)
    assert paper.process_snapshot(_capture(engine, following)) == "PENDING_PAPER_ENTRY"
    after_rows, after_events = records(paper.sessions)
    assert all(row in after_rows for row in before_rows)
    assert after_events[: len(before_events)] == before_events
    report_before = records(paper.sessions)
    performance = report(paper.sessions, following.captured_at)["strategies"]
    assert performance["ACTS_V2"]["closed_trades"] == 1
    assert performance["ACTS_V3"]["closed_trades"] == 0
    assert performance["ACTS_V3"]["open_position"][0]["cycle_id"]
    projected = summary(paper.sessions, following.captured_at)
    by_version = {item["strategy_id"]: item for item in projected["strategies"]}
    assert by_version["ACTS_V2"]["strategy_id"] == "ACTS_V2"
    assert by_version["ACTS_V3"]["latest_timestamp"] == following.captured_at.isoformat()
    page = trades(paper.sessions, following.captured_at)
    assert {item["strategy_id"] for item in page["items"]} == {"ACTS_V2", "ACTS_V3"}
    v3_page = trades(paper.sessions, following.captured_at, strategy="ACTS_V3")
    assert len(v3_page["items"]) == 1 and v3_page["items"][0]["cycle_id"]
    assert records(paper.sessions) == report_before
    verify_acts_paper_journal(paper.sessions)


def test_v3_status_does_not_label_same_day_v2_profit_as_v3(engine, monkeypatch):
    v2, raw, candidate = open_acts(engine)
    credit = stored(v2, ActsPaperTrade)[1]["entry_credit"]
    loss = mark_raw(raw, candidate, 15, credit * v2.config.stop_credit_multiple + 1)
    v2.process_snapshot(_capture(engine, loss))
    assert stored(v2, ActsPaperTrade)[0] == "CLOSED"
    old = records(v2.sessions)
    paper = ActsV3PaperService(v2.sessions, v3_settings())
    signal_context(paper, monkeypatch, direction=1)
    observation = _next(raw, seconds=615)
    assert paper.process_snapshot(_capture(engine, observation)) == "PENDING_PAPER_ENTRY"
    current = paper.status(observation.captured_at)
    assert current["realized_gross_pnl"] == 0
    assert current["total_gross_pnl"] is None  # Pending is not an executed fill.
    assert current["entries_remaining_today"] == 5  # The V2 fill still counts for risk.
    assert current["risk_budget_scope"] == "ALL_ACTS_VERSIONS"
    after_rows, after_events = records(paper.sessions)
    assert all(row in after_rows for row in old[0]) and after_events[: len(old[1])] == old[1]
    verify_acts_paper_journal(paper.sessions)


def test_postgresql_v3_restart_and_version_separated_reporting(monkeypatch):
    import os
    from uuid import uuid4

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from app.db.base import Base

    value = os.getenv("PHASE15_POSTGRES_TEST_URL")
    if not value:
        pytest.skip("PHASE15_POSTGRES_TEST_URL must point to disposable PostgreSQL 16")
    base = make_url(value)
    schema = f"acts_v3_{uuid4().hex}"
    admin = create_engine(base)
    target = create_engine(base.update_query_dict({"options": f"-csearch_path={schema}"}))
    try:
        with admin.begin() as connection:
            assert connection.scalar(text("SHOW server_version")).startswith("16.")
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        Base.metadata.create_all(target)
        test_closed_v2_evidence_is_untouched_and_reports_separate_versions(target, monkeypatch)
        paper = ActsV3PaperService(build_session_factory(target), v3_settings())
        with paper.sessions() as session:
            cursor = session.get(ActsPaperCursor, 1)
            snapshot_id = cursor.snapshot_id
        before = records(paper.sessions)
        assert paper.process_snapshot(snapshot_id) == "DUPLICATE"
        assert records(paper.sessions) == before
        verify_acts_paper_journal(paper.sessions)
    finally:
        target.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
