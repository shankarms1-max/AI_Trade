"""Production-baseline V3 read projections; no engines, workers or broker calls."""

from copy import deepcopy
from datetime import timedelta
import json
import os
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.db.base import Base
from app.db.session import build_session_factory, get_session_factory
from app.main import app
from app.parallel_paper import monitor
from app.parallel_paper.report import report
from app.parallel_paper.trade_monitor import trades
from app.scalper.models import ScalperMarketSnapshotRecord
from tests.test_phase15_monitor import AT, seed, trade_doc


def latest(version, at=AT):
    return {
        "strategy_version": version,
        "timestamp": at.isoformat(),
        "status": "NO_SETUP",
        "primary_blocker": "CONSUMED_CYCLE" if version == "ACTS_V3" else "VOLUME_CONFIRMATION_REQUIRED",
        "direction": "BULL" if version == "ACTS_V3" else "BEAR",
        "opportunity_score": 86.2 if version == "ACTS_V3" else 58.6,
        "score_band": "VERY_STRONG" if version == "ACTS_V3" else "TRADEABLE",
        "alpha_state": "NEUTRAL",
    }


def current(sessions, version="ACTS_V3"):
    with sessions.begin() as session:
        cursor = session.get(ActsPaperCursor, 1)
        cursor.latest_json = json.dumps(latest(version))
        cursor.observed_at = AT.isoformat()


def journal(sessions, kind, payload, snapshot_id=2, trade_id=None):
    with sessions.begin() as session:
        sequence = (session.scalar(select(func.max(ActsPaperEvent.sequence))) or 0) + 1
        session.add(
            ActsPaperEvent(
                sequence=sequence,
                event_key=f"fixture:{sequence}",
                event_type=kind,
                snapshot_id=snapshot_id,
                trade_id=trade_id,
                payload=json.dumps(payload),
                previous_hash="a" * 64,
                event_hash="b" * 64,
            )
        )


def add(sessions, ident, version, state="CLOSED", gross=120, mark=75):
    doc = trade_doc(
        AT,
        gross=gross,
        mark=mark,
        exit=state == "CLOSED",
        entry=state not in {"PENDING_PAPER_ENTRY", "ENTRY_REJECTED"},
    )
    doc.update(
        strategy_version=version,
        lot_size=50,
        lots=1,
        entry_credit=42,
        current_debit=40.5,
        policy_hash="frozen-policy",
        exit_policy_version="MIRROR_EXIT_V1",
        policy={"config": {"allowed_widths": [400]}, "exit_policy": {"forced_exit_time_ist": "15:20:00"}},
        mirror_exit_request={"reason": "OPPOSITE_ACTS_V2_ENTRY_SIGNAL"},
        entry_quotes={"short": {"fill_price": 100, "side": "bid"}, "long": {"fill_price": 58, "side": "ask"}},
    )
    doc["candidate"]["spread_width"] = 400
    doc["candidate"]["short_leg"].update(option_type="PE", strike=24900)
    doc["candidate"]["long_leg"].update(option_type="PE", strike=24500)
    if version == "ACTS_V3":
        doc["opportunity_cycle"] = {
            "cycle_id": "frozen-cycle",
            "cycle_state": "ENTERED",
            "rearm_reason": "EXCEPTIONAL_REVERSAL_CONFIRMATION",
            "previous_direction": "BEAR",
            "previous_exit_reason": "SPREAD_STOP",
            "previous_exit_timestamp": (AT - timedelta(minutes=6)).isoformat(),
            "reversal_confirmation_seconds": 60,
            "opportunity_cycle_policy_version": "ACTS_V3_OPPORTUNITY_CYCLE_V1",
            "private": "DO_NOT_RETURN",
        }
    with sessions.begin() as session:
        snapshot = ScalperMarketSnapshotRecord(
            id=(session.scalar(select(func.max(ScalperMarketSnapshotRecord.id))) or 0) + 1,
            capture_key=ident,
            captured_at=AT,
            request_started_at=AT,
            response_received_at=AT,
            nifty_spot=25000,
            nifty_future=25010,
            lot_size=50,
            atm_strike=25000,
            expiry=AT.date(),
            source="TEST",
            feature_json="{}",
            signal_json="{}",
        )
        session.add(snapshot)
        session.flush()
        doc["last_snapshot_id"] = snapshot.id
        session.add(ActsPaperTrade(id=ident, state=state, document=json.dumps(doc), decision_snapshot_id=snapshot.id))
    if state == "OPEN":
        journal(
            sessions,
            "ACTS_MARK",
            {
                "strategy_version": version,
                "available": True,
                "quotes": {
                    "short": {"fill_price": 100.2, "side": "ask", "quote": {"ask": 100.2}},
                    "long": {"fill_price": 59.7, "side": "bid", "quote": {"bid": 59.7}},
                },
            },
            snapshot_id=doc["last_snapshot_id"],
            trade_id=ident,
        )
    return doc


