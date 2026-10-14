"""Expiry releases only the entry block, through the append-only paper journal."""
from copy import deepcopy
from datetime import datetime, time, timedelta
import json

import pytest
from sqlalchemy import event, select

from app.paper.models import PaperEvent, PaperTrade
from app.paper.service import (PaperEngine, assert_no_paper_exposure, blocks_paper_exposure,
                               paper_summary, unresolved_blocks_exposure, verify_journal)
from app.research.manifest import canonical
from app.risk.models import EvaluationContext
from app.risk.service import build_and_store_risk
from tests.test_forward_paper import (  # noqa: F401
    change_short, events, isolated_settings, next_snapshot, paper_case, trade,
)
from tests.test_phase7_risk import approved_config, persisted_context


def make_unresolved(case):
    case.paper.observe(next_snapshot(case))
    case.paper.observe(next_snapshot(case, mutate=change_short(case, instrument_token="wrong-token")))
    state, doc = trade(case)
    assert state == "UNRESOLVED_EXPOSURE" and doc["unresolved_reason"] == "MISSING_EXACT_CONTRACT"
    return doc


def observation_on(case, day, *, decisions=False):
    at = datetime.combine(day, time(10, 18), case.raw.timestamp_ist.tzinfo)
    expiry = day + timedelta(days=7)
    raw = case.raw.model_copy(update={
        "timestamp_ist": at, "request_started_at": at, "response_received_at": at,
        "expiry": expiry, "required_contracts": None,
        "options": [item.model_copy(update={"expiry": expiry, "source_market_timestamp": at})
                    for item in case.raw.options]})
    if decisions:
        snapshot_id, risks = persisted_context(case.repository, case.sessions, raw)
        result = build_and_store_risk(risks, snapshot_id, approved_config(), EvaluationContext.HISTORICAL)
        assert result.approved_count > 0
    else:
        snapshot_id = case.repository.save_market_snapshot(raw, at).snapshot_id
    case.clock[0] = at
    return snapshot_id


def journal_rows(case):
    with case.sessions() as session:
        return [(row.sequence, row.event_key, row.event_type, row.payload, row.previous_hash, row.event_hash)
                for row in session.scalars(select(PaperEvent).order_by(PaperEvent.sequence))]


@pytest.mark.parametrize("days_after", [-1, 0])
def test_unresolved_blocks_through_expiry_inclusive(paper_case, days_after):  # noqa: F811
    case = paper_case
    original = make_unresolved(case)
    expiry = case.raw.expiry
    day = expiry + timedelta(days=days_after)
    assert unresolved_blocks_exposure(original, day)
    state = case.paper.get_state(day)
    assert not state.monetary_pnl_complete and state.open_strategy_keys
    with pytest.raises(ValueError, match="NO_FORWARD_PAPER_EXPOSURE"):
        assert_no_paper_exposure(case.sessions, day)
    snapshot_id = observation_on(case, day, decisions=True)
    case.paper.observe(snapshot_id)
    case.paper.enqueue(snapshot_id)
    assert trade(case) == ("UNRESOLVED_EXPOSURE", original)
    assert not any(kind == "UNRESOLVED_EXPOSURE_EXPIRED" for kind, _ in events(case))
    with case.sessions() as session:
        assert len(session.scalars(select(PaperTrade)).all()) == 1
    verify_journal(case.sessions)


