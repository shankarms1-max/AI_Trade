"""Research capture persists market evidence without entering a trading path."""
from datetime import datetime, time, timedelta
from pathlib import Path
import json
import os
import sys
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.scalper.config import ScalperConfig
from app.scalper.models import (ScalperEvent, ScalperMarketSnapshotRecord,
                                ScalperOptionQuoteRecord, ScalperTrade)
from app.scalper.research_capture import (IST, MODE, ResearchSnapshotCollector,
                                          ResearchSnapshotRepository, capture_allowed)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from worker import scalper_research_collector as worker  # noqa: E402


def _market(market_snapshot):
    at = datetime(2099, 10, 1, 10, 16, 25, tzinfo=IST)
    quote = market_snapshot.options[0].model_copy(update={
        "bid": 12.15, "ask": 12.25, "bid_quantity": 130,
        "ask_quantity": 260, "depth_unit": "UNITS", "tick_size": .05,
        "source_market_timestamp": at - timedelta(seconds=2)})
    return market_snapshot.model_copy(update={
        "timestamp_ist": at, "request_started_at": at - timedelta(seconds=1),
        "response_received_at": at, "future_instrument_id": "nse_fo|FUT123",
        "future_expiry": market_snapshot.expiry, "lot_size": 65,
        "options": [quote, *market_snapshot.options[1:]]})


def _collector(engine, market):
    return ResearchSnapshotCollector(
        ResearchSnapshotRepository(engine), lambda: market,
        start=time(9, 20), end=time(15, 20))


def test_capture_writes_only_snapshot_and_exact_option_rows(engine, market_snapshot):
    market = _market(market_snapshot)
    writes = []

    def record_write(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record_write)
    try:
        result = _collector(engine, market).capture_once(market.timestamp_ist)
    finally:
        event.remove(engine, "before_cursor_execute", record_write)
    assert result is not None and result.mode == MODE
    assert result.duplicate is False and result.quote_count == len(market.options)
    assert len(writes) >= 2
    assert all("scalper_market_snapshots" in sql or "scalper_option_quotes" in sql
               for sql in writes)

    with Session(engine) as session:
        snapshot = session.scalar(select(ScalperMarketSnapshotRecord))
        quotes = session.scalars(select(ScalperOptionQuoteRecord).order_by(
            ScalperOptionQuoteRecord.id)).all()
        assert session.scalar(select(func.count()).select_from(ScalperTrade)) == 0
        assert session.scalar(select(func.count()).select_from(ScalperEvent)) == 0
    assert snapshot is not None
    assert snapshot.captured_at.replace(tzinfo=IST) == market.timestamp_ist
    assert snapshot.request_started_at.replace(tzinfo=IST) == market.request_started_at
    assert snapshot.response_received_at.replace(tzinfo=IST) == market.response_received_at
    assert snapshot.future_instrument_id == market.future_instrument_id
    assert snapshot.future_expiry == market.future_expiry
    assert snapshot.nifty_future == market.nifty_future
    assert snapshot.india_vix is None
    assert snapshot.feature_json == snapshot.signal_json == "{}"
    assert json.loads(snapshot.context_json)["mode"] == MODE
    assert len(quotes) == len(market.options)
    first = quotes[0]
    source = market.options[0]
    assert (first.exchange, first.instrument_token, first.trading_symbol,
            first.expiry, float(first.strike), first.option_type) == (
                source.exchange, source.instrument_token, source.trading_symbol,
                source.expiry, source.strike, source.option_type.value)
    assert (float(first.bid), float(first.ask), first.bid_quantity,
            first.ask_quantity, first.depth_unit, first.volume,
            first.open_interest) == (source.bid, source.ask, source.bid_quantity,
                                     source.ask_quantity, source.depth_unit,
                                     source.volume, source.open_interest)
    assert first.source_market_timestamp.replace(tzinfo=IST) == source.source_market_timestamp


