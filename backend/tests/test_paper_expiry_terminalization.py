"""Expired exposure has no execution/P&L; only append causal lifecycle evidence."""
from datetime import datetime, timedelta, timezone
import json
import socket

import pytest
from sqlalchemy import select

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.acts_paper.service import ActsPaperService, _lab_trade
from app.parallel_paper.config import STRATEGIES
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperEvent, ParallelPaperTrade
from app.parallel_paper.report import statistics
from app.parallel_paper.service import ParallelPaperService, _lab
from app.parallel_paper.trade_monitor import trades
from app.paper import expiry
from app.research.manifest import canonical, digest
from app.scalper.models import ScalperCandidate
from app.scalper.repository import IST
from app.scalper_lab.engine import Ledger, _risk_blockers
from tests.test_phase15_exit_book import case, process, stored, audit, wide
from tests.test_scalper import settings

ALL = ("ACTS_V2", *STRATEGIES)


def models(strategy):
    return (ActsPaperTrade, ActsPaperEvent, ActsPaperCursor) if strategy == "ACTS_V2" else (
        ParallelPaperTrade, ParallelPaperEvent, ParallelPaperCursor)


def seed(engine, strategy, state="OPEN", *, old=False):
    paper, raw, candidate = case(engine, strategy)
    trade_model, _, cursor_model = models(strategy)
    with paper.sessions.begin() as session:
        row = session.scalar(select(trade_model))
        doc = json.loads(row.document)
        for name in ("short_leg", "long_leg"):
            doc["candidate"][name]["expiry"] = raw.captured_at.date().isoformat()
        if old:
            doc["policy"].pop("exit_book_policy")
            doc["policy_hash"] = digest(doc["policy"])
        if state == "UNRESOLVED_EXPOSURE":
            doc["unresolved_reason"] = "BID_ASK_SPREAD_TOO_WIDE"
        if state == "PENDING_PAPER_ENTRY":
            for name in ("entry_credit", "entry_timestamp", "entry_quotes"):
                doc.pop(name, None)
        row.state, row.document = state, canonical(doc)
        candidate = ScalperCandidate.model_validate(doc["candidate"])
        cursor = session.get(cursor_model, 1 if strategy == "ACTS_V2" else strategy)
        if strategy == "ACTS_V2":
            paper._emit(session, cursor, "ACTS_TEST_EXPIRY", row.decision_snapshot_id, raw, row)
        else:
            paper._emit(session, cursor, row.decision_snapshot_id, raw, "TEST_EXPIRY", row)
    raw = raw.model_copy(update={"expiry": raw.captured_at.date(), "quotes": [
        q.model_copy(update={"expiry": raw.captured_at.date()}) for q in raw.quotes]})
    return paper, raw, candidate


def events(paper, strategy):
    _, event_model, _ = models(strategy)
    with paper.sessions() as session:
        return [(e.sequence, e.payload, e.previous_hash, e.event_hash)
                for e in session.scalars(select(event_model).order_by(event_model.sequence))]


def no_book(raw, candidate, seconds):
    at = raw.captured_at + timedelta(seconds=seconds)
    chain_expiry = raw.expiry if at.date() == raw.captured_at.date() else at.date() + timedelta(days=7)
    return raw.model_copy(update={
        "captured_at": at, "request_started_at": at, "response_received_at": at,
        # Deliberately wrong current chain expiry; only the held legs determine expiry.
        "expiry": chain_expiry,
        "quotes": [q.model_copy(update={"bid": None, "ltp": .05, "expiry": chain_expiry, "source_market_timestamp": at})
                   if q.instrument_token == candidate.long_leg.instrument_token else
                   q.model_copy(update={"expiry": chain_expiry, "source_market_timestamp": at}) for q in raw.quotes]})


@pytest.mark.parametrize("strategy", ALL)
def test_expiry_day_missing_hedge_bid_does_not_create_zero_or_tick_fill(engine, strategy):
    paper, raw, candidate = seed(engine, strategy)
    seconds = int((raw.captured_at.replace(hour=15, minute=20, second=0) - raw.captured_at).total_seconds())
    current = no_book(raw, candidate, seconds)
    process(engine, paper, strategy, current)
    state, doc = stored(paper, strategy)
    assert state == "UNRESOLVED_EXPOSURE" and doc["unresolved_reason"] == "MISSING_EXECUTABLE_SIDE"
    assert doc.get("exit_debit") is None and doc.get("final_outcome") is None
    assert not doc.get("expiry_terminalization")
    audit(paper, strategy)