def test_post_expiry_release_is_journaled_idempotent_and_new_entries_work(paper_case, monkeypatch):  # noqa: F811
    case = paper_case
    original = make_unresolved(case)
    expiry = case.raw.expiry
    before = journal_rows(case)
    # A deployed source-tree change must not replace the historical policy hash.
    monkeypatch.setattr("app.paper.service.implementation_hash", lambda: "backport-source-tree")
    case.paper = PaperEngine(case.sessions, case.settings, clock=lambda: case.clock[0])
    assert case.paper.policy_hash != original["policy_hash"]
    day = expiry + timedelta(days=1)
    snapshot_id = observation_on(case, day, decisions=True)
    writes = []

    def record_write(_conn, _cursor, statement, _parameters, _context, _many):
        writes.append(statement.lstrip().upper())

    bind = case.sessions.kw["bind"]
    event.listen(bind, "before_cursor_execute", record_write)
    try:
        case.paper.observe(snapshot_id)
    finally:
        event.remove(bind, "before_cursor_execute", record_write)
    assert not any(sql.startswith(("UPDATE PAPER_EVENTS", "DELETE FROM PAPER_EVENTS")) for sql in writes)
    state, released = trade(case)
    assert state == "UNRESOLVED_EXPOSURE"
    release = released.pop("exposure_release")
    assert released == original
    assert release == dict(candidate_expiry=expiry.isoformat(), release_snapshot_id=snapshot_id,
                           release_date=day.isoformat(), release_market_timestamp=case.clock[0].isoformat(),
                           reason="CANDIDATE_EXPIRY_PASSED", exit_price_inferred=False,
                           pnl_inferred=False, profitability_claim=False)
    assert journal_rows(case)[:len(before)] == before
    release_event = [doc for kind, doc in events(case) if kind == "UNRESOLVED_EXPOSURE_EXPIRED"]
    assert len(release_event) == 1
    assert release_event[0]["policy_hash"] == original["policy_hash"]
    assert release_event[0]["trade_document"] == {**original, "exposure_release": release}
    assert "final_outcome" not in released and "exit_timestamp" not in released
    assert paper_summary(case.sessions)["unresolved"] == 1
    assert paper_summary(case.sessions)["closed"] == 0
    state = case.paper.get_state(day)
    assert state.monetary_pnl_complete and not state.open_strategy_keys
    assert state.realized_pnl_today == 0 and state.trades_today == 0
    assert_no_paper_exposure(case.sessions, day)
    assert_no_paper_exposure(case.sessions, snapshot_id=snapshot_id)
    # A later release marker cannot release exposure in an earlier causal evaluation.
    with pytest.raises(ValueError, match="NO_FORWARD_PAPER_EXPOSURE"):
        assert_no_paper_exposure(case.sessions, expiry)
    assert not case.paper.get_state(expiry).monetary_pnl_complete
    sequence = verify_journal(case.sessions)
    case.paper.observe(snapshot_id)
    assert verify_journal(case.sessions) == sequence
    case.paper.enqueue(snapshot_id)
    with case.sessions() as session:
        rows = session.scalars(select(PaperTrade).order_by(PaperTrade.decision_snapshot_id)).all()
        assert rows[0].state == "UNRESOLVED_EXPOSURE"
        assert rows[1].state == "PENDING_PAPER_ENTRY"
        assert json.loads(rows[1].document)["policy_hash"] == case.paper.policy_hash
        assert rows[0].document == canonical({**original, "exposure_release": release})
    # Restart and a subsequent observation do not append a second release.
    case.paper = PaperEngine(case.sessions, case.settings, clock=lambda: case.clock[0])
    next_id = observation_on(case, day + timedelta(days=1))
    case.paper.observe(next_id)
    assert sum(kind == "UNRESOLVED_EXPOSURE_EXPIRED" for kind, _ in events(case)) == 1
    verify_journal(case.sessions)


def test_failed_release_rolls_back_projection_and_journal(paper_case, monkeypatch):  # noqa: F811
    case = paper_case
    original = make_unresolved(case)
    before = journal_rows(case)
    snapshot_id = observation_on(case, case.raw.expiry + timedelta(days=1))
    emit = case.paper.emit

    def fail(*args, **kwargs):
        emit(*args, **kwargs)
        raise RuntimeError("interrupted release")

    monkeypatch.setattr(case.paper, "emit", fail)
    with pytest.raises(RuntimeError, match="interrupted release"):
        case.paper.observe(snapshot_id)
    assert trade(case) == ("UNRESOLVED_EXPOSURE", original)
    assert journal_rows(case) == before
    verify_journal(case.sessions)
    monkeypatch.setattr(case.paper, "emit", emit)
    case.paper.observe(snapshot_id)
    verify_journal(case.sessions)


@pytest.mark.parametrize("state", ["PENDING_PAPER_ENTRY", "OPEN"])
def test_pending_and_open_always_block_even_past_expiry(paper_case, state):  # noqa: F811
    case = paper_case
    if state == "OPEN":
        case.paper.observe(next_snapshot(case))
    document = trade(case)[1]
    day = case.raw.expiry + timedelta(days=10)
    assert blocks_paper_exposure(state, document, day)
    assert case.paper.get_state(day).open_strategy_keys
    with pytest.raises(ValueError, match="NO_FORWARD_PAPER_EXPOSURE"):
        assert_no_paper_exposure(case.sessions, day)


@pytest.mark.parametrize("damage", ["missing_candidate", "invalid_expiry", "leg_expiry", "missing_token"])
def test_malformed_unresolved_history_never_releases(paper_case, damage):  # noqa: F811
    original = trade(paper_case)[1]
    damaged = deepcopy(original)
    if damage == "missing_candidate":
        del damaged["candidate"]
    elif damage == "invalid_expiry":
        damaged["candidate"]["expiry"] = "unknown"
    elif damage == "leg_expiry":
        damaged["candidate"]["long_leg"]["expiry"] = "2099-12-31"
    else:
        damaged["candidate"]["long_leg"]["instrument_token"] = None
    assert unresolved_blocks_exposure(damaged, paper_case.raw.expiry + timedelta(days=10))
