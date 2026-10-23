"""Positions/journal are bounded, side-effect-free views of stored evidence."""
from datetime import timedelta
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select

from app.acts_paper.models import ActsPaperEvent, ActsPaperTrade
from app.db.session import get_session_factory
from app.main import app
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperEvent, ParallelPaperTrade
from app.parallel_paper.monitor import ORDER, summary
from app.parallel_paper.trade_monitor import trades
from app.scalper.models import ScalperMarketSnapshotRecord
from tests.test_phase15_monitor import AT, seed, trade_doc


def add(sessions, ident, *, strategy="ACTS_V2", state="OPEN", at=AT, marked=True, width=400, mark_quotes=None):
    doc = trade_doc(at, mark=75, entry=state != "PENDING_PAPER_ENTRY", exit=state == "CLOSED")
    doc.update(lot_size=50, lots=1, policy_hash="stored-policy-hash", exit_policy_version="MIRROR_EXIT_V1",
               policy={"config": {"allowed_widths": [width]}, "secret": "DO_NOT_RETURN"},
               unresolved_reason="UNKNOWN_REQUIRED_DEPTH" if state == "UNRESOLVED_EXPOSURE" else None,
               rejection_reason="STALE_QUOTE" if state == "ENTRY_REJECTED" else None,
               mirror_diagnostics={"opposite_full_entry_signal_first_at": at.isoformat(), "secret": "DO_NOT_RETURN"},
               entry_quotes={"short": {"fill_price": 12, "side": "bid", "secret": "DO_NOT_RETURN"}})
    doc["candidate"]["spread_width"] = width
    doc["candidate"]["long_leg"]["strike"] = doc["hedge_strike"] = 24900 - width
    with sessions.begin() as session:
        snapshot = ScalperMarketSnapshotRecord(
            capture_key=f"trade-{strategy}-{ident}", captured_at=at,
            request_started_at=at, response_received_at=at, nifty_spot=25000, lot_size=50,
            atm_strike=25000, expiry=at.date(), source="TEST", feature_json="{}", signal_json="{}")
        session.add(snapshot)
        session.flush()
        doc["last_snapshot_id"] = snapshot.id
        model = ActsPaperTrade if strategy == "ACTS_V2" else ParallelPaperTrade
        session.add(model(
            id=ident, state=state, document=json.dumps(doc), decision_snapshot_id=snapshot.id,
            **({} if strategy == "ACTS_V2" else {"strategy_id": strategy, "strategy_version": f"{strategy}_V1"})))
        session.flush()
        if marked:
            event_model = ActsPaperEvent if strategy == "ACTS_V2" else ParallelPaperEvent
            session.add(event_model(
                sequence=snapshot.id, event_key=f"{ident}:mark",
                event_type="ACTS_MARK" if strategy == "ACTS_V2" else "MARK", trade_id=ident,
                snapshot_id=snapshot.id, payload=json.dumps({"available": True, "quotes": mark_quotes or {}}),
                previous_hash="a" * 64, event_hash="b" * 64,
                **({} if strategy == "ACTS_V2" else {"strategy_id": strategy})))
    return doc