def history(sessions):
    seed(sessions)
    add(sessions, "v2-closed", "ACTS_V2", gross=120)
    add(sessions, "v3-closed", "ACTS_V3", gross=-40)
    doc = add(sessions, "v3-open", "ACTS_V3", state="OPEN")
    journal(
        sessions,
        "ACTS_OBSERVATION",
        {"strategy_version": "ACTS_V2", "latest": latest("ACTS_V2"), "market_timestamp": AT.isoformat()},
    )
    current(sessions)
    return doc


def records(sessions):
    with sessions() as session:
        return (
            [(row.id, row.state, row.document) for row in session.scalars(select(ActsPaperTrade))],
            [
                (row.sequence, row.payload, row.previous_hash, row.event_hash)
                for row in session.scalars(select(ActsPaperEvent))
            ],
        )


def test_v2_only_database_still_reports_unobserved_v3_as_zero_and_na(session_factory):
    from tests.phase15_summary_legacy import summary as legacy_summary

    seed(session_factory)
    add(session_factory, "historical-v2", "ACTS_V2", gross=120)
    add(session_factory, "managed-v2", "ACTS_V2", state="OPEN", mark=75)
    current(session_factory, "ACTS_V2")
    journal(
        session_factory,
        "ACTS_OBSERVATION",
        {
            "strategy_version": "ACTS_V2",
            "latest": latest("ACTS_V2"),
            "market_timestamp": AT.isoformat(),
        },
    )
    before = records(session_factory)
    assert all(json.loads(row[2])["strategy_version"] == "ACTS_V2" for row in before[0])
    assert all(json.loads(row[1])["strategy_version"] == "ACTS_V2" for row in before[1])
    result = monitor.summary(session_factory, AT)
    assert len(result["strategies"]) == 7
    by_version = {row["strategy_id"]: row for row in result["strategies"]}
    v3 = by_version["ACTS_V3"]
    assert v3["display_name"] == "ACTS V3"
    for key in (
        "trades_today",
        "open_position_count",
        "unresolved_exposure_count",
        "realized_gross_pnl_today",
        "unrealized_gross_pnl",
        "entry_rejections_today",
        "wins_today",
        "losses_today",
    ):
        assert v3[key] == 0, key
    for key in ("latest_status", "latest_timestamp", "direction", "latest_blocker", "orb_range"):
        assert v3[key] is None, key
    assert all(value is None for value in v3["decision_features"].values())
    assert v3["today_trades"] == []
    assert by_version["ACTS_V2"]["decision_features"]["opportunity_score"] == 58.6
    assert result["totals"]["combined_gross_pnl"] == 195
    # Exact baseline rows, contracts and totals are retained; only the V3 row is added.
    assert {
        **result,
        "strategies": [row for row in result["strategies"] if row["strategy_id"] != "ACTS_V3"],
    } == legacy_summary(session_factory, AT)
    performance = report(session_factory, AT)["strategies"]
    assert set(monitor.ACTS_VERSIONS).issubset(performance)
    assert performance["ACTS_V2"]["gross_pnl"] == 120
    assert performance["ACTS_V3"]["gross_pnl"] == performance["ACTS_V3"]["unrealized_pnl"] == 0
    assert performance["ACTS_V3"]["trades"] == performance["ACTS_V3"]["open_position_count"] == 0
    assert performance["ACTS_V3"]["latest"] == {}
    assert records(session_factory) == before


def test_unobserved_v3_exists_even_without_an_acts_cursor(session_factory):
    result = monitor.summary(session_factory, AT)
    assert len(result["strategies"]) == 7
    v3 = next(row for row in result["strategies"] if row["strategy_id"] == "ACTS_V3")
    assert v3["trades_today"] == v3["realized_gross_pnl_today"] == v3["unrealized_gross_pnl"] == 0
    assert v3["latest_status"] is v3["latest_timestamp"] is v3["direction"] is None
    assert set(monitor.ACTS_VERSIONS).issubset(report(session_factory, AT)["strategies"])