@pytest.mark.parametrize("strategy", ALL)
@pytest.mark.parametrize("state", ["OPEN", "UNRESOLVED_EXPOSURE", "PENDING_PAPER_ENTRY"])
def test_first_post_expiry_observation_appends_unpriced_terminal_event(engine, strategy, state, monkeypatch):
    paper, raw, candidate = seed(engine, strategy, state)
    before = events(paper, strategy)
    original = stored(paper, strategy)[1]
    following = no_book(raw, candidate, 86400)

    def forbidden(*_, **__):
        raise AssertionError("expiry lifecycle must never execute a book, cost model or network order")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("app.acts_paper.service.executable_pair", forbidden)
    monkeypatch.setattr("app.parallel_paper.service.executable_pair", forbidden)
    process(engine, paper, strategy, following)
    actual, doc = stored(paper, strategy)
    assert actual == ("ENTRY_REJECTED" if state == "PENDING_PAPER_ENTRY" else "CLOSED")
    assert doc["terminalized_at"] == following.captured_at.isoformat()
    assert doc["policy_hash"] == original["policy_hash"] and doc["policy"] == original["policy"]
    if actual == "ENTRY_REJECTED":
        assert doc["rejection_reason"] == "CONTRACT_EXPIRED_BEFORE_FILL" and doc.get("entry_credit") is None
    else:
        assert doc["exit_reason"] == "CONTRACT_EXPIRED_UNPRICED" and expiry.is_unpriced(doc)
        assert doc["expiry_date"] == candidate.short_leg.expiry.isoformat()
        assert doc["exit_pricing_status"] == "UNAVAILABLE"
        assert all(doc[key] is None for key in (
            "exit_debit", "exit_quotes", "final_outcome", "gross_pnl", "exit_timestamp", "exit_snapshot_id"))
        assert doc["previous_unresolved_reason"] == (
            "BID_ASK_SPREAD_TOO_WIDE" if state == "UNRESOLVED_EXPOSURE" else None)
    assert events(paper, strategy)[:len(before)] == before
    terminal_events = [json.loads(e[1]) for e in events(paper, strategy)[len(before):]
                       if json.loads(e[1]).get("trade_id")]
    assert len(terminal_events) == 1
    assert terminal_events[0]["lifecycle_version"] == expiry.VERSION
    assert terminal_events[0]["snapshot_id"] == doc["terminalization_snapshot_id"]
    assert process(engine, paper, strategy, following) == "DUPLICATE"
    audit(paper, strategy)
    assert not trades(paper.sessions, following.captured_at, positions=True)["items"]
    report = statistics([(actual, doc)], following.captured_at)
    assert report["wins"] == report["losses"] == report["closed_trades"] == report["gross_pnl"] == 0
    assert report["open_position_count"] == report["unresolved_exposure_count"] == 0
    assert report["expired_unpriced_count"] == (actual == "CLOSED")
    if actual == "CLOSED":
        row = trades(paper.sessions, following.captured_at)["items"][0]
        assert row["realized_gross_pnl"] is None and row["exit_timestamp"] is None
        assert row["terminal_status"] == expiry.LABEL
    trade_model, _, _ = models(strategy)
    with paper.sessions() as session:
        row = session.scalar(select(trade_model))
        lab = _lab_trade(row) if strategy == "ACTS_V2" else _lab(row)
        config = paper.config if strategy == "ACTS_V2" else paper.configs[strategy]
        assert Ledger(trades=[lab]).active() is None
        # Also exercise the same-session accounting path, which used to assert.
        lab.decision = {**lab.decision, "timestamp": following.captured_at.isoformat()}
        reasons = _risk_blockers(Ledger(trades=[lab]), config, following.captured_at, False)
        assert not set(reasons) & {"MAX_OPEN_POSITIONS_REACHED", "HARD_DAILY_LOSS_REACHED",
                                   "MAX_CONSECUTIVE_LOSSES_REACHED", "LOSS_COOLDOWN_ACTIVE",
                                   "ANY_EXIT_COOLDOWN_ACTIVE"}


