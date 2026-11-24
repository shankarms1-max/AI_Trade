"""End-to-end ACTS_V3_MCX_FUTURES PAPER pipeline on SYNTHETIC data: worker Runner -> engine -> hash-chained store ->
/api/mcx-futures/* and /api/engines/status. Also proves isolation (instruments, NIFTY ACTS untouched), restart
idempotence, the concurrent-writer guard, PAPER-only start-up and the market-only capability check."""
from __future__ import annotations

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
        return {"at": o.at, "ltp": o.ltp, "bid": o.bid, "ask": o.ask, "bid_qty": None, "ask_qty": None,
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
    skewed = mcx_api.build_status(sessions, now=now - timedelta(minutes=10), settings=ENABLED)
    assert next(r for r in skewed["instruments"] if r["instrument"] == "CRUDEOILM")["state_reason"] == \
        "FEED_TIMESTAMP_IN_FUTURE_CLOCK_SKEW"  # a future-dated feed is never reported as healthy
    off = mcx_api.build_status(sessions, now=now, settings=McxFuturesSettings.from_env({}))
    assert {r["state"] for r in off["instruments"]} == {"DISABLED"}
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
