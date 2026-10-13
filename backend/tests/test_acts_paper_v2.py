"""ACTS V2 qualification and paper journal regressions."""
from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.acts_paper import acts_v2
from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.acts_paper.service import ActsPaperService, verify_acts_paper_journal
from app.db.session import build_session_factory
from app.paper.models import PaperEvent, PaperTrade
from app.scalper.config import ScalperConfig
from app.scalper.models import ScalperCandidate, ScalperEvent, ScalperTrade
from app.scalper_lab.engine import _candidate
from tests.test_acts_paper import _capture, _next, _seed_position
from tests.test_scalper import settings
from tests.test_scalper_acts import acts_context, qualified_rows


def _decision(direction=1, classification="NEUTRAL"):
    rows = qualified_rows(direction, 70, classification)
    ctx = acts_context(rows)
    selected = "BULL" if direction > 0 else "BEAR"
    candidate, rejected = _candidate(
        rows[-1].snapshot, {"strategy_id": acts_v2.STRATEGY_ID,
                            "direction": selected}, ctx,
        ScalperConfig.from_settings(settings()))
    assert candidate is not None
    config = acts_v2.ActsV2Config.from_settings(settings())
    return ctx, candidate, rejected, config


@pytest.mark.parametrize("direction,expected", [(1, "STRONG_BULL"),
                                                (-1, "STRONG_BEAR")])
def test_strong_trend_can_enter_with_neutral_alpha_and_one_positioning_check(
        monkeypatch, direction, expected):
    ctx, candidate, rejected, config = _decision(direction)
    from app.scalper_lab import acts
    original = acts.positioning

    def one_check(context, side):
        evidence = original(context, side)
        evidence["passed"] = ["A"]
        evidence["checks"] = {"A": True, "B": False, "C": False, "D": False}
        evidence["opposite_checks"] = {name: False for name in "ABCD"}
        return evidence

    monkeypatch.setattr(acts, "positioning", one_check)
    decision = acts_v2.decide(ctx, {}, candidate, rejected, [], config)
    assert decision["market_state"] == expected
    assert decision["status"] == "ENTER"
    assert decision["positioning_signals_passed"] == 1
    assert decision["score_band"] in {"TRADEABLE", "STRONG", "VERY_STRONG"}
    assert decision["score_components"]["alpha"] == 8
    assert decision["positioning_score_contribution"] < 0
    assert decision["opportunity_score"] == sum(decision["score_components"].values())


@pytest.mark.parametrize("direction,label", [(1, "BULL"), (-1, "BEAR")])
def test_pullback_watch_causally_becomes_resumption(direction, label):
    ctx, candidate, rejected, config = _decision(direction)
    watch = {}
    ctx["futures_returns_bps"]["3m"] = -direction * 3
    pullback = acts_v2.decide(ctx, watch, candidate, rejected, [], config)
    assert pullback["market_state"] == f"{label}_PULLBACK"
    assert pullback["status"] == "WATCH"
    assert watch["direction"] == label
    ctx["timestamp"] = (datetime.fromisoformat(ctx["timestamp"])
                        + timedelta(seconds=15)).isoformat()
    ctx["futures_returns_bps"]["3m"] = direction * 5
    resumption = acts_v2.decide(ctx, watch, candidate, rejected, [], config)
    assert resumption["market_state"] == f"{label}_RESUMPTION"
    assert resumption["status"] == "ENTER"
    assert resumption["entry_reason"] == "PULLBACK_RESUMPTION"
    assert watch == {}


def test_opposite_alpha_reduces_score_without_automatic_veto():
    support = _decision(1, "BULL")
    opposite = _decision(1, "STRONG_BEAR")
    decisions = [acts_v2.decide(ctx, {}, candidate, rejected, [], config)
                 for ctx, candidate, rejected, config in (support, opposite)]
    assert decisions[1]["opportunity_score"] < decisions[0]["opportunity_score"]
    assert "CONFIGURED_STRONG_OPPOSITE_ALPHA_VETO" not in decisions[1]["hard_blockers"]
    config = acts_v2.ActsV2Config.from_settings(settings(
        acts_v2_veto_strong_opposite_alpha=True))
    ctx, candidate, rejected, _ = opposite
    vetoed = acts_v2.decide(ctx, {}, candidate, rejected, [], config)
    assert vetoed["status"] == "NO_SETUP"
    assert "CONFIGURED_STRONG_OPPOSITE_ALPHA_VETO" in vetoed["hard_blockers"]