def test_summary_splits_versions_counts_pnl_signals_and_rejections(session_factory):
    history(session_factory)
    for version in monitor.ACTS_VERSIONS:
        journal(session_factory, "ACTS_ENTRY_REJECTED", {"strategy_version": version, "reason": f"{version}_REJECT"})
    journal(session_factory, "ACTS_ENTRY_REJECTED", {"strategy_version": "ACTS_V1", "reason": "IGNORE"})
    before = records(session_factory)
    result = monitor.summary(session_factory, AT)
    versions = {row["strategy_id"]: row for row in result["strategies"]}
    assert len(versions) == 7
    assert versions["ACTS_V2"]["trades_today"] == 1
    assert versions["ACTS_V3"]["trades_today"] == 2
    assert versions["ACTS_V2"]["realized_gross_pnl_today"] == 120
    assert versions["ACTS_V3"]["realized_gross_pnl_today"] == -40
    assert versions["ACTS_V2"]["unrealized_gross_pnl"] == 0
    assert versions["ACTS_V3"]["unrealized_gross_pnl"] == 75
    for version, score in (("ACTS_V2", 58.6), ("ACTS_V3", 86.2)):
        assert versions[version]["decision_features"]["opportunity_score"] == score
        assert versions[version]["entry_rejections_today"] == 1
        assert versions[version]["latest_rejection_reason"] == f"{version}_REJECT"
    assert result["totals"]["realized_gross_pnl_today"] == 80
    assert result["totals"]["combined_gross_pnl"] == 155
    assert result["totals"]["trades_today"] == 3
    assert versions["ACTS_V3"]["today_trades"][0]["cycle_id"] == "frozen-cycle"
    assert "DO_NOT_RETURN" not in json.dumps(result)
    assert records(session_factory) == before


def test_performance_preserves_v2_metrics_and_adds_independent_v3(session_factory):
    seed(session_factory)
    add(session_factory, "v2", "ACTS_V2")
    baseline = report(session_factory, AT)["strategies"]["ACTS_V2"]
    journal(
        session_factory,
        "ACTS_OBSERVATION",
        {"strategy_version": "ACTS_V2", "latest": latest("ACTS_V2"), "market_timestamp": AT.isoformat()},
    )
    add(session_factory, "v3", "ACTS_V3", gross=-80)
    current(session_factory)
    result = report(session_factory, AT)["strategies"]
    for key in baseline.keys() - {"latest", "journal_head"}:
        assert result["ACTS_V2"][key] == baseline[key], key
    assert result["ACTS_V2"]["latest"]["strategy_version"] == "ACTS_V2"
    assert result["ACTS_V3"]["latest"]["strategy_version"] == "ACTS_V3"
    assert result["ACTS_V3"]["gross_pnl"] == -80 and result["ACTS_V3"]["closed_trades"] == 1


@pytest.mark.parametrize("state", ["PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE"])
def test_v3_positions_and_journal_preserve_production_projection(session_factory, state):
    seed(session_factory)
    doc = add(session_factory, "v3", "ACTS_V3", state=state)
    add(session_factory, "v2", "ACTS_V2")
    add(session_factory, "v1", "ACTS_V1")
    result = trades(session_factory, AT, positions=True)["items"]
    assert len(result) == 1 and result[0]["strategy_id"] == "ACTS_V3"
    row = result[0]
    assert row["short_option_type"] == row["hedge_option_type"] == "PE"
    assert row["spread_width"] == 400 and row["policy_allowed_widths"] == [400]
    assert row["management"]["forced_exit_time_ist"] == "15:20:00"
    assert row["management"]["requested_exit_reason"] == "OPPOSITE_ACTS_V2_ENTRY_SIGNAL"
    assert row["entry_quotes"]["short"]["fill_price"] == 100
    assert row["strategy_features"] is None  # Never fabricate an ORB range for ACTS.
    assert row["cycle_id"] == doc["opportunity_cycle"]["cycle_id"]
    assert row["reversal_confirmation_seconds"] == 60
    assert row["mark_available"] == (state == "OPEN")
    if state == "OPEN":
        assert row["latest_mark_quotes"]["short"]["ask"] == 100.2
        assert row["latest_mark_quotes"]["long"]["bid"] == 59.7
    page = trades(session_factory, AT, strategy="ACTS_V3", day=AT.date())
    assert [item["trade_id"] for item in page["items"]] == ["v3"]
    assert {item["strategy_id"] for item in trades(session_factory, AT)["items"]} == set(monitor.ACTS_VERSIONS)


