"""Read-only monitor coverage using stored projection rows and immutable events."""
from datetime import datetime, timedelta
import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.db.session import get_session_factory
from app.main import app
from app.parallel_paper.config import STRATEGIES
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperEvent, ParallelPaperTrade
from app.parallel_paper.monitor import summary
from app.scalper.models import ScalperMarketSnapshotRecord
from app.scalper.repository import IST


AT = datetime(2099, 10, 1, 10, 0, tzinfo=IST)


def seed(sessions, at=AT):
    with sessions.begin() as session:
        session.add_all([ParallelPaperCursor(strategy_id=name, sequence=0, head_hash="GENESIS",
                         lock_version=0, state_json="{}", latest_json=json.dumps({
                             "status": "WATCH", "blockers": ["BAR_ALREADY_EVALUATED"],
                             "timestamp": at.isoformat()}), observed_at=at.isoformat())
                         for name in STRATEGIES])
        session.add(ActsPaperCursor(id=1, sequence=0, head_hash="GENESIS", lock_version=0,
                         watch_json="{}", latest_json=json.dumps({
                             "strategy_version": "ACTS_V2", "status": "WATCH",
                             "primary_blocker": "VOLUME_CONFIRMATION_REQUIRED",
                             "timestamp": at.isoformat()}), observed_at=at.isoformat()))
        for index in (1, 2):
            observed = at + timedelta(seconds=index * 15)
            session.add(ScalperMarketSnapshotRecord(id=index, capture_key=f"test-{index}",
                captured_at=observed, request_started_at=observed, response_received_at=observed,
                nifty_spot=25000, lot_size=50, atm_strike=25000, expiry=at.date(),
                source="TEST", feature_json="{}", signal_json="{}"))


def trade_doc(at=AT, *, gross=120, mark=None, entry=True, exit=False):
    return {"strategy_version": "ACTS_V2", "direction": "BULL",
            "decision_timestamp": at.isoformat(),
            "entry_timestamp": at.isoformat() if entry else None,
            "exit_timestamp": (at + timedelta(minutes=5)).isoformat() if exit else None,
            "candidate": {"short_leg": {"strike": 24900}, "long_leg": {"strike": 24800},
                          "quote_evidence": {"secret": "MUST_NOT_ESCAPE"}},
            "short_strike": 24900, "hedge_strike": 24800,
            "entry_credit": 12, "current_debit": 10, "exit_debit": 9 if exit else None,
            "gross_mark_rupees": mark, "final_outcome": {"gross_rupees": gross} if exit else None,
            "mfe": 3, "mae": -1, "entry_reason": "STRUCTURE_CONFIRMED",
            "exit_reason": "PROFIT_CAPTURE" if exit else None,
            "policy": {"secret": "MUST_NOT_ESCAPE"}}


def parallel_trade(sessions, strategy, trade_id, state, doc):
    with sessions.begin() as session:
        session.add(ParallelPaperTrade(id=trade_id, strategy_id=strategy,
            strategy_version=f"{strategy}_V1", decision_snapshot_id=1,
            state=state, document=json.dumps(doc)))


def acts_trade(sessions, trade_id, state, doc):
    with sessions.begin() as session:
        session.add(ActsPaperTrade(id=trade_id, decision_snapshot_id=2,
            state=state, document=json.dumps(doc)))


def parallel_event(sessions, strategy, sequence, kind, payload, *, snapshot_id=1, trade_id=None):
    with sessions.begin() as session:
        session.add(ParallelPaperEvent(strategy_id=strategy, sequence=sequence,
            event_key=f"{snapshot_id}:{sequence}:{kind}", event_type=kind,
            snapshot_id=snapshot_id, trade_id=trade_id,
            previous_hash="a" * 64, event_hash="b" * 64, payload=json.dumps(payload)))


def acts_event(sessions, sequence, kind, payload, *, snapshot_id=2, trade_id=None):
    with sessions.begin() as session:
        session.add(ActsPaperEvent(sequence=sequence,
            event_key=f"{snapshot_id}:{sequence}:{kind}", event_type=kind,
            snapshot_id=snapshot_id, trade_id=trade_id,
            previous_hash="a" * 64, event_hash="b" * 64, payload=json.dumps(payload)))