def test_option_volume_pcr_is_directional_evidence():
    ctx, candidate, rejected, config = _decision()
    for radius in ("3", "5"):
        ctx["options"]["local_pcr"][radius]["volume"] = 2.0
    supportive = acts_v2.decide(ctx, {}, candidate, rejected, [], config)
    for radius in ("3", "5"):
        ctx["options"]["local_pcr"][radius]["volume"] = .5
    opposing = acts_v2.decide(ctx, {}, candidate, rejected, [], config)
    assert supportive["score_components"]["participation"] > (
        opposing["score_components"]["participation"])
    assert supportive["score_evidence"]["volume_factor"] > .5
    assert opposing["score_evidence"]["volume_factor"] < .5


def test_missing_execution_and_risk_limits_are_hard_blocks():
    ctx, candidate, rejected, config = _decision()
    no_book = acts_v2.decide(ctx, {}, None, {"STALE_QUOTE": 1}, [], config)
    assert no_book["status"] == "NO_SETUP"
    assert "NO_EXECUTABLE_CANDIDATE" in no_book["hard_blockers"]
    assert no_book["candidate_rejections"] == {"STALE_QUOTE": 1}
    limited = acts_v2.decide(ctx, {}, candidate, rejected,
                             ["MAX_TRADES_PER_DAY_REACHED", "HARD_DAILY_LOSS_REACHED"],
                             config)
    assert limited["status"] == "NO_SETUP"
    assert len(limited["hard_blockers"]) == 2


@pytest.mark.parametrize("change,reason", [
    ({"bid_quantity": 0, "ask_quantity": 0}, "INSUFFICIENT_DEPTH"),
    ({"source_market_timestamp": None}, "MISSING_QUOTE_TIMESTAMP"),
])
def test_real_book_rejection_prevents_entry(change, reason):
    rows = qualified_rows(1, 70)
    ctx = acts_context(rows)
    raw = rows[-1].snapshot
    broken = raw.model_copy(update={
        "quotes": [quote.model_copy(update=change) for quote in raw.quotes]})
    candidate, rejected = _candidate(
        broken, {"strategy_id": acts_v2.STRATEGY_ID, "direction": "BULL"}, ctx,
        ScalperConfig.from_settings(settings()))
    assert candidate is None
    assert rejected[reason] > 0
    decision = acts_v2.decide(ctx, {}, candidate, rejected, [],
                              acts_v2.ActsV2Config.from_settings(settings()))
    assert decision["status"] == "NO_SETUP"
    assert "NO_EXECUTABLE_CANDIDATE" in decision["hard_blockers"]


def test_score_is_deterministic_and_weight_validation_is_strict():
    ctx, candidate, rejected, config = _decision()
    first = acts_v2.decide(ctx, {}, candidate, rejected, [], config)
    second = acts_v2.decide(ctx, {}, candidate, rejected, [], config)
    assert first == second
    invalid = settings(acts_v2_score_weights_json='{"momentum_15m":100}')
    with pytest.raises(ValueError, match="ACTS_V2_SCORE_WEIGHTS_INVALID"):
        acts_v2.ActsV2Config.from_settings(invalid)