@pytest.mark.parametrize("minutes", [15, 30, 60])
def test_trade_range_is_frozen_and_option_quotes_remain_separate_read_only_evidence(session_factory, minutes):
    seed(session_factory)
    strategy = f"ORB_{minutes}"
    doc = add(session_factory, "frozen", strategy=strategy, mark_quotes={
        "short": {"quote": {"ask": 100.20}, "fill_price": 100.20, "side": "ask"},
        "long": {"quote": {"bid": 31}, "fill_price": 31, "side": "bid"}})
    start = AT.replace(hour=9, minute=15)
    features = {"range_minutes": minutes, "range_start": start.isoformat(),
                "range_end": (start + timedelta(minutes=minutes)).isoformat(), "complete": True,
                "opening_high": 25684.40, "opening_low": 25621.15, "confirmation_seconds": 45,
                "secret": "DO_NOT_RETURN"}
    doc["strategy_features"] = features
    doc["candidate"]["short_leg"]["option_type"] = "CE"
    doc["candidate"]["long_leg"]["option_type"] = "CE"
    doc["entry_quotes"] = {"short": {"fill_price": 100, "side": "bid"},
                           "long": {"fill_price": 30, "side": "ask"}}
    doc["entry_credit"], doc["current_debit"] = 70, 69.2
    with session_factory.begin() as session:
        session.get(ParallelPaperTrade, "frozen").document = json.dumps(doc)
        session.get(ParallelPaperCursor, strategy).latest_json = json.dumps({"status": "NO_SETUP", "features": {
            "opening_high": 99999, "opening_low": 88888, "complete": True}})
    with session_factory() as session:
        before_doc = session.get(ParallelPaperTrade, "frozen").document
        before_events = [(row.sequence, row.payload, row.event_hash)
                         for row in session.scalars(select(ParallelPaperEvent))]
    writes = []

    def refuse_write(*_):
        writes.append(True)
        raise AssertionError("read-only ORB projection attempted a write")

    event.listen(session_factory.class_, "before_flush", refuse_write)
    try:
        result = trades(session_factory, AT, positions=True)["items"][0]
        compact = next(row for row in summary(session_factory, AT)["strategies"]
                       if row["strategy_id"] == strategy)["today_trades"][0]
    finally:
        event.remove(session_factory.class_, "before_flush", refuse_write)
    for row in (result, compact):
        assert row["strategy_features"]["opening_high"] == 25684.40
        assert row["strategy_features"]["opening_low"] == 25621.15
        assert row["strategy_features"]["range_end"] == features["range_end"]
        assert "DO_NOT_RETURN" not in json.dumps(row["strategy_features"])
    assert result["entry_quotes"]["short"]["fill_price"] == 100
    assert result["latest_mark_quotes"]["short"]["ask"] == 100.20
    assert result["latest_mark_quotes"]["long"]["bid"] == 31
    assert result["short_option_type"] == result["hedge_option_type"] == "CE"
    assert not writes
    with session_factory() as session:
        assert session.get(ParallelPaperTrade, "frozen").document == before_doc
        after_events = [(row.sequence, row.payload, row.event_hash)
                        for row in session.scalars(select(ParallelPaperEvent))]
        assert after_events == before_events


def test_positions_include_today_and_prior_day_independently_of_cursors(session_factory):
    seed(session_factory)
    add(session_factory, "today")
    add(session_factory, "prior", strategy="ORB_15", at=AT - timedelta(days=1))
    add(session_factory, "closed", state="CLOSED")
    result = trades(session_factory, AT, positions=True)
    assert {row["trade_id"] for row in result["items"]} == {"today", "prior"}
    assert next(row for row in result["items"] if row["trade_id"] == "prior")["current_unrealized_gross_pnl"] is None
    assert result["mode"] == "PAPER" and result["accounting_status"] == "GROSS_ONLY"
    assert "DO_NOT_RETURN" not in json.dumps(result)


@pytest.mark.parametrize("state", ["PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE"])
def test_all_active_states_are_visible_without_latest_strategy_cursor(session_factory, state):
    seed(session_factory)
    add(session_factory, "active", state=state)
    result = trades(session_factory, AT, positions=True)["items"][0]
    assert result["state"] == state
    assert result["current_unrealized_gross_pnl"] == (75 if state == "OPEN" else None)
    assert result["mark_available"] is (state == "OPEN")
    assert result["spread_width"] == 400 and result["policy_allowed_widths"] == [400]
    if state == "UNRESOLVED_EXPOSURE":
        assert result["unresolved_reason"] == "UNKNOWN_REQUIRED_DEPTH"


@pytest.mark.parametrize("case", ["absent", "failed", "prior-day", "newer-observation", "future"])
def test_unavailable_marks_never_reuse_stale_pnl(session_factory, case):
    seed(session_factory)
    captured_at = (AT - timedelta(days=1) if case == "prior-day"
                   else AT + timedelta(seconds=15) if case == "future" else AT)
    doc = add(session_factory, "active", at=captured_at,
              marked=case != "absent")
    with session_factory.begin() as session:
        if case == "failed":
            session.add(ActsPaperEvent(
                sequence=999, event_key="failed", event_type="ACTS_MARK",
                snapshot_id=doc["last_snapshot_id"], trade_id="active", previous_hash="a" * 64,
                event_hash="b" * 64, payload=json.dumps({"available": False, "reason": "STALE_QUOTE"})))
        if case == "newer-observation":
            doc["last_snapshot_id"] = 999
            session.get(ActsPaperTrade, "active").document = json.dumps(doc)
    row = trades(session_factory, AT, positions=True)["items"][0]
    assert row["current_debit"] is None and row["current_unrealized_gross_pnl"] is None
    assert row["mark_available"] is False