def test_repeated_capture_and_restart_are_bucket_idempotent(engine, market_snapshot):
    first_market = _market(market_snapshot)
    first = _collector(engine, first_market).capture_once(first_market.timestamp_ist)
    assert first is not None
    later = first_market.model_copy(update={
        "timestamp_ist": first_market.timestamp_ist + timedelta(seconds=3),
        "request_started_at": first_market.request_started_at + timedelta(seconds=3),
        "response_received_at": first_market.response_received_at + timedelta(seconds=3),
        "nifty_spot": first_market.nifty_spot + 10})
    second = _collector(engine, later).capture_once(later.timestamp_ist)
    assert second is not None and second.duplicate is True
    assert second.snapshot_id == first.snapshot_id
    next_bucket = later.model_copy(update={
        "timestamp_ist": later.timestamp_ist + timedelta(seconds=15),
        "request_started_at": later.request_started_at + timedelta(seconds=15),
        "response_received_at": later.response_received_at + timedelta(seconds=15)})
    third = _collector(engine, next_bucket).capture_once(next_bucket.timestamp_ist)
    assert third is not None and third.snapshot_id != first.snapshot_id
    with Session(engine) as session:
        rows = session.scalars(select(ScalperMarketSnapshotRecord).order_by(
            ScalperMarketSnapshotRecord.id)).all()
        assert len(rows) == 2
        assert float(rows[0].nifty_spot) == first_market.nifty_spot
        assert session.scalar(select(func.count()).select_from(
            ScalperOptionQuoteRecord)) == 2 * len(first_market.options)


def test_capture_skips_outside_window_weekends_and_holidays(engine, market_snapshot):
    market = _market(market_snapshot)
    calls = []

    def builder():
        calls.append(1)
        return market

    collector = ResearchSnapshotCollector(
        ResearchSnapshotRepository(engine), builder,
        start=time(9, 20), end=time(15, 20),
        holidays=frozenset({market.timestamp_ist.date()}))
    assert collector.capture_once(market.timestamp_ist) is None
    assert not calls
    assert capture_allowed(market.timestamp_ist.replace(hour=9, minute=19),
                           time(9, 20), time(15, 20), frozenset()) is False
    assert capture_allowed(market.timestamp_ist.replace(
        hour=15, minute=20, second=0),
        time(9, 20), time(15, 20), frozenset()) is True
    assert capture_allowed(market.timestamp_ist.replace(
        hour=15, minute=20, second=15),
        time(9, 20), time(15, 20), frozenset()) is False
    assert capture_allowed(datetime(2099, 10, 3, 10, tzinfo=IST),
                           time(9, 20), time(15, 20), frozenset()) is False
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(
            ScalperMarketSnapshotRecord)) == 0


def test_research_capture_begins_at_0915(engine, market_snapshot):
    market = _market(market_snapshot)
    opening = market.timestamp_ist.replace(hour=9, minute=15, second=0)
    quote = market.options[0].model_copy(update={
        "source_market_timestamp": opening - timedelta(seconds=2)})
    market = market.model_copy(update={"timestamp_ist": opening,
                                       "request_started_at": opening - timedelta(seconds=1),
                                       "response_received_at": opening,
                                       "options": [quote, *market.options[1:]]})
    collector = ResearchSnapshotCollector(
        ResearchSnapshotRepository(engine), lambda: market,
        start=worker.RESEARCH_START_TIME, end=time(15, 20))

    assert collector.capture_once(opening - timedelta(seconds=15)) is None
    assert collector.capture_once(opening) is not None
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(
            ScalperMarketSnapshotRecord)) == 1


def test_capture_is_independent_of_strategy_and_order_methods(
        engine, market_snapshot, monkeypatch):
    market = _market(market_snapshot)

    class StrictMarketDataBuilder:
        def __call__(self):
            return market

        def place_order(self, *_args, **_kwargs):
            raise AssertionError("broker order path invoked")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("strategy or paper path invoked")

    monkeypatch.setattr("app.scalper.service.ScalperService.capture_once", forbidden)
    monkeypatch.setattr("app.scalper.service.ScalperPaperEngine.process_snapshot", forbidden)
    monkeypatch.setattr("app.scalper.strategy.build_candidates", forbidden)
    collector = ResearchSnapshotCollector(
        ResearchSnapshotRepository(engine), StrictMarketDataBuilder(),
        start=time(9, 20), end=time(15, 20))
    result = collector.capture_once(market.timestamp_ist)
    assert result is not None and result.mode == MODE
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(
            ScalperMarketSnapshotRecord)) == 1