def test_v2_paper_fills_and_restart_preserve_journal(engine):
    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(ActsPaperCursor(id=1, sequence=0, head_hash="GENESIS",
                                    lock_version=0, watch_json="{}", latest_json="{}"))
    config = settings(acts_paper_enabled=True)
    service = ActsPaperService(sessions, config)
    rows = qualified_rows(1, 70, "NEUTRAL")
    for row in rows:
        snapshot_id = _capture(engine, row.snapshot)
    assert service.process_snapshot(snapshot_id) == "PENDING_PAPER_ENTRY"
    with Session(engine) as session:
        trade = session.scalar(select(ActsPaperTrade))
        assert trade is not None
        doc = json.loads(trade.document)
        assert doc["strategy_version"] == acts_v2.STRATEGY_ID
        assert doc["opportunity_score"] >= config.acts_v2_tradeable_score
        assert doc["score_components"]
        candidate = doc["candidate"]
    selected = ScalperCandidate.model_validate(candidate)
    next_raw = _next(rows[-1].snapshot, candidate=selected)
    assert service.process_snapshot(_capture(engine, next_raw)) == "NO_SETUP"
    latest = service.status(next_raw.captured_at)["latest_observation"]
    assert latest["primary_blocker"] == "MAX_OPEN_POSITIONS_REACHED"
    with Session(engine) as session:
        trade = session.scalar(select(ActsPaperTrade))
        assert trade is not None and trade.state == "OPEN"
        doc = json.loads(trade.document)
        assert doc["entry_quotes"]["short"]["side"] == "bid"
        assert doc["entry_quotes"]["long"]["side"] == "ask"
        assert doc["entry_credit"] == pytest.approx(
            doc["entry_quotes"]["short"]["fill_price"]
            - doc["entry_quotes"]["long"]["fill_price"])
        events = session.scalars(select(ActsPaperEvent)).all()
        assert any(json.loads(event.payload).get("decision", {}).get("score_components")
                   for event in events)
        for table in (PaperTrade, PaperEvent, ScalperTrade, ScalperEvent):
            assert session.scalar(select(func.count()).select_from(table)) == 0
    restarted = ActsPaperService(sessions, config)
    verify_acts_paper_journal(sessions)
    assert restarted.status(next_raw.captured_at)["current_state"] == "OPEN"
    assert len(restarted.required_contracts()) == 2
    close_at = next_raw.captured_at.replace(hour=15, minute=21, second=0)
    forced = _next(next_raw, seconds=int((close_at - next_raw.captured_at).total_seconds()),
                   candidate=selected)
    restarted.process_snapshot(_capture(engine, forced))
    with Session(engine) as session:
        closed = session.scalar(select(ActsPaperTrade))
        assert closed is not None and closed.state == "CLOSED"
        result = json.loads(closed.document)
        assert result["exit_reason"] == "FORCED_CLOSE"
        assert result["exit_quotes"]["short"]["side"] == "ask"
        assert result["exit_quotes"]["long"]["side"] == "bid"
        assert result["exit_debit"] == pytest.approx(
            result["exit_quotes"]["short"]["fill_price"]
            - result["exit_quotes"]["long"]["fill_price"])
    verify_acts_paper_journal(sessions)


def test_existing_v1_open_trade_keeps_policy_and_journal_under_v2(engine):
    service, raw, candidate, _ = _seed_position(engine)
    with Session(engine) as session:
        original = session.scalar(select(ActsPaperTrade))
        assert original is not None
        document = original.document
        journal = [event.event_hash for event in session.scalars(select(ActsPaperEvent))]
    v2 = ActsPaperService(service.sessions, settings(acts_paper_enabled=True))
    assert v2.v1_policy_hash == service.policy_hash
    assert v2.required_contracts()
    with Session(engine) as session:
        current = session.scalar(select(ActsPaperTrade))
        assert current is not None and current.document == document
        assert [event.event_hash for event in session.scalars(select(ActsPaperEvent))] == journal
    v2.process_snapshot(_capture(engine, _next(raw, candidate=candidate)))
    with Session(engine) as session:
        current = session.scalar(select(ActsPaperTrade))
        assert current is not None and current.state == "OPEN"
        assert json.loads(current.document)["strategy_version"] == "ACTS_V1"
    verify_acts_paper_journal(service.sessions)


def test_v1_unresolved_exposure_is_terminal_under_v2(engine):
    service, raw, candidate, _ = _seed_position(engine)
    service.process_snapshot(_capture(engine, _next(
        raw, candidate=candidate, missing="long_leg")))
    with Session(engine) as session:
        unresolved = session.scalar(select(ActsPaperTrade))
        assert unresolved is not None and unresolved.state == "UNRESOLVED_EXPOSURE"
        original_document = unresolved.document
        hashes = [event.event_hash for event in session.scalars(select(ActsPaperEvent))]
    v2 = ActsPaperService(service.sessions, settings(acts_paper_enabled=True))
    v2.process_snapshot(_capture(engine, _next(raw, seconds=30, candidate=candidate)))
    with Session(engine) as session:
        unresolved = session.scalar(select(ActsPaperTrade))
        assert unresolved is not None and unresolved.document == original_document
        assert unresolved.state == "UNRESOLVED_EXPOSURE"
        assert [event.event_hash for event in session.scalars(
            select(ActsPaperEvent).order_by(ActsPaperEvent.sequence))][:len(hashes)] == hashes
    verify_acts_paper_journal(service.sessions)