@pytest.mark.parametrize("version", ["ACTS_V2", "ACTS_V3"])
def test_conflicting_mark_version_cannot_mask_or_relabel_correct_mark(session_factory, version):
    seed(session_factory)
    doc = add(session_factory, "held", version, state="OPEN")
    other = "ACTS_V2" if version == "ACTS_V3" else "ACTS_V3"
    journal(
        session_factory,
        "ACTS_MARK",
        {"strategy_version": other, "available": False},
        snapshot_id=doc["last_snapshot_id"],
        trade_id="held",
    )
    current(session_factory, version)
    row = trades(session_factory, AT, positions=True)["items"][0]
    assert row["strategy_id"] == version and row["mark_available"]
    assert row["current_unrealized_gross_pnl"] == 75
    projected = {item["strategy_id"]: item for item in monitor.summary(session_factory, AT)["strategies"]}
    assert projected[version]["unrealized_gross_pnl"] == 75


def test_v3_unknown_mark_version_is_unavailable_not_a_fabricated_quote(session_factory):
    seed(session_factory)
    doc = add(session_factory, "held", "ACTS_V3", state="OPEN")
    journal(session_factory, "ACTS_MARK", {"available": True}, snapshot_id=doc["last_snapshot_id"], trade_id="held")
    row = trades(session_factory, AT, positions=True)["items"][0]
    assert row["current_debit"] is row["current_unrealized_gross_pnl"] is None
    assert row["latest_mark_quotes"] == {} and row["mark_unavailable_reason"] == "ACTS_MARK_VERSION_UNAVAILABLE"


@pytest.mark.parametrize("payload", ["{bad", "[]", '"bad"'])
def test_malformed_selected_events_fail_closed_without_hiding_parallel(session_factory, payload):
    history(session_factory)
    with session_factory.begin() as session:
        sequence = (session.scalar(select(func.max(ActsPaperEvent.sequence))) or 0) + 1
        session.add(
            ActsPaperEvent(
                sequence=sequence,
                event_key="invalid",
                event_type="ACTS_ENTRY_REJECTED",
                snapshot_id=2,
                payload=payload,
                previous_hash="a" * 64,
                event_hash="b" * 64,
            )
        )
    rows = {row["strategy_id"]: row for row in monitor.summary(session_factory, AT)["strategies"]}
    assert rows["ACTS_V2"]["trades_today"] is rows["ACTS_V3"]["trades_today"] is None
    assert rows["ORB_15"]["trades_today"] == rows["EMA_PULLBACK"]["trades_today"] == 0


def test_malformed_trade_does_not_escape_existing_per_strategy_failure_handler(session_factory):
    history(session_factory)
    with session_factory.begin() as session:
        session.get(ActsPaperTrade, "v3-open").document = "{bad"
    rows = {row["strategy_id"]: row for row in monitor.summary(session_factory, AT)["strategies"]}
    assert rows["ACTS_V2"]["trades_today"] is rows["ACTS_V3"]["trades_today"] is None
    assert rows["ORB_30"]["trades_today"] == 0


def test_current_production_apis_are_read_only_and_v3_filters_work(session_factory, monkeypatch):
    history(session_factory)
    from app.api import phase15

    class Clock:
        @staticmethod
        def now(_tz):
            return AT

    monkeypatch.setattr(phase15, "datetime", Clock)
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    before = records(session_factory)

    def read_only(_conn, _cursor, statement, *_args):
        assert (
            statement.lstrip()
            .upper()
            .startswith(("SELECT", "SET TRANSACTION READ ONLY", "PRAGMA MAIN.TABLE_INFO(", "PRAGMA TEMP.TABLE_INFO("))
        ), statement

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", read_only)
    try:
        with TestClient(app) as client:
            for path in (
                "/api/health",
                "/api/dashboard/latest",
                "/api/phase15/summary",
                "/api/phase15/positions",
                "/api/phase15/journal?strategy=ACTS_V3",
            ):
                response = client.get(path)
                assert response.status_code == 200, (path, response.text)
            assert client.get("/api/phase15/positions").json()["items"][0]["strategy_id"] == "ACTS_V3"
            page = client.get("/api/phase15/journal?strategy=ACTS_V3").json()
            assert {row["trade_id"] for row in page["items"]} == {"v3-open", "v3-closed"}
            assert client.get("/api/phase15/journal?strategy=ACTS_V1").status_code == 422
    finally:
        event.remove(engine, "before_cursor_execute", read_only)
        app.dependency_overrides.clear()
    assert records(session_factory) == before