@pytest.mark.parametrize("strategy", ALL)
@pytest.mark.parametrize("state", ["OPEN", "UNRESOLVED_EXPOSURE", "PENDING_PAPER_ENTRY"])
def test_old_policy_expired_startup_is_deferred_only_to_expiry_not_execution(engine, strategy, state, monkeypatch):
    paper, raw, candidate = seed(engine, strategy, state, old=True)
    now = raw.captured_at + timedelta(days=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or IST)

    module = "app.acts_paper.service" if strategy == "ACTS_V2" else "app.parallel_paper.service"
    monkeypatch.setattr(module + ".datetime", Clock)
    before = events(paper, strategy)
    restarted = (ActsPaperService if strategy == "ACTS_V2" else ParallelPaperService)(paper.sessions, settings())
    assert events(paper, strategy) == before and stored(paper, strategy)[0] == state
    # Startup wall date cannot authorize execution of an older unexpired observation.
    with pytest.raises(RuntimeError, match="EXIT_BOOK_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES"):
        process(engine, restarted, strategy, no_book(raw, candidate, 15))
    assert events(paper, strategy) == before
    process(engine, restarted, strategy, no_book(raw, candidate, 86400))
    assert stored(paper, strategy)[0] == ("ENTRY_REJECTED" if state == "PENDING_PAPER_ENTRY" else "CLOSED")
    audit(paper, strategy)


@pytest.mark.parametrize("strategy", ALL)
def test_ist_expiry_boundary_and_same_day_unresolved_guard(engine, strategy):
    paper, raw, candidate = seed(engine, strategy, "UNRESOLVED_EXPOSURE")
    doc = stored(paper, strategy)[1]
    # IST midnight is still the prior UTC date. No expiry-session terminalization.
    assert not expiry.expired(doc, raw.captured_at.replace(hour=23, minute=59))
    first = (raw.captured_at + timedelta(days=1)).replace(hour=0, minute=0).astimezone(timezone.utc)
    assert expiry.expired(doc, first)
    current = no_book(raw, candidate, 15)
    process(engine, paper, strategy, current)
    assert stored(paper, strategy)[0] == "UNRESOLVED_EXPOSURE"


def test_priced_exit_accounting_and_risk_remain_identical():
    at = datetime(2026, 10, 21, 10, tzinfo=IST)
    doc = {"entry_timestamp": at.isoformat(), "exit_timestamp": at.isoformat(),
           "final_outcome": {"gross_rupees": -500, "net_rupees": -510}}
    expected = statistics([("CLOSED", doc)], at)
    unpriced = {"entry_timestamp": at.isoformat(), "exit_timestamp": None,
                "expiry_terminalization": True, "final_outcome": None}
    actual = statistics([("CLOSED", doc), ("CLOSED", unpriced)], at)
    for name in ("wins", "losses", "breakeven", "gross_pnl", "net_pnl", "realized_pnl_today", "max_drawdown"):
        assert actual[name] == expected[name]
    assert actual["expired_unpriced_count"] == 1


