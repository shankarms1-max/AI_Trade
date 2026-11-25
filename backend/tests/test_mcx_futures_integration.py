"""End-to-end ACTS_V3_MCX_FUTURES PAPER pipeline on SYNTHETIC data: worker Runner -> engine -> hash-chained store ->
/api/mcx-futures/* and /api/engines/status. Also proves isolation (instruments, NIFTY ACTS untouched), restart
idempotence, the concurrent-writer guard, PAPER-only start-up and the market-only capability check."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.acts_paper.models import ActsPaperCursor
from app.api import engines as engines_api
from app.api import mcx_futures as mcx_api
from app.db.base import Base
import app.db.models  # noqa: F401
from app.mcx_futures import store
from app.mcx_futures.models import McxFuturesCursor, McxFuturesEvent, McxFuturesTrade
from app.mcx_futures.settings import McxFuturesSettings
from tests.test_mcx_futures_support import master_rows, observations_from_bars, trend_bars

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
import mcx_futures_paper_worker as worker  # noqa: E402

ENABLED = McxFuturesSettings.from_env({"MCX_FUTURES_PAPER_ENABLED": "true", "MCX_FUTURES_CRUDEOILM_ENABLED": "true",
                                       "MCX_FUTURES_NATGASMINI_ENABLED": "true",
                                       "MCX_FUTURES_TRADING_ENABLED": "true",
                                       "MCX_FUTURES_KILL_SWITCH_FILE": "/nonexistent/kill"})


def _sessions():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as s:
        for inst in ("CRUDEOILM", "NATGASMINI"):
            s.add(McxFuturesCursor(instrument=inst, sequence=0, head_hash="GENESIS", lock_version=0,
                                   contract_json="{}", state_json="{}", latest_json="{}"))
        s.add(ActsPaperCursor(id=1))
    return sessions


class FakeMarket:
    """SYNTHETIC market-only client: replays a prepared observation list; exposes no order capability."""

    def __init__(self, instrument, observations):
        self.instrument, self.obs, self.i = instrument, observations, 0

    def master_rows(self, instrument):
        assert instrument == self.instrument
        return master_rows(instrument)

    def quote(self, token, trading_symbol):
        o = self.obs[self.i]
        return {"at": o.at, "ltp": o.ltp, "bid": o.bid, "ask": o.ask, "bid_qty": o.bid_qty, "ask_qty": o.ask_qty,
                "cum_volume": o.cum_volume, "oi": None, "source_ts": o.source_ts}

    def advance(self):
        self.i += 1


def _run(instrument, sessions, observations, upto=None):
    market = FakeMarket(instrument, observations)
    runner = worker.Runner(instrument, sessions, market, ENABLED, clock=lambda: market.obs[market.i].at)
    results = []
    for _ in range(len(observations) if upto is None else upto):
        results.append(runner.tick())
        market.advance()
    return runner, market, results


@pytest.fixture(scope="module")
def crude_run():
    sessions = _sessions()
    obs = observations_from_bars(trend_bars(date(2026, 11, 24), start_price=5600, slope=0.6, tick=1.0, minutes=600,
                                            wiggle=2.0), 1.0)
    runner, market, results = _run("CRUDEOILM", sessions, obs)
    return sessions, runner, results, obs


def test_pipeline_processes_every_observation_and_chain_verifies(crude_run):
    sessions, runner, results, obs = crude_run
    assert results.count("OK") == len(obs)
    v = store.verify_chain(sessions, "CRUDEOILM")
    assert v["ok"] and v["events"] > len(obs) / 4
    with sessions() as session:
        captured = session.scalars(select(McxFuturesEvent).where(
            McxFuturesEvent.instrument == "CRUDEOILM", McxFuturesEvent.event_type == "OBSERVATION"
        ).order_by(McxFuturesEvent.sequence)).all()
    assert len(captured) == len(obs)
    first = json.loads(captured[0].payload)["payload"]
    assert first["quote"]["at"] == obs[0].at.isoformat()
    assert first["quote"]["bid_qty"] == obs[0].bid_qty
    assert first["contract_token"] == runner.spec.token
    assert store.verify_chain(sessions, "NATGASMINI") == {"ok": True, "events": 0, "head_hash": "GENESIS"}


def test_trades_are_projected_with_contract_correct_pnl(crude_run):
    sessions, *_ = crude_run
    with sessions() as s:
        trades = [json.loads(t.document) | {"state": t.state} for t in s.scalars(select(McxFuturesTrade)).all()]
    closed = [t for t in trades if t["state"] == "CLOSED"]
    assert closed, "a steady synthetic trend should produce at least one paper trade"
    for t in closed:
        sign = 1 if t["direction"] == "LONG" else -1
        expected_gross = (t["exit_price"] - t["entry_price"]) * sign * 10 * t["quantity_lots"]  # CRUDEOILM lot 10
        assert t["gross_pnl"] == pytest.approx(expected_gross)
        assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["fees"])
        assert t["instrument"] == "CRUDEOILM" and t["mode"] == "PAPER" and t["contract"].startswith("CRUDEOILM")


def test_status_and_engines_api_reflect_journal(crude_run):
    sessions, runner, results, obs = crude_run
    now = obs[-1].at + timedelta(seconds=5)
    st = mcx_api.build_status(sessions, now=now, settings=ENABLED)
    crude = next(r for r in st["instruments"] if r["instrument"] == "CRUDEOILM")
    natgas = next(r for r in st["instruments"] if r["instrument"] == "NATGASMINI")
    assert st["mode"] == "PAPER" and st["calibration_status"] == "PRE_REGISTERED_UNVALIDATED"
    assert crude["state"] == "RUNNING" and crude["contract"]["lot_size"] == 10 and crude["contract"]["tick_size"] == 1.0
    assert crude["last_decision"]["status"] in ("ENTER", "NO_ENTRY", "EXIT_SIGNAL")
    assert set(crude["last_decision"]["components"]) == {"trend", "momentum", "structure", "strength", "regime",
                                                         "execution"}
    assert all(isinstance(v, float) for v in crude["last_decision"]["components"].values())
    assert natgas["state"] == "IDLE"  # enabled, never observed: not dressed up as running
    later = mcx_api.build_status(sessions, now=now + timedelta(minutes=5), settings=ENABLED)
    assert next(r for r in later["instruments"] if r["instrument"] == "CRUDEOILM")["state"] == "DATA_STALE"
    assert next(r for r in later["instruments"] if r["instrument"] == "CRUDEOILM")["feed"]["age_seconds"] > 300
    after_close = mcx_api.build_status(sessions, now=now.replace(hour=23, minute=56), settings=ENABLED)
    assert next(r for r in after_close["instruments"] if r["instrument"] == "CRUDEOILM")["state"] == \
        "MARKET_CLOSED"
    skewed = mcx_api.build_status(sessions, now=now - timedelta(minutes=10), settings=ENABLED)
    assert next(r for r in skewed["instruments"] if r["instrument"] == "CRUDEOILM")["state_reason"] == \
        "FEED_TIMESTAMP_IN_FUTURE_CLOCK_SKEW"  # a future-dated feed is never reported as healthy
    off = mcx_api.build_status(sessions, now=now, settings=McxFuturesSettings.from_env({}))
    # Standalone worker state wins over unrelated API-container environment flags.
    assert {r["instrument"]: r["state"] for r in off["instruments"]} == {
        "CRUDEOILM": "RUNNING", "NATGASMINI": "DISABLED"}
    assert next(r for r in off["instruments"] if r["instrument"] == "CRUDEOILM")["execution_state"] == "PAPER_ENABLED"
    perf = mcx_api.performance(instrument="CRUDEOILM", sessions=sessions)
    assert perf["by_instrument"]["CRUDEOILM"]["trades"] >= 1 and perf["mode"] == "PAPER"
    page = mcx_api.trades(instrument="CRUDEOILM", limit=2, before=None, sessions=sessions)
    assert len(page["items"]) <= 2 and all(i["mode"] == "PAPER" for i in page["items"])
    dec = mcx_api.decisions(instrument="CRUDEOILM", limit=20, sessions=sessions)
    assert dec["items"] and all(i["instrument"] == "CRUDEOILM" for i in dec["items"])
    scored = [i for i in dec["items"] if i["type"] == "DECISION" and i["status"] in ("ENTER", "NO_ENTRY", "EXIT_SIGNAL")]
    assert scored and all(isinstance(i["score"], (int, float)) for i in scored)
    assert all(set(i["components"]) == set(i["component_max"]) == {"trend", "momentum", "structure", "strength",
                                                                    "regime", "execution"} for i in scored)
    assert all(isinstance(v, float) for i in scored for v in i["components"].values())
    eng = engines_api.build(sessions, now=now.astimezone(timezone.utc), mcx_settings=ENABLED)
    ids = {e["id"]: e for e in eng["engines"]}
    assert ids["mcx-crudeoilm"]["state"] == "RUNNING" and ids["mcx-natgasmini"]["state"] == "IDLE"
    assert ids["ast-futures-v1-direct"]["state"] == "NOT_CONFIGURED"
    assert ids["nifty-acts"]["state"] in ("MARKET_CLOSED", "DATA_STALE")  # no NIFTY observation in this DB
    with pytest.raises(Exception):
        mcx_api.trades(instrument="NIFTY", limit=5, before=None, sessions=sessions)


def test_restart_resumes_from_store_without_duplicate_execution():
    sessions = _sessions()
    obs = observations_from_bars(trend_bars(date(2026, 11, 24), start_price=310, slope=0.03, tick=0.1, minutes=240,
                                            wiggle=0.12), 0.1)
    _, market, _ = _run("NATGASMINI", sessions, obs, upto=500)
    seq_before = store.load_cursor(sessions, "NATGASMINI")["sequence"]
    # A restarted worker replays the last processed observation first: the engine rejects it, nothing executes twice.
    market.i -= 1
    runner = worker.Runner("NATGASMINI", sessions, market, ENABLED, clock=lambda: market.obs[market.i].at)
    assert runner.tick() == "OK"
    with sessions() as s:
        last = s.scalars(select(McxFuturesEvent).where(McxFuturesEvent.instrument == "NATGASMINI")
                         .order_by(McxFuturesEvent.sequence.desc())).first()
    assert last.event_type == "OBSERVATION_REJECTED"
    assert store.load_cursor(sessions, "NATGASMINI")["sequence"] == seq_before + 1
    assert store.verify_chain(sessions, "NATGASMINI")["ok"]


def test_concurrent_writer_is_refused():
    sessions = _sessions()
    cur = store.load_cursor(sessions, "CRUDEOILM")
    store.commit_step(sessions, "CRUDEOILM", cur["lock_version"], [], state={}, latest={}, observed_at=None)
    with pytest.raises(store.StoreConflict):
        store.commit_step(sessions, "CRUDEOILM", cur["lock_version"], [], state={}, latest={}, observed_at=None)


def test_journal_events_are_immutable():
    sessions = _sessions()
    cur = store.load_cursor(sessions, "CRUDEOILM")
    store.commit_step(sessions, "CRUDEOILM", cur["lock_version"],
                      [{"type": "SESSION", "at": "2026-11-24T09:00:00+05:30", "payload": {}}],
                      state={}, latest={}, observed_at=None)
    with pytest.raises(ValueError, match="IMMUTABLE"):
        with sessions.begin() as s:
            row = s.scalars(select(McxFuturesEvent)).first()
            row.payload = "{}"


def test_only_paper_mode_can_start_and_flags_default_off(monkeypatch):
    monkeypatch.setenv("MCX_FUTURES_MODE", "LIVE")
    assert worker.main(["--mode", "PAPER", "--instrument", "CRUDEOILM"]) == 2
    with pytest.raises(SystemExit):
        worker.main(["--mode", "LIVE", "--instrument", "CRUDEOILM"])
    defaults = McxFuturesSettings.from_env({})
    assert not defaults.enabled("CRUDEOILM") and not defaults.enabled("NATGASMINI")
    assert not defaults.entries_enabled("CRUDEOILM")


def test_collect_only_mode_is_persisted_and_reported_without_api_env_flags():
    sessions = _sessions()
    stream = observations_from_bars(trend_bars(date(2026, 11, 24), start_price=5600, slope=0.6,
                                               tick=1.0, minutes=2), 1.0)
    flags = McxFuturesSettings.from_env({"MCX_FUTURES_PAPER_ENABLED": "true",
                                         "MCX_FUTURES_CRUDEOILM_ENABLED": "true",
                                         "MCX_FUTURES_TRADING_ENABLED": "false"})
    market = FakeMarket("CRUDEOILM", stream)
    runner = worker.Runner("CRUDEOILM", sessions, market, flags, clock=lambda: stream[0].at)
    assert runner.tick() == "OK"
    status = mcx_api.build_status(sessions, now=stream[0].at + timedelta(seconds=1),
                                  settings=McxFuturesSettings.from_env({}))
    crude = next(row for row in status["instruments"] if row["instrument"] == "CRUDEOILM")
    assert crude["state"] == "RUNNING"
    assert crude["execution_state"] == "COLLECT_ONLY"
    assert crude["execution_reason"] == "MCX_FUTURES_TRADING_ENABLED_FALSE"
    assert crude["warmup"]["ready"] is False
    with sessions() as session:
        assert not session.scalars(select(McxFuturesTrade)).all()


def test_missing_source_quote_timestamp_is_not_relabelled_as_capture_time():
    sessions = _sessions()
    original = observations_from_bars(trend_bars(date(2026, 11, 24), start_price=5600,
                                                 slope=0.6, tick=1.0, minutes=1), 1.0)[0]
    quote = replace(original, source_ts=None)
    market = FakeMarket("CRUDEOILM", [quote])
    runner = worker.Runner("CRUDEOILM", sessions, market, ENABLED, clock=lambda: quote.at)
    assert runner.tick() == "OK"
    row = next(item for item in mcx_api.build_status(sessions, now=quote.at + timedelta(seconds=1),
                                                      settings=ENABLED)["instruments"]
               if item["instrument"] == "CRUDEOILM")
    assert row["state"] == "DATA_UNAVAILABLE"
    assert row["state_reason"] == "NO_SOURCE_QUOTE_TIMESTAMP"
    assert row["feed"]["last_quote_at"] is None
    assert row["feed"]["observed_at"] == quote.at.isoformat()


def test_legacy_worker_mode_and_quote_source_remain_unverified():
    sessions = _sessions()
    at = "2026-11-24T12:00:00+05:30"
    store.commit_step(sessions, "CRUDEOILM", 0, [], state={},
                      latest={"observed_at": at, "session": {"status": "OPEN"},
                              "feed": {"last_quote_at": at, "bid": 5600.0, "ask": 5601.0}},
                      observed_at=at)
    row = next(item for item in mcx_api.build_status(
        sessions, now=datetime.fromisoformat(at) + timedelta(seconds=1),
        settings=McxFuturesSettings.from_env({}))["instruments"]
        if item["instrument"] == "CRUDEOILM")
    assert row["state"] == "DATA_UNAVAILABLE"
    assert row["execution_state"] == "LEGACY_MODE_UNVERIFIED"
    assert row["feed"]["last_quote_at"] is None
    assert row["feed"]["observed_at"] == at
    assert row["feed"]["bid_qty"] is None


def test_restart_uses_stored_held_contract_without_resolving_a_new_master():
    from app.mcx_futures.config import CRUDEOILM
    from app.mcx_futures.costs import DEFAULT_SCHEDULE
    from app.mcx_futures.engine import McxFuturesEngine
    from tests.test_mcx_futures_support import spec_for

    sessions = _sessions()
    spec = spec_for("CRUDEOILM")
    state = McxFuturesEngine(CRUDEOILM, spec, DEFAULT_SCHEDULE).snapshot()
    state["position"] = {"direction": "LONG"}  # synthetic restart identity fixture
    store.commit_step(sessions, "CRUDEOILM", 0, [], state=state, latest={}, observed_at=None,
                      contract=spec.to_dict())

    class NoDiscovery:
        def master_rows(self, *_):
            raise AssertionError("held contract must not be replaced by a current master")

    now = datetime(2026, 11, 24, 12, tzinfo=worker.IST)
    runner = worker.Runner("CRUDEOILM", sessions, NoDiscovery(), ENABLED, clock=lambda: now)
    runner._load(now)
    assert runner.spec.token == spec.token
    assert runner.engine.spec.to_dict() == spec.to_dict()


def test_prior_session_open_exposure_stays_visible_in_status():
    sessions = _sessions()
    with sessions.begin() as session:
        session.add(McxFuturesTrade(trade_id="synthetic-prior-open", instrument="CRUDEOILM",
                                    trading_date=date(2026, 11, 23), state="OPEN",
                                    document=json.dumps({"trade_id": "synthetic-prior-open", "instrument": "CRUDEOILM",
                                                         "state": "OPEN", "entry_at": "2026-11-23T22:00:00+05:30",
                                                         "entry_price": 5600.0, "direction": "LONG", "quantity_lots": 1})))
    now = datetime(2026, 11, 24, 12, tzinfo=worker.IST)
    row = next(item for item in mcx_api.build_status(sessions, now=now, settings=ENABLED)["instruments"]
               if item["instrument"] == "CRUDEOILM")
    assert row["open_position_count"] == 1
    assert row["position"]["entry_at"] == "2026-11-23T22:00:00+05:30"


def test_trade_count_includes_open_entry_and_realized_uses_exit_day():
    sessions = _sessions()
    with sessions.begin() as session:
        session.add(McxFuturesTrade(trade_id="today-open", instrument="CRUDEOILM",
                                    trading_date=date(2026, 11, 24), state="OPEN",
                                    document=json.dumps({"trade_id": "today-open", "instrument": "CRUDEOILM",
                                                         "entry_at": "2026-11-24T10:00:00+05:30",
                                                         "entry_price": 5600.0, "direction": "LONG"})))
        session.add(McxFuturesTrade(trade_id="yesterday-closed", instrument="CRUDEOILM",
                                    trading_date=date(2026, 11, 23), state="CLOSED",
                                    document=json.dumps({"trade_id": "yesterday-closed", "instrument": "CRUDEOILM",
                                                         "entry_at": "2026-11-23T22:00:00+05:30",
                                                         "exit_at": "2026-11-24T11:00:00+05:30",
                                                         "net_pnl": 125.0})))
    row = next(item for item in mcx_api.build_status(
        sessions, now=datetime(2026, 11, 24, 12, tzinfo=worker.IST), settings=ENABLED)["instruments"]
        if item["instrument"] == "CRUDEOILM")
    assert row["trades_today"] == 1
    assert row["realized_pnl_today"] == 125.0


def test_kill_switch_and_closed_session_do_not_touch_market(tmp_path):
    kill = tmp_path / "KILL"
    kill.write_text("stop")
    flags = McxFuturesSettings.from_env({"MCX_FUTURES_PAPER_ENABLED": "true", "MCX_FUTURES_CRUDEOILM_ENABLED": "true",
                                         "MCX_FUTURES_KILL_SWITCH_FILE": str(kill)})

    class Boom:
        def master_rows(self, *_):
            raise AssertionError("market must not be called")

        quote = master_rows

    r = worker.Runner("CRUDEOILM", _sessions(), Boom(), flags,
                      clock=lambda: datetime(2026, 11, 24, 12, 0, tzinfo=worker.IST))
    assert r.tick() == "KILL_SWITCH_ACTIVE"
    r2 = worker.Runner("CRUDEOILM", _sessions(), Boom(), ENABLED,
                       clock=lambda: datetime(2026, 11, 28, 12, 0, tzinfo=worker.IST))  # Saturday
    assert r2.tick() == "MARKET_CLOSED"


def test_market_reader_refuses_order_capable_clients():
    from app.commodity_acts.broker import CommodityDataError
    from app.mcx_futures.market import McxFuturesMarket

    class OrderCapable:
        def search_scrip(self, **_): ...
        def quotes(self, **_): ...
        def place_order(self, **_): ...

    with pytest.raises(CommodityDataError):
        McxFuturesMarket(OrderCapable())


def test_quote_parser_maps_kotak_fields_and_checks_identity():
    from app.commodity_acts.broker import CommodityDataError, MarketOnlySDK
    from app.mcx_futures.market import McxFuturesMarket

    row = {"exchange": "mcx_fo", "exchange_token": "576265", "display_symbol": "CRUDEOILM18DEC26FUT", "ltp": "5612",
           "last_volume": "123450", "open_int": "4321", "lstup_time": "1795080000",
           "depth": {"buy": [{"price": "5611", "quantity": "30"}], "sell": [{"price": "5613", "quantity": "20"}]}}
    m = McxFuturesMarket(MarketOnlySDK(lambda **_: [], lambda **_: {"data": [row]}))
    q = m.quote("576265", "CRUDEOILM18DEC26FUT")
    assert (q["ltp"], q["bid"], q["ask"], q["cum_volume"], q["oi"]) == (5612, 5611, 5613, 123450, 4321)
    with pytest.raises(CommodityDataError):
        m.quote("576265", "NATGASMINI28DEC26FUT")


def test_status_warmup_progress_and_old_cursor_null(crude_run):
    early_sessions = _sessions()
    early_observations = observations_from_bars(
        trend_bars(date(2026, 11, 24), start_price=5600, slope=0.6, tick=1.0, minutes=30, wiggle=2.0), 1.0
    )
    _run("CRUDEOILM", early_sessions, early_observations, upto=1)
    early_status = mcx_api.build_status(early_sessions, now=early_observations[0].at, settings=ENABLED)
    early_row = next(r for r in early_status["instruments"] if r["instrument"] == "CRUDEOILM")
    assert early_row["warmup"]["ready"] is False
    assert early_row["warmup"]["bars_available"] < early_row["warmup"]["bars_required"]
    sessions, _, _, obs = crude_run
    now = obs[-1].at + timedelta(seconds=5)
    ready = mcx_api.build_status(sessions, now=now, settings=ENABLED)
    row = next(r for r in ready["instruments"] if r["instrument"] == "CRUDEOILM")
    from app.mcx_futures.features import warmup_bars
    from app.mcx_futures.config import CRUDEOILM
    assert row["warmup"]["bars_required"] == warmup_bars(CRUDEOILM)
    assert row["warmup"]["bars_available"] >= row["warmup"]["bars_required"]
    assert row["warmup"]["ready"] is True
    assert next(r for r in ready["instruments"] if r["instrument"] == "NATGASMINI")["warmup"] is None
    with sessions.begin() as session:
        cursor = session.get(McxFuturesCursor, "CRUDEOILM")
        latest = json.loads(cursor.latest_json)
        latest.pop("warmup")
        cursor.latest_json = json.dumps(latest)
    older = mcx_api.build_status(sessions, now=now, settings=ENABLED)
    assert next(r for r in older["instruments"] if r["instrument"] == "CRUDEOILM")["warmup"] is None


def test_paper_entries_are_enabled_by_default_and_collect_only_is_an_explicit_opt_out():
    """A deployment that sets only MCX_FUTURES_PAPER_ENABLED + the instrument flag must take paper entries. An image
    swap must never silently turn a trading worker into a collector (regression: the flag once defaulted to false)."""
    on = McxFuturesSettings.from_env({"MCX_FUTURES_PAPER_ENABLED": "true", "MCX_FUTURES_CRUDEOILM_ENABLED": "true"})
    assert on.trading_enabled and on.entries_enabled("CRUDEOILM") and not on.entries_enabled("NATGASMINI")
    off = McxFuturesSettings.from_env({"MCX_FUTURES_PAPER_ENABLED": "true", "MCX_FUTURES_CRUDEOILM_ENABLED": "true",
                                       "MCX_FUTURES_TRADING_ENABLED": "false"})
    assert not off.entries_enabled("CRUDEOILM")
    assert not McxFuturesSettings.from_env({}).entries_enabled("CRUDEOILM")      # nothing enabled -> nothing runs


def test_session_metadata_is_current_and_persisted_history_is_labelled(crude_run):
    """Yesterday's cut-off times must never be presented as today's session (seen in production on 25 Nov)."""
    sessions, runner, results, obs = crude_run
    now = obs[-1].at + timedelta(seconds=5)
    same = next(r for r in mcx_api.build_status(sessions, now=now, settings=ENABLED)["instruments"]
                if r["instrument"] == "CRUDEOILM")
    day = now.date().isoformat()
    assert same["session"]["source"] == "CALENDAR_NOW" and same["session"]["session_date"] == day
    assert same["session"]["last_entry_time"][:10] == day and same["session"]["forced_exit_time"][:10] == day
    assert same["last_observed_session"]["is_current_session"] is True
    nxt_now = now + timedelta(days=1)                      # next calendar day, still inside a trading session
    nxt = next(r for r in mcx_api.build_status(sessions, now=nxt_now, settings=ENABLED)["instruments"]
               if r["instrument"] == "CRUDEOILM")
    assert nxt["session"]["status"] in ("OPEN", "CLOSED") and nxt["session"]["as_of"] == nxt_now.isoformat()
    if nxt["session"]["session_date"]:
        assert nxt["session"]["last_entry_time"][:10] == nxt["session"]["session_date"] != day
    hist = nxt["last_observed_session"]
    assert hist["is_current_session"] is False and hist["session_date"] == day and "HISTORICAL" in hist["note"]
