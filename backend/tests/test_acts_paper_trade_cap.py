"""ACTS V2's bounded daily cap is independent of its other execution protections."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import select

from app.acts_paper import acts_v2
from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.acts_paper.service import ActsPaperService, verify_acts_paper_journal
from app.db.session import build_session_factory
from app.parallel_paper.config import ExperimentConfig, STRATEGIES
from app.research.manifest import canonical, digest, json_value
from app.scalper_lab.engine import LabTrade, Ledger, _candidate, _risk_blockers
from tests.test_acts_paper import _capture, _next
from tests.test_mirror_exit_v1 import mark_raw, open_acts
from tests.test_scalper import settings
from tests.test_scalper_acts import acts_context, qualified_rows


def service(engine, **overrides):
    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(ActsPaperCursor(id=1, sequence=0, head_hash="GENESIS", lock_version=0,
                                    watch_json="{}", latest_json="{}"))
    return ActsPaperService(sessions, settings(**overrides))


def qualified(paper):
    rows = qualified_rows(1, 70, "NEUTRAL")
    ctx = acts_context(rows)
    candidate, rejected = _candidate(rows[-1].snapshot, {"strategy_id": "ACTS_V2", "direction": "BULL"},
                                     ctx, paper.config)
    assert candidate is not None and candidate.spread_width == 400
    return ctx, candidate, rejected


def closed(candidate, at, index, pnl=1, *, recent=False):
    exit_at = at - timedelta(seconds=1 if recent else 600 + index * 15)
    return LabTrade("ACTS_V2", candidate, {"timestamp": at.isoformat()}, index, state="CLOSED",
                    entry_timestamp=exit_at - timedelta(seconds=30), exit_timestamp=exit_at,
                    outcome={"gross_rupees": pnl})


@pytest.mark.parametrize("completed", range(7))
def test_first_six_qualify_and_seventh_is_blocked(engine, completed):
    paper = service(engine)
    ctx, candidate, rejected = qualified(paper)
    at = datetime.fromisoformat(ctx["timestamp"])
    ledger = Ledger(trades=[closed(candidate, at, i) for i in range(completed)])
    risk = _risk_blockers(ledger, paper.config, at, False)
    decision = acts_v2.decide(ctx, {}, candidate, rejected, risk, paper.v2_config)
    assert decision["status"] == ("ENTER" if completed < 6 else "NO_SETUP")
    assert decision["hard_blockers"] == ([] if completed < 6 else ["MAX_TRADES_PER_DAY_REACHED"])


@pytest.mark.parametrize("base_cap,expected", [(0, 0), (2, 2), (3, 3), (6, 6), (10, 6)])
def test_effective_cap_respects_lower_configured_limit(engine, base_cap, expected):
    paper = service(engine, scalper_max_trades_per_day=base_cap)
    assert paper.config.max_trades_per_day == expected
    assert paper.policy["config"]["max_trades_per_day"] == expected
    assert paper.policy_hash == digest(paper.policy)
    old = deepcopy(paper.policy)
    old["config"]["max_trades_per_day"] = min(base_cap, 3)
    assert (paper.policy_hash != digest(old)) == (base_cap > 3)
    assert json_value(replace(paper.config, max_trades_per_day=min(base_cap, 3)).payload()) == old["config"]


@pytest.mark.parametrize("kind,reason", [
    ("PENDING_ENTRY", "MAX_OPEN_POSITIONS_REACHED"), ("OPEN", "MAX_OPEN_POSITIONS_REACHED"),
    ("UNRESOLVED", "MAX_OPEN_POSITIONS_REACHED"), ("exit", "ANY_EXIT_COOLDOWN_ACTIVE"),
    ("loss", "LOSS_COOLDOWN_ACTIVE"), ("streak", "MAX_CONSECUTIVE_LOSSES_REACHED"),
    ("daily", "HARD_DAILY_LOSS_REACHED"),
])
def test_other_risk_gates_are_identical_and_still_block(engine, kind, reason):
    paper = service(engine)
    ctx, candidate, rejected = qualified(paper)
    at = datetime.fromisoformat(ctx["timestamp"])
    if kind in {"PENDING_ENTRY", "OPEN", "UNRESOLVED"}:
        ledger = Ledger(trades=[LabTrade("ACTS_V2", candidate, {"timestamp": at.isoformat()}, 1, state=kind)])
    elif kind == "streak":
        ledger = Ledger(trades=[closed(candidate, at, i, -1) for i in range(paper.config.max_consecutive_losses)])
    else:
        pnl = -paper.config.hard_daily_loss if kind == "daily" else -1 if kind == "loss" else 1
        ledger = Ledger(trades=[closed(candidate, at, 1, pnl, recent=kind != "daily")])
    reasons = _risk_blockers(ledger, paper.config, at, False)
    previous = _risk_blockers(ledger, replace(paper.config, max_trades_per_day=3), at, False)
    assert reason in reasons
    assert set(reasons) == set(previous) - {"MAX_TRADES_PER_DAY_REACHED"}
    assert acts_v2.decide(ctx, {}, candidate, rejected, reasons, paper.v2_config)["status"] == "NO_SETUP"


def test_v1_and_parallel_trade_caps_remain_unchanged(engine):
    paper = service(engine, acts_paper_strategy_version="ACTS_V1")
    assert paper.config.max_trades_per_day == 3 and paper.policy_hash == paper.v1_policy_hash
    experiment = ExperimentConfig()
    assert all(experiment.execution(settings(), strategy).max_trades_per_day == 6 for strategy in STRATEGIES)
    custom = ExperimentConfig.model_validate({"risk": {"ORB_15": {"max_trades_per_day": 2}}})
    assert custom.execution(settings(), "ORB_15").max_trades_per_day == 2


def test_new_policy_is_persisted_without_rewriting_historical_policy_or_events(engine, monkeypatch):
    paper, raw, candidate = open_acts(engine)
    forced_at = raw.captured_at.replace(hour=15, minute=20, second=0)
    forced = mark_raw(raw, candidate, int((forced_at - raw.captured_at).total_seconds()), 40)
    paper.process_snapshot(_capture(engine, forced))
    # Fixture of a genuinely priced historical CLOSED trade under the prior cap.
    with paper.sessions.begin() as session:
        trade = session.scalar(select(ActsPaperTrade))
        assert trade.state == "CLOSED"
        doc = json.loads(trade.document)
        doc["policy"]["config"]["max_trades_per_day"] = 3
        doc["policy_hash"] = digest(doc["policy"])
        trade.document = canonical(doc)
        paper._emit(session, session.get(ActsPaperCursor, 1), "ACTS_TEST_PRIOR_CAP",
                    trade.decision_snapshot_id, raw, trade)
        old_id, old_document, old_hash = trade.id, trade.document, doc["policy_hash"]
    with paper.sessions() as session:
        before = [(event.payload, event.event_hash) for event in session.scalars(
            select(ActsPaperEvent).order_by(ActsPaperEvent.sequence))]
    restarted = ActsPaperService(paper.sessions, settings())
    assert restarted.policy_hash != old_hash
    ctx, _, _ = qualified(restarted)
    following = _next(raw, seconds=86400)
    ctx["timestamp"] = following.captured_at.isoformat()
    monkeypatch.setattr(restarted, "_context", lambda _: deepcopy(ctx))
    assert restarted.process_snapshot(_capture(engine, following)) == "PENDING_PAPER_ENTRY"
    with paper.sessions() as session:
        assert session.get(ActsPaperTrade, old_id).document == old_document
        pending = session.scalar(select(ActsPaperTrade).where(ActsPaperTrade.state == "PENDING_PAPER_ENTRY"))
        new_doc = json.loads(pending.document)
        assert new_doc["policy"]["config"]["max_trades_per_day"] == 6
        assert new_doc["policy_hash"] == restarted.policy_hash == digest(new_doc["policy"])
        assert [(event.payload, event.event_hash) for event in session.scalars(
            select(ActsPaperEvent).order_by(ActsPaperEvent.sequence))][:len(before)] == before
    verify_acts_paper_journal(paper.sessions)


def test_active_old_cap_policy_cannot_execute_under_new_cap(engine):
    paper, raw, candidate = open_acts(engine)
    with paper.sessions.begin() as session:
        trade = session.scalar(select(ActsPaperTrade))
        doc = json.loads(trade.document)
        doc["policy"]["config"]["max_trades_per_day"] = 3
        doc["policy_hash"] = digest(doc["policy"])
        trade.document = canonical(doc)
        paper._emit(session, session.get(ActsPaperCursor, 1), "ACTS_TEST_PRIOR_CAP",
                    trade.decision_snapshot_id, raw, trade)
        old_policy = deepcopy(doc["policy"])
    restarted = ActsPaperService(paper.sessions, settings())
    restarted.process_snapshot(_capture(engine, _next(raw, candidate=candidate)))
    with paper.sessions() as session:
        trade = session.scalar(select(ActsPaperTrade))
        doc = json.loads(trade.document)
        assert trade.state == "UNRESOLVED_EXPOSURE" and doc["unresolved_reason"] == "ACTS_PAPER_POLICY_CHANGED"
        assert doc["policy"] == old_policy and doc["policy_hash"] == digest(old_policy)
    verify_acts_paper_journal(paper.sessions)