@pytest.mark.parametrize("strategy", ["ORB_15", "ORB_30", "ORB_60"])
@pytest.mark.parametrize("state", ["OPEN", "UNRESOLVED_EXPOSURE"])
def test_orb_active_signal_is_diagnostic_after_management_not_repeated_rejection(engine, strategy, state, monkeypatch):
    paper, raw, candidate = seed(engine, strategy, state)
    credit = stored(paper, strategy)[1]["entry_credit"]
    monkeypatch.setattr("app.parallel_paper.service.decide", lambda *_: {
        "status": "ENTER", "direction": "BULL", "features": {"breakout_timestamp": raw.captured_at.isoformat()},
        "blockers": [], "entry_reason": "ORB_BREAKOUT"})

    def forbidden(*_, **__):
        raise AssertionError("active entry diagnostic must not rebuild entry candidates")

    monkeypatch.setattr("app.parallel_paper.service.build_candidates", forbidden)
    for seconds in (15, 30, 45):
        current = wide(raw, candidate, seconds, credit)
        assert process(engine, paper, strategy, current) == "BLOCKED"
    trade_model, event_model, cursor_model = models(strategy)
    with paper.sessions() as session:
        assert len(session.scalars(select(trade_model)).all()) == 1
        latest = json.loads(session.get(cursor_model, strategy).latest_json)
        assert latest["blockers"] == ["ACTIVE_POSITION_BLOCKS_ENTRY"]
        event_types = session.scalars(select(event_model.event_type)).all()
        assert "ENTRY_REJECTED" not in event_types
        if state == "OPEN":
            assert "MARK" in event_types  # MIRROR_EXIT_V1/marks were evaluated first.
    assert stored(paper, strategy)[0] == state
    audit(paper, strategy)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_next_session_can_create_new_pending_entry_after_expiry(engine, strategy, monkeypatch):
    paper, raw, candidate = seed(engine, strategy, "UNRESOLVED_EXPOSURE")
    following = no_book(raw, candidate, 86400)
    next_expiry = following.captured_at.date() + timedelta(days=7)
    following = following.model_copy(update={"expiry": next_expiry, "quotes": [
        q.model_copy(update={"expiry": next_expiry, "source_market_timestamp": following.captured_at})
        for q in raw.quotes]})
    monkeypatch.setattr("app.parallel_paper.service.decide", lambda *_: {
        "strategy_id": strategy, "status": "ENTER", "direction": "BULL",
        "features": {"breakout_timestamp": following.captured_at.isoformat(),
                     "bar_end": following.captured_at.isoformat()},
        "blockers": [], "entry_reason": "TEST_EXISTING_VALID_SIGNAL"})
    assert process(engine, paper, strategy, following) == "PENDING_PAPER_ENTRY"
    with paper.sessions() as session:
        rows = session.scalars(select(ParallelPaperTrade)).all()
        assert sorted(row.state for row in rows) == ["CLOSED", "PENDING_PAPER_ENTRY"]
        assert json.loads(rows[0].document)["exit_reason"] == "CONTRACT_EXPIRED_UNPRICED"
    audit(paper, strategy)


def test_acts_next_session_can_enter_after_unpriced_expiry(engine, monkeypatch):
    from tests.test_acts_paper_v2 import _decision
    paper, raw, candidate = seed(engine, "ACTS_V2", "UNRESOLVED_EXPOSURE")
    at = raw.captured_at + timedelta(days=1)
    following = raw.model_copy(update={
        "captured_at": at, "request_started_at": at, "response_received_at": at,
        "expiry": at.date() + timedelta(days=7), "quotes": [q.model_copy(update={
            "expiry": at.date() + timedelta(days=7), "source_market_timestamp": at}) for q in raw.quotes]})
    ctx, _, _, _ = _decision()
    ctx["timestamp"] = at.isoformat()
    ctx["expiry_date"] = following.expiry
    monkeypatch.setattr(paper, "_context", lambda _: ctx)
    assert process(engine, paper, "ACTS_V2", following) == "PENDING_PAPER_ENTRY"
    with paper.sessions() as session:
        assert sorted(row.state for row in session.scalars(select(ActsPaperTrade))) == ["CLOSED", "PENDING_PAPER_ENTRY"]
    audit(paper, "ACTS_V2")


@pytest.mark.parametrize("strategy", ALL)
def test_expiry_day_five_paisa_hedge_without_depth_is_not_a_fill(engine, strategy):
    paper, raw, candidate = seed(engine, strategy)
    seconds = int((raw.captured_at.replace(hour=15, minute=20, second=0) - raw.captured_at).total_seconds())
    current = no_book(raw, candidate, seconds)
    current = current.model_copy(update={"quotes": [
        q.model_copy(update={"bid": .05, "bid_quantity": 0})
        if q.instrument_token == candidate.long_leg.instrument_token else q for q in current.quotes]})
    process(engine, paper, strategy, current)
    state, doc = stored(paper, strategy)
    assert state == "UNRESOLVED_EXPOSURE" and doc["unresolved_reason"] == "INSUFFICIENT_DEPTH"
    assert doc.get("exit_debit") is None and doc.get("gross_pnl") is None
    audit(paper, strategy)