def test_six_strategies_and_compact_gross_totals(session_factory):
    seed(session_factory)
    parallel_trade(session_factory, "ORB_15", "closed", "CLOSED", trade_doc(gross=120, exit=True))
    acts_trade(session_factory, "acts-open", "OPEN", trade_doc(mark=-35))
    result = summary(session_factory, AT)
    assert [row["strategy_id"] for row in result["strategies"]] == ["ACTS_V2", *STRATEGIES]
    assert result["totals"] == {"realized_gross_pnl_today": 120,
        "unrealized_gross_pnl": -35, "combined_gross_pnl": 85,
        "open_positions": 1, "trades_today": 2}
    assert result["accounting_status"] == "GROSS_ONLY"
    assert all(row["accounting_status"] == "GROSS_ONLY" for row in result["strategies"])
    assert all("net" not in str(key).lower() for key in result["totals"])
    assert "MUST_NOT_ESCAPE" not in json.dumps(result)
    detail = result["strategies"][0]["today_trades"][0]
    assert detail["state"] == "OPEN" and detail["current_unrealized_gross_pnl"] == -35
    assert detail["short_strike"] == 24900 and detail["hedge_strike"] == 24800
    assert detail["mfe"] == 3 and detail["mae"] == -1


def test_unknown_mark_and_unresolved_exposure_propagate(session_factory):
    seed(session_factory)
    parallel_trade(session_factory, "ORB_15", "open", "OPEN", trade_doc(mark=55))
    parallel_event(session_factory, "ORB_15", 1, "MARK",
        {"available": False, "reason": "MISSING_EXACT_CONTRACT"}, trade_id="open")
    result = summary(session_factory, AT)
    assert result["strategies"][1]["unrealized_gross_pnl"] is None
    assert result["strategies"][1]["today_trades"][0]["current_debit"] is None
    assert result["totals"]["combined_gross_pnl"] is None

    acts_trade(session_factory, "unresolved", "UNRESOLVED_EXPOSURE", trade_doc(mark=999))
    result = summary(session_factory, AT)
    assert result["totals"]["open_positions"] == 2
    assert result["strategies"][0]["unrealized_gross_pnl"] is None


def test_stored_mirror_policy_and_evidence_are_reported_without_reinterpreting_legacy(session_factory):
    seed(session_factory)
    doc = trade_doc(exit=True)
    doc.update(exit_policy_version="MIRROR_EXIT_V1", exit_reason="OPPOSITE_ORB_BREAKOUT",
               best_gross_points=4, mirror_diagnostics={"opposite_breakout_first_at": AT.isoformat()})
    parallel_trade(session_factory, "ORB_15", "mirror", "CLOSED", doc)
    acts_trade(session_factory, "legacy", "CLOSED", trade_doc(exit=True))
    result = summary(session_factory, AT)
    current = result["strategies"][1]["today_trades"][0]
    assert current["exit_policy_version"] == "MIRROR_EXIT_V1"
    assert current["exit_reason"] == "OPPOSITE_ORB_BREAKOUT"
    assert current["best_gross_points"] == 4
    assert current["mirror_diagnostics"] == doc["mirror_diagnostics"]
    legacy = result["strategies"][0]["today_trades"][0]
    assert legacy["exit_policy_version"] is None
    assert legacy["exit_reason"] == "PROFIT_CAPTURE"


def test_entry_debit_is_not_presented_as_a_current_quote_before_first_mark(session_factory):
    seed(session_factory)
    acts_trade(session_factory, "fresh-open", "OPEN", trade_doc(mark=None))
    result = summary(session_factory, AT)
    detail = result["strategies"][0]["today_trades"][0]
    assert detail["entry_credit"] == 12
    assert detail["current_debit"] is None
    assert detail["current_unrealized_gross_pnl"] is None
    assert result["totals"]["combined_gross_pnl"] is None


