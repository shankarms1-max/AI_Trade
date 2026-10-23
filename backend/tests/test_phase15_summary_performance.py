"""Focused read-projection parity and bounded event-decoding regression coverage."""
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import os
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.db.base import Base
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperEvent, ParallelPaperTrade
from app.parallel_paper.monitor import summary
from app.scalper.models import ScalperMarketSnapshotRecord as Snapshot
from app.scalper.repository import IST
from tests.phase15_summary_legacy import summary as legacy_summary
from tests.test_phase15_monitor import (
    AT, acts_event, acts_trade, parallel_event, parallel_trade, seed, trade_doc,
)


@pytest.fixture(name="session_factory")
def projection_sessions(engine):
    """Optional real PostgreSQL parity, restricted to an isolated loopback test DB."""
    value = os.getenv("PHASE15_SUMMARY_TEST_URL")
    if not value:
        yield sessionmaker(bind=engine, expire_on_commit=False)
        return
    url = make_url(value)
    if (url.get_backend_name() != "postgresql" or url.host != "127.0.0.1"
            or url.database != "phase15_summary_validation" or url.username != "phase15_validation"):
        raise ValueError("DISPOSABLE_LOOPBACK_DATABASE_REQUIRED")
    pg = create_engine(url)
    schema = "summary_contract_" + uuid4().hex
    scoped = pg.execution_options(schema_translate_map={None: schema})
    tables = [Base.metadata.tables[model.__tablename__] for model in (
        Snapshot, ActsPaperCursor, ActsPaperTrade, ActsPaperEvent,
        ParallelPaperCursor, ParallelPaperTrade, ParallelPaperEvent)]
    with pg.begin() as connection:
        connection.execute(CreateSchema(schema))
    try:
        Base.metadata.create_all(scoped, tables=tables)
        yield sessionmaker(bind=scoped, expire_on_commit=False)
    finally:
        with pg.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        pg.dispose()


def same(sessions, at=AT):
    expected = legacy_summary(sessions, at)
    assert summary(sessions, at) == expected
    return expected


@pytest.mark.parametrize("acts", [False, True])
@pytest.mark.parametrize("availability", [True, False, None, 0, "false"])
def test_latest_mark_and_version_filter_parity(session_factory, acts, availability):
    seed(session_factory)
    if acts:
        acts_trade(session_factory, "open", "OPEN", trade_doc(mark=55))
        for seq, available, version in [(1, False, "ACTS_V2"), (2, availability, "ACTS_V2"),
                                        (3, False, "ACTS_V1")]:
            acts_event(session_factory, seq, "ACTS_MARK", {"available": available,
                       "strategy_version": version}, trade_id="open")
    else:
        parallel_trade(session_factory, "ORB_15", "open", "OPEN", trade_doc(mark=55))
        for seq, available in [(1, False), (2, availability)]:
            parallel_event(session_factory, "ORB_15", seq, "MARK", {"available": available}, trade_id="open")
    result = same(session_factory)
    assert result["strategies"][0 if acts else 1]["unrealized_gross_pnl"] == (
        None if availability is False else 55)


def test_rejection_order_null_ids_orb_signals_and_escaped_json_parity(session_factory):
    seed(session_factory)
    parallel_trade(session_factory, "ORB_15", "reject", "ENTRY_REJECTED", trade_doc(entry=False))
    for seq, (direction, breakout) in enumerate([
            ("BULL", AT.isoformat()), ("BULL", AT.isoformat()), ("BEAR", AT.isoformat()),
            ("BULL", None), ("BULL", None)], 1):
        parallel_event(session_factory, "ORB_15", seq, "OBSERVATION", {"decision": {
            "status": "ENTRY_REJECTED", "direction": direction,
            "features": {"breakout_timestamp": breakout}, "blockers": [f"OBS_{seq}"]}})
    for seq, trade_id in [(6, "reject"), (7, "reject"), (8, None), (9, None)]:
        parallel_event(session_factory, "ORB_15", seq, "ENTRY_REJECTED", {"reason": f"FILL_{seq}"},
                       trade_id=trade_id)
    # Filtering must understand JSON, not grep canonical strings.
    with session_factory.begin() as session:
        session.add(ParallelPaperEvent(strategy_id="ORB_15", sequence=10, event_key="escaped",
                    event_type="OBSERVATION", snapshot_id=1, previous_hash="a", event_hash="b",
                    payload=r'{"decision":{"status":"ENTRY_\u0052EJECTED","blockers":["ESCAPED"]}}'))
    result = same(session_factory)["strategies"][1]
    assert result["entry_rejections_today"] == 8
    assert result["latest_rejection_reason"] == "ESCAPED"