def assert_bounded_history(sessions, monkeypatch):
    seed(sessions)
    add(sessions, "old-v2", "ACTS_V2")
    current(sessions)
    total = 94_000
    with sessions.begin() as session:
        # Unrelated malformed history must never be parsed. The huge watch
        # must never be transferred to Python, even inside the bounded tail.
        session.execute(
            ActsPaperEvent.__table__.insert(),
            [
                {
                    "sequence": index,
                    "event_key": f"volume:{index}",
                    "snapshot_id": 2,
                    "event_type": "ACTS_OBSERVATION" if index in (1, total) else "ACTS_NO_SETUP",
                    "trade_id": None,
                    "previous_hash": "a" * 64,
                    "event_hash": "b" * 64,
                    "payload": (
                        json.dumps(
                            {
                                "strategy_version": "ACTS_V2" if index == 1 else "ACTS_V3",
                                "market_timestamp": AT.isoformat(),
                                "latest": latest("ACTS_V2" if index == 1 else "ACTS_V3"),
                                "watch": {"huge": "DO_NOT_LOAD" * 100_000},
                            }
                        )
                        if index in (1, total)
                        else "not-json-unrelated"
                    ),
                }
                for index in range(1, total + 1)
            ],
        )
    original = json.loads

    def small_json(value, *args, **kwargs):
        assert not isinstance(value, str) or "DO_NOT_LOAD" not in value, "whole journal body transferred"
        return original(value, *args, **kwargs)

    monkeypatch.setattr(monitor.json, "loads", small_json)
    sql = []

    def capture(_conn, _cursor, statement, parameters, *_args):
        sql.append((statement, deepcopy(parameters)))

    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        for _ in range(2):
            result = monitor.summary(sessions, AT)
            rows = {row["strategy_id"]: row for row in result["strategies"]}
            assert rows["ACTS_V2"]["trades_today"] == 1
            assert rows["ACTS_V2"]["realized_gross_pnl_today"] == 120
            assert rows["ACTS_V2"]["latest_timestamp"] is None  # Outside bounded signal tail.
            assert rows["ACTS_V2"]["decision_features"]["opportunity_score"] is None
            assert rows["ACTS_V3"]["decision_features"]["opportunity_score"] == 86.2
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    tails = [(statement, params) for statement, params in sql if "max(acts_paper_events.sequence)" in statement]
    assert len(tails) == 2
    assert all("acts_paper_events.sequence >" in statement and "LIMIT" in statement for statement, _ in tails)
    assert all(
        monitor.ACTS_HISTORY_EVENT_LIMIT in (params.values() if isinstance(params, dict) else params)
        for _, params in tails
    )
    assert len(sql) <= 18  # Two polls; no N+1 query per trade/event.
    if engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            plan = connection.exec_driver_sql("EXPLAIN (ANALYZE, FORMAT JSON) " + tails[0][0], tails[0][1]).scalar()
        assert "acts_paper_events_pkey" in json.dumps(plan)

        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)

        scans = [node for node in nodes(plan[0]["Plan"]) if node.get("Relation Name") == "acts_paper_events"]
        assert scans and all("Index" in node["Node Type"] for node in scans)
        assert all(
            node["Actual Rows"] + node.get("Rows Removed by Filter", 0) <= monitor.ACTS_HISTORY_EVENT_LIMIT
            for node in scans
        )


def test_94k_events_do_not_cause_full_journal_replay_or_history_loss(session_factory, monkeypatch):
    assert_bounded_history(session_factory, monkeypatch)


def test_postgresql_versioned_reporting_and_94k_event_bound(monkeypatch):
    value = os.getenv("PHASE15_POSTGRES_TEST_URL")
    if not value:
        pytest.skip("PHASE15_POSTGRES_TEST_URL must point to disposable PostgreSQL 16")
    base = make_url(value)
    admin = create_engine(base)
    try:
        for case in (
            test_v2_only_database_still_reports_unobserved_v3_as_zero_and_na,
            test_summary_splits_versions_counts_pnl_signals_and_rejections,
            lambda sessions: test_v3_positions_and_journal_preserve_production_projection(sessions, "OPEN"),
            lambda sessions: test_malformed_selected_events_fail_closed_without_hiding_parallel(sessions, "{bad"),
            lambda sessions: test_malformed_selected_events_fail_closed_without_hiding_parallel(sessions, "[]"),
            lambda sessions: test_malformed_selected_events_fail_closed_without_hiding_parallel(sessions, '"bad"'),
            lambda sessions: assert_bounded_history(sessions, monkeypatch),
        ):
            schema = f"v3_reporting_{uuid4().hex}"
            target = create_engine(base.update_query_dict({"options": f"-csearch_path={schema}"}))
            try:
                with admin.begin() as connection:
                    assert connection.scalar(text("SHOW server_version")).startswith("16.")
                    connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                Base.metadata.create_all(target)
                case(build_session_factory(target))
            finally:
                target.dispose()
                with admin.begin() as connection:
                    connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        admin.dispose()