def test_worker_requires_database_url(monkeypatch):
    from tests.test_scalper import settings

    monkeypatch.setattr(worker, "get_settings", settings)
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
    monkeypatch.setattr(worker, "build_engine", lambda *_: (_ for _ in ()).throw(
        AssertionError("database must not be opened")))
    assert worker.main(["--mode", MODE]) == 2


@pytest.mark.parametrize("scalper_enabled", [False, True])
def test_worker_schedules_exact_fifteen_second_research_cadence(
        monkeypatch, scalper_enabled):
    from tests.test_scalper import settings

    jobs = []
    disposed = []
    windows = []

    class FakeEngine:
        def dispose(self):
            disposed.append(True)

    class FakeScheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, _run, trigger, **kwargs):
            jobs.append((trigger, kwargs))

        def add_listener(self, *_args):
            pass

        def start(self):
            pass

    original_collector = worker.ResearchSnapshotCollector

    def track_collector(*args, **kwargs):
        windows.append((kwargs["start"], kwargs["end"]))
        return original_collector(*args, **kwargs)

    monkeypatch.setattr(worker, "ResearchSnapshotCollector", track_collector)
    monkeypatch.setattr(worker, "get_settings", lambda: settings().model_copy(
        update={"scalper_enabled": scalper_enabled,
                "scalper_start_time": time(9, 20),
                "database_url": SecretStr("sqlite:///research-test")}))
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
    monkeypatch.setattr(worker, "build_engine", lambda *_: FakeEngine())
    monkeypatch.setattr(worker, "make_live_snapshot_builder", lambda *_: None)
    monkeypatch.setattr(worker, "BlockingScheduler", FakeScheduler)
    assert worker.main(["--mode", MODE]) == 0
    assert disposed == [True]
    assert windows == [(time(9, 15), time(15, 20))]
    assert ScalperConfig.from_settings(settings(
        scalper_start_time=time(9, 20))).start_time == time(9, 20)
    assert len(jobs) == 1
    trigger, options = jobs[0]
    now = datetime(2099, 10, 1, 10, 0, 1, tzinfo=IST)
    first = trigger.get_next_fire_time(None, now)
    second = trigger.get_next_fire_time(first, first)
    assert first is not None and second is not None
    assert (second - first).total_seconds() == 15
    opening = datetime(2099, 10, 1, 9, 14, 59, tzinfo=IST)
    first_opening = trigger.get_next_fire_time(None, opening)
    assert first_opening == datetime(2099, 10, 1, 9, 15, tzinfo=IST)
    assert capture_allowed(first_opening, *windows[0], frozenset())
    assert options["max_instances"] == 1
    assert options["coalesce"] is True


@pytest.mark.skipif(not os.getenv("PHASE15_POSTGRES_TEST_URL"),
                    reason="PHASE15_POSTGRES_TEST_URL is required")
def test_postgresql_research_capture_is_atomic_and_idempotent(market_snapshot):
    base = make_url(os.environ["PHASE15_POSTGRES_TEST_URL"])
    admin = create_engine(base)
    name = f"scalper_research_{uuid4().hex}"
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        engine = create_engine(base.set(database=name))
        try:
            ScalperMarketSnapshotRecord.__table__.create(engine)
            ScalperOptionQuoteRecord.__table__.create(engine)
            market = _market(market_snapshot)
            first = _collector(engine, market).capture_once(market.timestamp_ist)
            second = _collector(engine, market).capture_once(market.timestamp_ist)
            assert first is not None and second is not None
            assert first.snapshot_id == second.snapshot_id and second.duplicate
            with engine.connect() as connection:
                assert connection.scalar(select(func.count()).select_from(
                    ScalperMarketSnapshotRecord)) == 1
                assert connection.scalar(select(func.count()).select_from(
                    ScalperOptionQuoteRecord)) == len(market.options)
        finally:
            engine.dispose()
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
        admin.dispose()