def test_lifetime_expiry_cross_day_closes_and_acts_versions(session_factory):
    seed(session_factory)
    yesterday = AT - timedelta(days=1)
    for strategy, state, doc in [
        ("ORB_15", "OPEN", trade_doc(yesterday, mark=23)),
        ("ORB_30", "UNRESOLVED_EXPOSURE", trade_doc(yesterday, mark=999)),
        ("ORB_60", "CLOSED", {**trade_doc(yesterday), "expiry_terminalization": True,
                              "exit_pricing_status": "UNAVAILABLE"}),
        ("EMA_PULLBACK", "CLOSED", {**trade_doc(yesterday, exit=True), "exit_timestamp": AT.isoformat()}),
    ]:
        parallel_trade(session_factory, strategy, strategy, state, doc)
    acts_trade(session_factory, "old-v1", "OPEN", {**trade_doc(mark=1000), "strategy_version": "ACTS_V1"})
    result = same(session_factory)
    assert result["totals"]["open_positions"] == 1
    assert result["totals"]["unresolved_exposures"] == 1
    assert result["totals"]["expired_unpriced_count"] == 1
    assert result["totals"]["realized_gross_pnl_today"] == 120
    assert result["totals"]["trades_today"] == 0


@pytest.mark.parametrize("status", ["ENTRY_REJECTED", "WATCH"])
def test_prefilter_never_treats_text_as_a_decision_or_loses_json_escapes(session_factory, status):
    seed(session_factory)
    payload = json.dumps({"decision": {"status": status, "blockers": ["ACTUAL_REASON"]},
                          "unrelated": "ENTRY_REJECTED"})
    # Escape every character of both the key and value. No literal status need exist.
    payload = payload.replace('"decision"', '"' + ''.join(f'\\u{ord(c):04x}' for c in "decision") + '"')
    payload = payload.replace('"' + status + '"', '"' + ''.join(f'\\u{ord(c):04x}' for c in status) + '"')
    with session_factory.begin() as session:
        session.add(ParallelPaperEvent(strategy_id="ORB_15", sequence=1, event_key="escaped-all",
                    event_type="OBSERVATION", snapshot_id=1, previous_hash="a", event_hash="b", payload=payload))
    assert same(session_factory)["strategies"][1]["entry_rejections_today"] == int(status == "ENTRY_REJECTED")


@pytest.mark.parametrize("delta,included", [(-1, False), (0, True), (86399999999, True), (86400000000, False)])
def test_ist_day_boundaries_use_capture_not_payload_time(session_factory, delta, included):
    seed(session_factory)
    start = datetime.combine(AT.date(), time.min, IST)
    capture = (start + timedelta(microseconds=delta)).astimezone(timezone.utc)
    # SQLite does not preserve timestamptz; PostgreSQL exercises real UTC binds.
    if session_factory.kw["bind"].dialect.name == "sqlite":
        capture = capture.astimezone(IST)
    with session_factory.begin() as session:
        session.get(Snapshot, 1).captured_at = capture
    parallel_event(session_factory, "ORB_15", 1, "ENTRY_REJECTED", {
        "timestamp": (AT - timedelta(days=30)).isoformat(), "reason": "BOUNDARY"})
    assert same(session_factory)["strategies"][1]["entry_rejections_today"] == int(included)


@pytest.mark.parametrize("acts", [False, True])
def test_selected_invalid_json_keeps_per_strategy_failure_isolation(session_factory, acts):
    seed(session_factory)
    with session_factory.begin() as session:
        model = ActsPaperEvent if acts else ParallelPaperEvent
        values = dict(sequence=1, event_key="invalid", event_type="ACTS_ENTRY_REJECTED" if acts else "OBSERVATION",
                      snapshot_id=1, previous_hash="a", event_hash="b",
                      payload='{"decision":{"status":"ENTRY_REJECTED"},broken')
        if not acts:
            values["strategy_id"] = "ORB_15"
        session.add(model(**values))
    assert same(session_factory)["strategies"][0 if acts else 1]["trades_today"] is None