def test_journal_includes_all_six_strategies_history_and_exact_stored_widths(session_factory):
    seed(session_factory)
    for index, strategy in enumerate(ORDER):
        add(session_factory, strategy, strategy=strategy, state="CLOSED", width=100 if index == 0 else 400,
            at=AT - timedelta(days=6 - index))
    result = trades(session_factory, AT)
    assert [row["trade_id"] for row in result["items"]] == list(reversed(ORDER))
    assert len({(row["strategy_id"], row["trade_id"]) for row in result["items"]}) == 6
    historical = result["items"][-1]
    assert historical["spread_width"] == 100 and historical["policy_allowed_widths"] == [100]
    assert historical["realized_gross_pnl"] == 120 and historical["current_unrealized_gross_pnl"] is None


def test_journal_date_strategy_state_filters_and_pagination(session_factory):
    seed(session_factory)
    add(session_factory, "yesterday", state="CLOSED", at=AT - timedelta(days=1))
    add(session_factory, "first", state="CLOSED")
    add(session_factory, "second", strategy="ORB_15", at=AT + timedelta(minutes=1))
    add(session_factory, "rejected", strategy="EMA_PULLBACK", state="ENTRY_REJECTED", at=AT + timedelta(minutes=2))
    assert len(trades(session_factory, AT, day=AT.date())["items"]) == 3
    assert [row["trade_id"] for row in trades(session_factory, AT, strategy="ORB_15")["items"]] == ["second"]
    closed = trades(session_factory, AT, state="CLOSED", day=AT.date())["items"]
    assert [row["trade_id"] for row in closed] == ["first"]
    assert trades(session_factory, AT, state="ENTRY_REJECTED")["items"][0]["rejection_reason"] == "STALE_QUOTE"
    page = trades(session_factory, AT, limit=2)
    rest = trades(session_factory, AT, limit=2, offset=2)
    assert page["has_more"] and not rest["has_more"]
    assert [row["trade_id"] for row in page["items"] + rest["items"]] == ["rejected", "second", "first", "yesterday"]


def test_date_filter_uses_ist_midnight_boundaries_and_positions_ignore_it(session_factory):
    seed(session_factory)
    start = AT.replace(hour=0)
    add(session_factory, "before", strategy="ORB_15", at=start - timedelta(seconds=1))
    add(session_factory, "start", at=start)
    add(session_factory, "last", strategy="ORB_30", at=start + timedelta(days=1, seconds=-1))
    add(session_factory, "after", strategy="EMA_PULLBACK", at=start + timedelta(days=1))
    assert {row["trade_id"] for row in trades(session_factory, AT, day=AT.date())["items"]} == {"start", "last"}
    assert len(trades(session_factory, AT, positions=True, day=AT.date())["items"]) == 4


def test_colliding_trade_ids_are_kept_separate_by_strategy(session_factory):
    seed(session_factory)
    add(session_factory, "shared", strategy="ACTS_V2", marked=False)
    add(session_factory, "shared", strategy="ORB_15", marked=True)
    rows = trades(session_factory, AT, positions=True)["items"]
    assert len(rows) == 2
    assert {row["strategy_id"]: row["mark_available"] for row in rows} == {"ACTS_V2": False, "ORB_15": True}


