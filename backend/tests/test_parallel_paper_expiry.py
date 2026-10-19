"""Real parallel strategy signals retain their gates while allowing expiry-day paper fills."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import socket

import pytest
from sqlalchemy import select

from app.parallel_paper.bars import Bar
from app.parallel_paper.config import STRATEGIES
from app.parallel_paper.indicators import ema
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperEvent, ParallelPaperTrade
from app.parallel_paper.service import ENTRY_POLICY_VERSION, ParallelPaperService, verify_journals
from app.paper import mirror_exit
from app.research.manifest import canonical, digest
from app.scalper.models import ScalperCandidate
from tests.test_acts_paper import _next
from tests.test_mirror_exit_v1 import open_parallel
from tests.test_parallel_paper_service import capture, raw, service
from tests.test_parallel_paper_strategies import START, bars, points
from tests.test_scalper import settings


def observation(at, price, expiry_days=0):
    base = raw(0, price)
    expiry = START.date() + timedelta(days=expiry_days)
    return base.model_copy(update={
        "captured_at": at, "request_started_at": at - timedelta(seconds=1), "response_received_at": at,
        "source_market_timestamp": at, "expiry": expiry,
        "quotes": [quote.model_copy(update={"expiry": expiry, "source_market_timestamp": at})
                   for quote in base.quotes]})


def stored(paper, strategy):
    with paper.sessions() as session:
        row = session.scalar(select(ParallelPaperTrade).where(ParallelPaperTrade.strategy_id == strategy))
        return None if row is None else (row.state, json.loads(row.document))


def records(paper):
    with paper.sessions() as session:
        return ([(row.id, row.state, row.document) for row in session.scalars(
                    select(ParallelPaperTrade).order_by(ParallelPaperTrade.id))],
                [(row.strategy_id, row.sequence, row.payload, row.previous_hash, row.event_hash)
                 for row in session.scalars(select(ParallelPaperEvent).order_by(
                    ParallelPaperEvent.strategy_id, ParallelPaperEvent.sequence))],
                [(row.strategy_id, row.sequence, row.head_hash, row.lock_version, row.snapshot_id,
                  row.observed_at, row.state_json, row.latest_json) for row in session.scalars(
                    select(ParallelPaperCursor).order_by(ParallelPaperCursor.strategy_id))])


def process(engine, paper, strategy, at, price, samples, completed, expiry_days=0):
    current = observation(at, price, expiry_days)
    snapshot_id = capture(engine, current)
    status = paper._process(strategy, snapshot_id, current, samples, completed, {})
    return status, snapshot_id, current


def signal(engine, paper, strategy, expiry_days=0, direction=1):
    if strategy.startswith("ORB_"):
        samples = points(int(strategy.split("_")[1]), direction)
        for seconds in (0, 15, 30):
            if seconds:
                samples.append(replace(samples[-1], at=samples[0].at + timedelta(
                    minutes=int(strategy.split("_")[1]), seconds=seconds)))
            status, snapshot_id, current = process(
                engine, paper, strategy, samples[-1].at, samples[-1].price, samples, [], expiry_days)
            if seconds < 30:
                assert status == "WATCH"
        completed = []
    elif strategy == "EMA_PULLBACK":
        completed = bars(direction, count=23)
        status, _, _ = process(engine, paper, strategy, completed[-1].end, completed[-1].close,
                               [], completed, expiry_days)
        assert status == "NO_SETUP"
        fast = ema([bar.close for bar in completed], paper.experiment.ema_fast)[-1]
        previous = completed[-1].close
        pullback = Bar(completed[-1].end, completed[-1].end + timedelta(minutes=3), previous,
                       max(previous, fast) + 3, min(previous, fast) - 3, fast, 100)
        completed.append(pullback)
        status, _, _ = process(engine, paper, strategy, pullback.end, fast, [], completed, expiry_days)
        assert status == "WATCH"
        resumed = previous + direction * 10
        completed.append(Bar(pullback.end, pullback.end + timedelta(minutes=3), fast,
                             max(resumed, fast) + 3, min(resumed, fast) - 3, resumed, 150))
        status, snapshot_id, current = process(engine, paper, strategy, completed[-1].end,
                                               resumed, [], completed, expiry_days)
        samples = []
    else:
        completed = bars(direction)
        status, snapshot_id, current = process(engine, paper, strategy, completed[-1].end,
                                               completed[-1].close, [], completed, expiry_days)
        samples = []
    return status, snapshot_id, current, samples, completed


def fill(engine, paper, strategy, current, samples, completed, seconds=15):
    following = _next(current, seconds=seconds)
    updated = [*samples, replace(samples[-1], at=following.captured_at)] if samples else []
    return paper._process(strategy, capture(engine, following), following, updated, completed, {}), following


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("direction", [1, -1])
def test_all_five_expiry_day_signals_fill_and_keep_execution_and_exit_policy(engine, monkeypatch, strategy, direction):
    paper = service(engine)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("parallel paper must not call a broker or order path")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("app.broker.kotak.market_data.KotakMarketDataAdapter.__init__", forbidden)
    status, snapshot_id, current, samples, completed = signal(engine, paper, strategy, direction=direction)
    assert status == "PENDING_PAPER_ENTRY"
    pending = stored(paper, strategy)[1]
    candidate = ScalperCandidate.model_validate(pending["candidate"])
    assert candidate.spread_width == 400 and paper.configs[strategy].allowed_widths == (400,)
    assert candidate.short_leg.expiry == candidate.long_leg.expiry == current.captured_at.date()
    assert pending.get("entry_timestamp") is None
    assert pending["policy"]["entry_policy_version"] == ENTRY_POLICY_VERSION
    assert pending["policy"]["entry_policy"] == {"same_day_expiry": "ALLOWED", "expired_contracts": "FORBIDDEN"}
    assert pending["exit_policy_version"] == mirror_exit.VERSION
    assert pending["policy"]["exit_policy"] == mirror_exit.policy(paper.configs[strategy])
    if strategy.startswith("ORB_"):
        assert pending["strategy_features"]["confirmation_seconds"] == 30
    elif strategy == "SUPERTREND_RSI_VOLUME":
        assert pending["strategy_features"]["volume_ratio"] == 1.5
        assert pending["strategy_features"]["rsi"] == (100 if direction > 0 else 0)
    before = records(paper)
    assert paper._process(strategy, snapshot_id, current, samples, completed, {}) == "DUPLICATE"
    assert records(paper)[:2] == before[:2]
    _, following = fill(engine, paper, strategy, current, samples, completed)
    state, opened = stored(paper, strategy)
    assert state == "OPEN" and opened["entry_timestamp"] == following.captured_at.isoformat()
    assert opened["entry_credit"] == pytest.approx(
        opened["entry_quotes"]["short"]["fill_price"] - opened["entry_quotes"]["long"]["fill_price"])
    assert opened["entry_quotes"]["short"]["side"] == "bid" and opened["entry_quotes"]["long"]["side"] == "ask"
    assert "EXPIRY_DAY_ENTRY_FORBIDDEN" not in canonical(records(paper))
    assert ParallelPaperService(paper.sessions, settings()).hashes == paper.hashes
    seconds = int((following.captured_at.replace(hour=15, minute=20, second=0) -
                   following.captured_at).total_seconds())
    fill(engine, paper, strategy, following, samples, completed, seconds)
    state, closed = stored(paper, strategy)
    assert state == "CLOSED" and closed["exit_reason"] == "FORCED_CLOSE"
    assert closed["exit_quotes"]["short"]["side"] == "ask" and closed["exit_quotes"]["long"]["side"] == "bid"
    verify_journals(paper.sessions)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_all_five_reject_already_expired_decision(engine, strategy):
    paper = service(engine)
    status, _, _, _, _ = signal(engine, paper, strategy, expiry_days=-1)
    assert status == "ENTRY_REJECTED" and stored(paper, strategy) is None
    with paper.sessions() as session:
        latest = json.loads(session.get(ParallelPaperCursor, strategy).latest_json)
    assert "EXPIRED_CONTRACT_ENTRY_FORBIDDEN" in latest["blockers"]
    assert "EXPIRY_DAY_ENTRY_FORBIDDEN" not in latest["blockers"]
    verify_journals(paper.sessions)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_all_five_pending_fills_recheck_expired_contract(engine, strategy):
    paper = service(engine)
    status, _, current, samples, completed = signal(engine, paper, strategy)
    assert status == "PENDING_PAPER_ENTRY"
    # Isolate expiry from the independent TTL limit in this synthetic delayed fill.
    paper.configs[strategy] = replace(paper.configs[strategy], entry_ttl_seconds=25 * 3600)
    fill(engine, paper, strategy, current, samples, completed, 24 * 3600)
    state, rejected = stored(paper, strategy)
    assert state == "ENTRY_REJECTED" and rejected["rejection_reason"] == "EXPIRED_CONTRACT_ENTRY_FORBIDDEN"
    assert rejected.get("entry_credit") is None
    verify_journals(paper.sessions)


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("state", ["PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE"])
def test_upgrade_refuses_any_old_active_strategy_without_modifying_history(engine, strategy, state):
    paper, initial, _, _ = open_parallel(engine, strategy)
    with paper.sessions.begin() as session:
        row = session.get(ParallelPaperTrade, "mirror-test")
        doc = json.loads(row.document)
        old_policy = deepcopy(doc["policy"])
        old_policy.pop("entry_policy_version")
        old_policy.pop("entry_policy")
        doc.update(policy=old_policy, policy_hash=digest(old_policy))
        row.state, row.document = state, canonical(doc)
        paper._emit(session, session.get(ParallelPaperCursor, strategy), row.decision_snapshot_id,
                    initial, "TEST_HISTORICAL_ENTRY_POLICY", row)
    before = records(paper)
    verify_journals(paper.sessions)
    with pytest.raises(RuntimeError, match="REQUIRES_ZERO_ACTIVE_TRADES"):
        ParallelPaperService(paper.sessions, settings())
    other = next(name for name in STRATEGIES if name != strategy)
    subsequent = _next(initial)
    snapshot_id = capture(engine, subsequent)
    with pytest.raises(RuntimeError, match="REQUIRES_ZERO_ACTIVE_TRADES"):
        paper._process(other, snapshot_id, subsequent, [], [], {})
    assert records(paper) == before
    verify_journals(paper.sessions)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_historical_expiry_rejections_are_preserved_and_new_policy_is_distinct(engine, strategy):
    paper = service(engine)
    status, snapshot_id, current, samples, completed = signal(engine, paper, strategy)
    assert status == "PENDING_PAPER_ENTRY"
    with paper.sessions.begin() as session:
        row = session.scalar(select(ParallelPaperTrade).where(ParallelPaperTrade.strategy_id == strategy))
        doc = json.loads(row.document)
        doc["policy"].pop("entry_policy_version")
        doc["policy"].pop("entry_policy")
        doc.update(policy_hash=digest(doc["policy"]), rejection_reason="EXPIRY_DAY_ENTRY_FORBIDDEN")
        row.state, row.document = "ENTRY_REJECTED", canonical(doc)
        paper._emit(session, session.get(ParallelPaperCursor, strategy), snapshot_id, current,
                    "ENTRY_REJECTED", row, reason=doc["rejection_reason"])
    before = records(paper)
    restarted = ParallelPaperService(paper.sessions, settings())
    assert records(restarted) == before
    assert restarted.hashes[strategy] != doc["policy_hash"]
    verify_journals(paper.sessions)
    if strategy == "ORB_15":
        # The next opposite breakout is a new eligible signal; retain all old
        # re-entry rules and confirmation state instead of resetting the cursor.
        for seconds in (15, 30, 45):
            at = current.captured_at + timedelta(seconds=seconds)
            samples.append(replace(samples[-1], at=at, price=24970))
            outcome, _, _ = process(engine, restarted, strategy, at, 24970, samples, [])
            assert outcome == ("PENDING_PAPER_ENTRY" if seconds == 45 else "WATCH")
        with paper.sessions() as session:
            rows = session.scalars(select(ParallelPaperTrade).order_by(ParallelPaperTrade.decision_snapshot_id)).all()
            assert len(rows) == 2 and rows[0].state == "ENTRY_REJECTED" and rows[1].state == "PENDING_PAPER_ENTRY"
            assert rows[0].document == before[0][0][2]
            old_events = session.scalars(select(ParallelPaperEvent).where(
                ParallelPaperEvent.strategy_id == strategy).order_by(ParallelPaperEvent.sequence)).all()
            assert [(item.strategy_id, item.sequence, item.payload, item.previous_hash, item.event_hash)
                    for item in old_events[:len(before[1])]] == before[1]
        verify_journals(paper.sessions)