def test_parallel_decision_rejections_count_without_trade_rows(session_factory):
    seed(session_factory)
    parallel_event(session_factory, "ORB_30", 1, "OBSERVATION",
        {"decision": {"status": "NO_SETUP", "blockers": ["NO_SETUP"]}})
    parallel_event(session_factory, "ORB_30", 2, "OBSERVATION",
        {"decision": {"status": "WATCH", "blockers": ["BAR_ALREADY_EVALUATED"]}})
    parallel_event(session_factory, "ORB_30", 3, "OBSERVATION",
        {"decision": {"status": "ENTRY_REJECTED", "blockers": ["MAX_TRADES_PER_DAY_REACHED"]}})
    parallel_event(session_factory, "ORB_30", 4, "OBSERVATION",
        {"decision": {"status": "WATCH", "blockers": ["ORB_REENTRY_DISABLED_OR_SAME_BREAKOUT"]}})
    row = next(item for item in summary(session_factory, AT)["strategies"] if item["strategy_id"] == "ORB_30")
    assert row["entry_rejections_today"] == 1
    assert row["latest_rejection_reason"] == "MAX_TRADES_PER_DAY_REACHED"
    assert row["latest_blocker"] == "BAR_ALREADY_EVALUATED"
    assert row["trades_today"] == 0


def test_later_fill_rejection_and_acts_rejections_are_counted_once(session_factory):
    seed(session_factory)
    parallel_trade(session_factory, "ORB_15", "rejected", "ENTRY_REJECTED", trade_doc(entry=False))
    parallel_event(session_factory, "ORB_15", 1, "ENTRY_REJECTED",
        {"reason": "FILL_ECONOMICS_OUTSIDE_RISK"}, trade_id="rejected")
    acts_trade(session_factory, "acts-rejected", "ENTRY_REJECTED", trade_doc(entry=False))
    acts_event(session_factory, 1, "ACTS_ENTRY_REJECTED",
        {"strategy_version": "ACTS_V2", "reason": "UNKNOWN_REQUIRED_DEPTH"}, trade_id="acts-rejected")
    acts_event(session_factory, 2, "ACTS_OBSERVATION",
        {"strategy_version": "ACTS_V2", "latest": {"status": "ENTRY_REJECTED"}})
    result = summary(session_factory, AT)
    assert result["strategies"][0]["entry_rejections_today"] == 1
    assert result["strategies"][1]["entry_rejections_today"] == 1
    assert result["strategies"][0]["trades_today"] == result["strategies"][1]["trades_today"] == 0


def test_missing_cursor_and_bad_single_strategy_leave_others_available(session_factory):
    seed(session_factory)
    with session_factory.begin() as session:
        session.delete(session.get(ParallelPaperCursor, "EMA_PULLBACK"))
        session.get(ParallelPaperCursor, "ORB_60").latest_json = "{bad"
    result = summary(session_factory, AT)
    unavailable = {row["strategy_id"]: row for row in result["strategies"]}
    assert unavailable["EMA_PULLBACK"]["trades_today"] is None
    assert unavailable["ORB_60"]["latest_status"] is None
    assert unavailable["ORB_15"]["trades_today"] == 0
    assert result["totals"]["combined_gross_pnl"] is None


def test_unobserved_strategy_and_acts_v1_are_not_presented_as_v2_data(session_factory):
    seed(session_factory)
    with session_factory.begin() as session:
        session.get(ParallelPaperCursor, "ORB_60").observed_at = None
        session.get(ActsPaperCursor, 1).latest_json = json.dumps({
            "strategy_version": "ACTS_V1", "status": "WATCH"})
    result = summary(session_factory, AT)
    assert result["strategies"][0]["trades_today"] is None
    assert result["strategies"][3]["trades_today"] is None
    assert result["strategies"][1]["trades_today"] == 0


def test_endpoint_reads_only_and_missing_schema_is_503(session_factory):
    seed(session_factory)
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    flushed = []
    def on_flush(*_):
        flushed.append(True)
        raise AssertionError("monitor attempted a write")
    event.listen(session_factory.class_, "before_flush", on_flush)
    try:
        result = TestClient(app).get("/api/phase15/summary")
        assert result.status_code == 200 and len(result.json()["strategies"]) == 6
        assert not flushed
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ParallelPaperEvent)) == 0
    finally:
        event.remove(session_factory.class_, "before_flush", on_flush)
        app.dependency_overrides.clear()

    empty_engine = create_engine("sqlite+pysqlite:///:memory:")
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker(bind=empty_engine)
    try:
        result = TestClient(app).get("/api/phase15/summary")
        assert result.status_code == 503
        assert result.json() == {"detail": "Phase 15 monitor unavailable"}
    finally:
        app.dependency_overrides.clear()
        empty_engine.dispose()
