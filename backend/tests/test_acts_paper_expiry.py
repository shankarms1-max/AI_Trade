"""ACTS V2 expiry-day entries without changing V1, execution or mirror exits."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.acts_paper.service import (ActsPaperService, V2_ENTRY_POLICY_VERSION,
                                    verify_acts_paper_journal)
from app.db.session import build_session_factory
from app.paper import mirror_exit
from app.research.manifest import canonical, digest
from app.scalper.models import ScalperCandidate
from tests.test_acts_paper import _capture, _next, _seed_position
from tests.test_mirror_exit_v1 import open_acts, stored
from tests.test_scalper import settings
from tests.test_scalper_acts import qualified_rows


def service(engine):
    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(ActsPaperCursor(id=1, sequence=0, head_hash="GENESIS", lock_version=0,
                                    watch_json="{}", latest_json="{}"))
    return ActsPaperService(sessions, settings(acts_paper_enabled=True))


def history(engine, expiry_days=0, direction=1):
    rows = qualified_rows(direction, 70, "NEUTRAL")
    expiry = rows[-1].snapshot.captured_at.date() + timedelta(days=expiry_days)
    for row in rows:
        raw = row.snapshot.model_copy(update={
            "expiry": expiry,
            "quotes": [quote.model_copy(update={"expiry": expiry}) for quote in row.snapshot.quotes]})
        snapshot_id = _capture(engine, raw)
    return snapshot_id, raw


def records(sessions):
    with sessions() as session:
        return ([(row.id, row.state, row.document) for row in session.scalars(
                    select(ActsPaperTrade).order_by(ActsPaperTrade.id))],
                [(row.sequence, row.payload, row.previous_hash, row.event_hash) for row in session.scalars(
                    select(ActsPaperEvent).order_by(ActsPaperEvent.sequence))])


@pytest.mark.parametrize("direction", [1, -1])
def test_v2_expiry_day_decision_and_next_observation_fill(engine, monkeypatch, direction):
    paper = service(engine)
    snapshot_id, raw = history(engine, direction=direction)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("paper execution must not use network or broker orders")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("app.broker.kotak.market_data.KotakMarketDataAdapter.__init__", forbidden)
    assert paper.process_snapshot(snapshot_id) == "PENDING_PAPER_ENTRY"
    pending = stored(paper, ActsPaperTrade)[1]
    candidate = ScalperCandidate.model_validate(pending["candidate"])
    assert candidate.short_leg.expiry == candidate.long_leg.expiry == raw.captured_at.date()
    assert candidate.spread_width == 400 and paper.config.allowed_widths == (400,)
    assert pending.get("entry_timestamp") is None
    assert pending["policy"]["entry_policy_version"] == V2_ENTRY_POLICY_VERSION
    assert pending["policy"]["entry_policy"] == {"same_day_expiry": "ALLOWED", "expired_contracts": "FORBIDDEN"}
    assert pending["exit_policy_version"] == mirror_exit.VERSION
    assert pending["policy"]["exit_policy"] == mirror_exit.acts_policy(paper.config)
    before = records(paper.sessions)
    assert paper.process_snapshot(snapshot_id) == "DUPLICATE"
    assert records(paper.sessions) == before
    following = _next(raw, candidate=candidate)
    paper.process_snapshot(_capture(engine, following))
    state, opened = stored(paper, ActsPaperTrade)
    assert state == "OPEN" and opened["entry_timestamp"] == following.captured_at.isoformat()
    assert opened["entry_credit"] == pytest.approx(
        opened["entry_quotes"]["short"]["fill_price"] - opened["entry_quotes"]["long"]["fill_price"])
    assert opened["entry_quotes"]["short"]["side"] == "bid"
    assert opened["entry_quotes"]["long"]["side"] == "ask"
    assert "EXPIRY_DAY_ENTRY_FORBIDDEN" not in canonical(records(paper.sessions))
    restarted = ActsPaperService(paper.sessions, settings(acts_paper_enabled=True))
    assert stored(restarted, ActsPaperTrade) == (state, opened)
    # Same-day trades retain the exact existing 15:20 forced close and observed exit book.
    seconds = int((following.captured_at.replace(hour=15, minute=20, second=0) -
                   following.captured_at).total_seconds())
    final = _next(following, seconds=seconds, candidate=candidate)
    restarted.process_snapshot(_capture(engine, final))
    state, closed = stored(restarted, ActsPaperTrade)
    assert state == "CLOSED" and closed["exit_reason"] == "FORCED_CLOSE"
    assert closed["exit_quotes"]["short"]["side"] == "ask"
    assert closed["exit_quotes"]["long"]["side"] == "bid"
    verify_acts_paper_journal(paper.sessions)


def test_v2_already_expired_candidate_is_blocked_before_pending(engine):
    paper = service(engine)
    snapshot_id, _ = history(engine, expiry_days=-1)
    assert paper.process_snapshot(snapshot_id) == "NO_SETUP"
    with paper.sessions() as session:
        assert session.scalar(select(ActsPaperTrade)) is None
        latest = json.loads(session.get(ActsPaperCursor, 1).latest_json)
    assert "EXPIRED_CONTRACT_ENTRY_FORBIDDEN" in latest["hard_blockers"]
    verify_acts_paper_journal(paper.sessions)


def test_v2_pending_fill_rechecks_expiry(engine):
    paper = service(engine)
    snapshot_id, raw = history(engine)
    assert paper.process_snapshot(snapshot_id) == "PENDING_PAPER_ENTRY"
    candidate = ScalperCandidate.model_validate(stored(paper, ActsPaperTrade)[1]["candidate"])
    # Keep TTL in range to exercise expiry independently of age or score gates.
    paper.config = replace(paper.config, entry_ttl_seconds=25 * 3600)
    following = _next(raw, candidate=candidate, seconds=24 * 3600)
    paper.process_snapshot(_capture(engine, following))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "ENTRY_REJECTED" and doc["rejection_reason"] == "EXPIRED_CONTRACT_ENTRY_FORBIDDEN"
    assert doc.get("entry_credit") is None
    verify_acts_paper_journal(paper.sessions)


@pytest.mark.parametrize("failure,reason", [
    ("depth", "INSUFFICIENT_DEPTH"), ("stale", "STALE_OR_FUTURE_QUOTE"),
    ("credit", "FILL_ECONOMICS_OUTSIDE_RISK"), ("max_loss", "FILL_ECONOMICS_OUTSIDE_RISK"),
    ("kill", "KILL_SWITCH_ACTIVE"), ("event", "MARKET_EVENT_BLOCKED"),
])
def test_expiry_day_fill_preserves_existing_risk_checks(engine, failure, reason):
    paper = service(engine)
    snapshot_id, raw = history(engine)
    assert paper.process_snapshot(snapshot_id) == "PENDING_PAPER_ENTRY"
    candidate = ScalperCandidate.model_validate(stored(paper, ActsPaperTrade)[1]["candidate"])
    following = _next(raw, candidate=candidate)
    if failure in {"depth", "stale"}:
        changes = ({"bid_quantity": 0, "ask_quantity": 0} if failure == "depth" else
                   {"source_market_timestamp": raw.captured_at - timedelta(seconds=60)})
        following = following.model_copy(update={"quotes": [quote.model_copy(update=changes)
                                                            for quote in following.quotes]})
    elif failure == "credit":
        paper.config = replace(paper.config, min_credit=1000)
    elif failure == "max_loss":
        paper.config = replace(paper.config, max_loss_per_trade=1)
    elif failure == "kill":
        paper.config = replace(paper.config, kill_switch=True)
    else:
        paper.events = SimpleNamespace(events_at=lambda _: [SimpleNamespace(block_entries=True)])
    paper.process_snapshot(_capture(engine, following))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "ENTRY_REJECTED" and doc.get("entry_credit") is None
    assert doc["rejection_reason"] == reason
    verify_acts_paper_journal(paper.sessions)


def test_v1_pending_expiry_day_remains_forbidden(engine):
    paper, raw, candidate, _ = _seed_position(engine, pending=True)
    following = _next(raw, candidate=candidate)
    following = following.model_copy(update={"captured_at": following.captured_at.replace(
        year=candidate.short_leg.expiry.year, month=candidate.short_leg.expiry.month,
        day=candidate.short_leg.expiry.day)})
    paper.config = replace(paper.config, entry_ttl_seconds=30 * 24 * 3600)
    paper.process_snapshot(_capture(engine, following))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "ENTRY_REJECTED" and doc["rejection_reason"] == "EXPIRY_DAY_ENTRY_FORBIDDEN"


@pytest.mark.parametrize("state", ["PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE", "CLOSED"])
def test_entry_policy_upgrade_refuses_old_active_v2_without_mutating_history(engine, state):
    paper, raw, _ = open_acts(engine)
    with paper.sessions.begin() as session:
        row = session.scalar(select(ActsPaperTrade))
        doc = json.loads(row.document)
        old_policy = deepcopy(doc["policy"])
        old_policy.pop("entry_policy_version")
        old_policy.pop("entry_policy")
        doc.update(policy=old_policy, policy_hash=digest(old_policy))
        paper._save(row, doc, state)
        paper._emit(session, session.get(ActsPaperCursor, 1), "ACTS_TEST_HISTORICAL_POLICY",
                    row.decision_snapshot_id, raw, row)
    before = records(paper.sessions)
    verify_acts_paper_journal(paper.sessions)
    if state == "CLOSED":
        restarted = ActsPaperService(paper.sessions, settings())
        assert restarted.policy_hash != doc["policy_hash"]
    else:
        with pytest.raises(RuntimeError, match="REQUIRES_ZERO_ACTIVE_TRADES"):
            ActsPaperService(paper.sessions, settings())
        # Repeat the safety check while holding the normal cursor transaction lock,
        # covering an old worker committing a position after new-service startup.
        with pytest.raises(RuntimeError, match="REQUIRES_ZERO_ACTIVE_TRADES"):
            paper.process_snapshot(_capture(engine, _next(raw)))
    assert records(paper.sessions) == before
    verify_acts_paper_journal(paper.sessions)
