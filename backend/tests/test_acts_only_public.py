"""The public product isolates ACTS without changing dormant research/history."""

import os
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url

from app.acts_paper import read_models
from app.db.base import Base
from app.db.session import build_session_factory, get_session_factory
from app.main import app
from app.parallel_paper import monitor
from app.parallel_paper.models import ParallelPaperEvent, ParallelPaperTrade
from app.parallel_paper.report import report
from app.parallel_paper.trade_monitor import trades as research_trades
from tests.test_acts_v3_reporting import assert_bounded_history, history, records, add, current
from tests.test_phase15_monitor import AT, seed, parallel_event, parallel_trade, trade_doc

RETIRED = ("ORB_15", "ORB_30", "ORB_60", "EMA_PULLBACK", "SUPERTREND_RSI_VOLUME")


def retired_evidence(sessions):
    for index, (strategy, state) in enumerate(
        zip(RETIRED, ("CLOSED", "OPEN", "UNRESOLVED_EXPOSURE", "CLOSED", "OPEN"))
    ):
        doc = trade_doc(AT, gross=999_999_999, mark=888_888_888, exit=state == "CLOSED")
        if strategy == "EMA_PULLBACK":
            doc.update(
                expiry_terminalization=True,
                exit_pricing_status="UNAVAILABLE",
                exit_reason="CONTRACT_EXPIRED_UNPRICED",
                final_outcome=None,
                exit_timestamp=None,
            )
        parallel_trade(sessions, strategy, strategy, state, doc)
        parallel_event(sessions, strategy, index + 1, "ENTRY_REJECTED", {"reason": "RETIRED_REJECTION"})


def parallel_records(sessions):
    with sessions() as session:
        return (
            [(row.id, row.state, row.document) for row in session.scalars(select(ParallelPaperTrade))],
            [(row.event_key, row.payload, row.event_hash) for row in session.scalars(select(ParallelPaperEvent))],
        )


def test_public_summary_is_exactly_current_and_historical_acts_and_ignores_parallel(session_factory):
    history(session_factory)
    before = read_models.summary(session_factory, AT)
    retired_evidence(session_factory)
    evidence = parallel_records(session_factory)
    acts_evidence = records(session_factory)
    statements = []

    def read_only(_conn, _cursor, statement, *_):
        statements.append(statement)
        assert statement.lstrip().upper().startswith(("SELECT", "SET TRANSACTION READ ONLY"))
        assert "parallel_paper_" not in statement.lower()

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", read_only)
    try:
        actual = read_models.summary(session_factory, AT)
    finally:
        event.remove(engine, "before_cursor_execute", read_only)
    assert actual == before
    assert [row["strategy_id"] for row in actual["strategies"]] == ["ACTS_V3", "ACTS_V2"]
    assert actual["totals"] == {
        "realized_gross_pnl_today": 80,
        "unrealized_gross_pnl": 75,
        "combined_gross_pnl": 155,
        "open_positions": 1,
        "unresolved_exposures": 0,
        "expired_unpriced_count": 0,
        "trades_today": 3,
    }
    assert all(row["entry_rejections_today"] == 0 for row in actual["strategies"])
    assert parallel_records(session_factory) == evidence
    assert records(session_factory) == acts_evidence
    # Full research functions and all historical records still exist.
    full = monitor.summary(session_factory, AT)
    assert len(full["strategies"]) == 7
    assert (
        next(row for row in full["strategies"] if row["strategy_id"] == "ORB_15")["realized_gross_pnl_today"]
        == 999_999_999
    )
    assert len(research_trades(session_factory, AT)["items"]) == 8
    assert report(session_factory, AT)["strategies"]["ORB_15"]["gross_pnl"] == 999_999_999