def test_projection_never_hydrates_orm_writes_or_evaluates_an_engine(session_factory, monkeypatch):
    from app.acts_paper.service import ActsPaperService
    from app.parallel_paper.service import ParallelPaperService

    seed(session_factory)
    acts_trade(session_factory, "acts", "OPEN", trade_doc(mark=10))
    tables = [ActsPaperCursor, ActsPaperTrade, ActsPaperEvent, ParallelPaperCursor,
              ParallelPaperTrade, ParallelPaperEvent, Snapshot]

    def fingerprint():
        with session_factory() as session:
            return hashlib.sha256(json.dumps(
                [list(session.execute(select(t.__table__)).all()) for t in tables], default=str).encode()).hexdigest()

    before = fingerprint()
    sql = []

    def forbidden(*args, **kwargs):
        raise AssertionError("READ_MODEL_MUST_NOT_EVALUATE_OR_WRITE")

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement.lstrip().split()[0].upper())

    for cls in [ActsPaperService, ParallelPaperService]:
        monkeypatch.setattr(cls, "__init__", forbidden)
        monkeypatch.setattr(cls, "process_snapshot", forbidden)
    event.listen(session_factory.class_, "loaded_as_persistent", forbidden)
    event.listen(session_factory.class_, "before_flush", forbidden)
    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        summary(session_factory, AT)
    finally:
        event.remove(session_factory.class_, "loaded_as_persistent", forbidden)
        event.remove(session_factory.class_, "before_flush", forbidden)
        event.remove(engine, "before_cursor_execute", record_sql)
    assert set(sql) <= {"SELECT", "SAVEPOINT", "RELEASE", "SET"}
    assert before == fingerprint()


def test_moderate_event_history_is_pruned_before_python_decoding(session_factory, monkeypatch):
    seed(session_factory)
    parallel_trade(session_factory, "ORB_15", "active", "OPEN", trade_doc(mark=55))
    acts_trade(session_factory, "acts-active", "OPEN", trade_doc(mark=21))
    rows = []
    for sequence in range(1, 1201):
        kind = "OBSERVATION" if sequence <= 800 else "MARK"
        payload = ({"decision": {"status": "WATCH"}, "state": {"history": [1] * 100}}
                   if kind == "OBSERVATION" else {"available": sequence != 1200})
        rows.append(dict(strategy_id="ORB_15", sequence=sequence, event_key=f"moderate-{sequence}",
                         event_type=kind, snapshot_id=1, trade_id=None if kind == "OBSERVATION" else "active",
                         previous_hash="a", event_hash="b", payload=json.dumps(payload)))
    with session_factory.begin() as session:
        session.execute(insert(ParallelPaperEvent), rows)
        session.execute(insert(ActsPaperEvent), [dict(
            sequence=sequence, event_key=f"moderate-acts-{sequence}", event_type="ACTS_MARK",
            snapshot_id=2, trade_id="acts-active", previous_hash="a", event_hash="b",
            payload=json.dumps({"available": False if sequence == 299 else True,
                                "strategy_version": "ACTS_V1" if sequence == 300 else "ACTS_V2"}))
            for sequence in range(1, 301)])
    expected = legacy_summary(session_factory, AT)
    original_loads = json.loads
    decoded = []

    def counted_loads(value, *args, **kwargs):
        decoded.append(value)
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", counted_loads)
    started = perf_counter()
    actual = summary(session_factory, AT)
    elapsed_ms = (perf_counter() - started) * 1000
    assert actual == expected
    assert actual["strategies"][1]["unrealized_gross_pnl"] is None
    assert actual["strategies"][0]["unrealized_gross_pnl"] is None
    # Six cursors, two trades and two latest active MARKs, not 1,500 event bodies.
    assert len(decoded) == 10
    print(f"SUMMARY_TIMING events=1500 elapsed_ms={elapsed_ms:.3f} json_decodes={len(decoded)}")