def test_evidence_is_allowlisted_and_historical_exit_reasons_unchanged(session_factory):
    seed(session_factory)
    doc = add(session_factory, "historical", state="CLOSED")
    doc.update(exit_reason="TIME_STOP", exit_policy_version=None, opportunity_score=58.2, score_band="TRADEABLE")
    doc["entry_quotes"]["short"]["quote"] = {
        "trading_symbol": "STORED_NIFTY", "bid": 12,
        "ask": 13, "bid_quantity": 100, "ask_quantity": 150, "depth_unit": "UNKNOWN",
        "source_market_timestamp": AT.isoformat(), "secret": "DO_NOT_RETURN"}
    with session_factory.begin() as session:
        session.get(ActsPaperTrade, "historical").document = json.dumps(doc)
    row = trades(session_factory, AT)["items"][0]
    assert row["exit_reason"] == "TIME_STOP" and row["exit_policy_version"] is None
    assert row["opportunity_score"] == 58.2 and row["score_band"] == "TRADEABLE"
    assert row["entry_quotes"]["short"] == {
        "fill_price": 12, "side": "bid", "trading_symbol": "STORED_NIFTY",
        "bid": 12, "ask": 13, "bid_quantity": 100, "ask_quantity": 150, "depth_unit": "UNKNOWN",
        "source_market_timestamp": AT.isoformat()}
    assert row["mirror_diagnostics"][0]["observed_at"] == AT.isoformat()
    assert "DO_NOT_RETURN" not in json.dumps(row)


def test_acts_v1_excluded_even_when_current_cursor_is_v1(session_factory):
    seed(session_factory)
    doc = add(session_factory, "v1")
    doc["strategy_version"] = "ACTS_V1"
    with session_factory.begin() as session:
        session.get(ActsPaperTrade, "v1").document = json.dumps(doc)
    assert trades(session_factory, AT)["items"] == []


def test_missing_closed_exit_time_and_pnl_remain_unknown(session_factory):
    seed(session_factory)
    doc = add(session_factory, "incomplete", state="CLOSED", marked=False)
    doc.update(exit_timestamp=None, final_outcome=None, holding_seconds=None)
    with session_factory.begin() as session:
        session.get(ActsPaperTrade, "incomplete").document = json.dumps(doc)
    row = trades(session_factory, AT)["items"][0]
    assert row["exit_timestamp"] is None and row["holding_seconds"] is None
    assert row["realized_gross_pnl"] is None


def test_endpoints_only_select_and_validate_filters(session_factory, engine, monkeypatch):
    seed(session_factory)
    add(session_factory, "active")
    import app.acts_paper.service as acts_service
    import app.parallel_paper.service as parallel_service

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read endpoint invoked an engine/write")
    monkeypatch.setattr(acts_service.ActsPaperService, "__init__", forbidden)
    monkeypatch.setattr(parallel_service.ParallelPaperService, "__init__", forbidden)
    monkeypatch.setattr("app.broker.kotak.market_data.KotakMarketDataAdapter.__init__", forbidden)
    statements = []

    def capture(_conn, _cursor, statement, *_):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT")
    event.listen(engine, "before_cursor_execute", capture)
    event.listen(session_factory.class_, "before_flush", forbidden)
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    try:
        client = TestClient(app)
        for path in ("positions", "journal"):
            response = client.get(f"/api/phase15/{path}")
            assert response.status_code == 200 and len(response.json()["items"]) == 1
        for query in ("strategy=BAD", "state=BAD", "date=not-a-date", "limit=101", "limit=0", "offset=-1"):
            assert client.get(f"/api/phase15/journal?{query}").status_code == 422
        assert client.get("/api/phase15/positions?limit=101").status_code == 422
        assert statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        event.remove(session_factory.class_, "before_flush", forbidden)
        app.dependency_overrides.clear()
    with session_factory() as session:
        assert json.loads(session.scalar(select(ActsPaperTrade.document)))["gross_mark_rupees"] == 75


def test_management_projection_uses_frozen_policy_and_persisted_exit_request(session_factory):
    seed(session_factory)
    doc = add(session_factory, "managed")
    doc["mirror_exit_request"] = {"reason": "OPPOSITE_ACTS_V2_ENTRY_SIGNAL", "private": "DO_NOT_EXPOSE"}
    doc["policy"]["exit_policy"] = {"forced_exit_time_ist": "15:20:00", "secret": "DO_NOT_EXPOSE"}
    with session_factory.begin() as session:
        session.get(ActsPaperTrade, "managed").document = json.dumps(doc)
    result = trades(session_factory, AT, positions=True)["items"][0]
    assert result["management"] == {"requested_exit_reason": "OPPOSITE_ACTS_V2_ENTRY_SIGNAL",
                                    "forced_exit_time_ist": "15:20:00"}
    assert "DO_NOT_EXPOSE" not in json.dumps(result)