def test_public_acts_zero_na_and_v2_history_remain_available(session_factory):
    seed(session_factory)
    add(session_factory, "historical-v2", "ACTS_V2", gross=120)
    current(session_factory, "ACTS_V2")
    result = read_models.summary(session_factory, AT)
    assert [row["strategy_id"] for row in result["strategies"]] == ["ACTS_V3", "ACTS_V2"]
    v3, v2 = result["strategies"]
    assert v3["trades_today"] == v3["open_position_count"] == v3["unresolved_exposure_count"] == 0
    assert v3["realized_gross_pnl_today"] == v3["unrealized_gross_pnl"] == 0
    assert v3["latest_status"] is v3["latest_timestamp"] is v3["direction"] is None
    assert all(value is None for value in v3["decision_features"].values())
    assert v2["realized_gross_pnl_today"] == result["totals"]["realized_gross_pnl_today"] == 120


def test_public_endpoints_never_query_or_return_parallel_and_reject_retired_filters(session_factory, monkeypatch):
    history(session_factory)
    retired_evidence(session_factory)
    before = parallel_records(session_factory), records(session_factory)
    from app.api import phase15

    class Clock:
        @staticmethod
        def now(_tz):
            return AT

    monkeypatch.setattr(phase15, "datetime", Clock)
    app.dependency_overrides[get_session_factory] = lambda: session_factory

    def read_only(_conn, _cursor, statement, *_):
        assert statement.lstrip().upper().startswith(("SELECT", "SET TRANSACTION READ ONLY"))
        assert "parallel_paper_" not in statement.lower()

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", read_only)
    try:
        with TestClient(app) as client:
            result = client.get("/api/phase15/summary")
            assert result.status_code == 200
            assert [row["strategy_id"] for row in result.json()["strategies"]] == ["ACTS_V3", "ACTS_V2"]
            positions = client.get("/api/phase15/positions").json()["items"]
            assert [row["trade_id"] for row in positions] == ["v3-open"]
            page = client.get("/api/phase15/journal").json()["items"]
            assert {row["trade_id"] for row in page} == {"v2-closed", "v3-closed", "v3-open"}
            assert client.get("/api/phase15/journal?strategy=ACTS_V2").json()["items"][0]["trade_id"] == "v2-closed"
            assert len(client.get("/api/phase15/journal?strategy=ACTS_V3").json()["items"]) == 2
            for retired in RETIRED:
                assert client.get(f"/api/phase15/journal?strategy={retired}").status_code == 422
            assert client.get("/api/phase15/journal?limit=1").json()["has_more"]
    finally:
        event.remove(engine, "before_cursor_execute", read_only)
        app.dependency_overrides.clear()
    assert (parallel_records(session_factory), records(session_factory)) == before


def test_public_projection_never_reads_malformed_dormant_history(session_factory):
    history(session_factory)
    retired_evidence(session_factory)
    with session_factory.begin() as session:
        session.get(ParallelPaperTrade, "ORB_15").document = "not-json-dormant"
    assert read_models.summary(session_factory, AT)["totals"]["combined_gross_pnl"] == 155
    assert len(read_models.trades(session_factory, AT)["items"]) == 3
    with pytest.raises(ValueError, match="INVALID_PHASE15_TRADE_FILTER"):
        read_models.trades(session_factory, AT, strategy="ORB_15")


def test_public_94k_event_history_stays_bounded(session_factory, monkeypatch):
    monkeypatch.setattr(monitor, "summary", read_models.summary)
    assert_bounded_history(session_factory, monkeypatch)


def test_postgresql_public_scope_and_history_bound(monkeypatch):
    value = os.getenv("PHASE15_POSTGRES_TEST_URL")
    if not value:
        pytest.skip("PHASE15_POSTGRES_TEST_URL must point to isolated local PostgreSQL 16")
    base = make_url(value)
    admin = create_engine(base)
    try:
        for case in (
            test_public_summary_is_exactly_current_and_historical_acts_and_ignores_parallel,
            test_public_acts_zero_na_and_v2_history_remain_available,
            lambda sessions: test_public_endpoints_never_query_or_return_parallel_and_reject_retired_filters(
                sessions, monkeypatch
            ),
            lambda sessions: test_public_94k_event_history_stays_bounded(sessions, monkeypatch),
        ):
            schema = f"acts_only_{uuid4().hex}"
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
