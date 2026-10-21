"""Read-only views and crash-gap recovery use persisted evidence, never backfills."""
from datetime import timedelta
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select, text

from app.commodity_acts.broker import KotakCommodityMarketData
from app.commodity_acts.paper_coordinator import CommodityPaperCoordinator
from app.commodity_acts.paper_engine import CommodityPaperEngine
from app.commodity_acts.paper_journal import verify
from app.commodity_acts.paper_models import CommodityPaperCursor
from app.commodity_acts.paper_read import page, summary
from tests.test_commodity_paper import AT, MarketClient, case, positions, raw, research_case, step
from tests.test_scalper import settings


def test_read_views_preserve_zero_vs_unknown_and_hide_stale_pnl(case):
    cfg = case[1]["CRUDEOILM"].config
    empty = summary(case[0], cfg, case[3].at)
    assert empty["broker_orders_enabled"] is False
    assert empty["instruments"][0]["realized_gross"] is None
    step(case, 0)
    step(case, 15)
    result = summary(case[0], cfg, case[3].at)
    row = result["instruments"][0]
    assert row["observation_freshness"] == "FRESH" and row["observation_age_seconds"] == 1
    assert row["open_positions"] == 1 and row["margin_status"] == "MARGIN_UNVERIFIED"
    assert row["unrealized_gross"] < 0 and row["realized_net"] is None
    assert row["worker_state"] == "PROCESS_STATE_UNAVAILABLE" and not row["profitability_claim"]
    later = case[3].at + timedelta(minutes=1)
    assert summary(case[0], cfg, later)["instruments"][0]["observation_freshness"] == "STALE"
    assert summary(case[0], cfg, later)["instruments"][0]["unrealized_gross"] is None
    stale = page(case[0], "CRUDEOILM", "positions", at=later)["items"][0]
    assert stale["unrealized_gross"] is None and stale["mark_status"] == "UNAVAILABLE_OR_STALE"
    for kind in ("orders", "fills"):
        item = page(case[0], "CRUDEOILM", kind)["items"][0]
        assert item["candidate"]["short_leg"] == positions(case)[0][1]["candidate"]["short_leg"]
    assert page(case[0], "NATGASMINI", "positions")["items"] == []
    events = page(case[0], "CRUDEOILM", "events", limit=1)
    assert events["has_more"]
    assert events["items"][0]["sequence"] > page(case[0], "CRUDEOILM", "events", limit=1, offset=1)["items"][0]["sequence"]


def test_paper_api_only_reads_and_rejects_writes_filters_and_secret_errors(case, engine, monkeypatch):
    step(case, 0)
    step(case, 15)
    from app.main import app
    from app.db.session import get_session_factory
    from app.api.commodity_acts import get_commodity_settings
    def forbidden(*args, **kwargs): raise AssertionError("API_INVOKED_EXECUTION_OR_BROKER")
    monkeypatch.setattr(CommodityPaperEngine, "__init__", forbidden)
    monkeypatch.setattr(KotakCommodityMarketData, "__init__", forbidden)
    def only_read(_conn, _cursor, statement, *_):
        assert statement.lstrip().upper().startswith(("SELECT", "PRAGMA MAIN.TABLE_INFO", "PRAGMA TEMP.TABLE_INFO"))
    event.listen(engine, "before_cursor_execute", only_read)
    app.dependency_overrides[get_session_factory] = lambda: case[0]
    app.dependency_overrides[get_commodity_settings] = lambda: case[1]["CRUDEOILM"].config
    try:
        client = TestClient(app)
        assert client.get("/api/commodity-acts/paper/summary").status_code == 200
        for kind in ("positions", "orders", "fills", "events"):
            path = f"/api/commodity-acts/paper/{kind}"
            assert client.get(f"{path}?instrument=CRUDEOILM").status_code == 200
            assert client.get(f"{path}?instrument=NATGASMINI").json()["items"] == []
            assert client.post(path).status_code == 405
            for query in ("instrument=NIFTY", "instrument=CRUDEOILM&limit=101", "instrument=CRUDEOILM&offset=-1"):
                assert client.get(f"{path}?{query}").status_code == 422
        assert client.get("/api/commodity-acts/paper/broker-orders?instrument=CRUDEOILM").status_code == 422
    finally:
        event.remove(engine, "before_cursor_execute", only_read)
        app.dependency_overrides.clear()


def test_coordinator_recovers_v1_to_paper_crash_gap_without_same_observation_fill(case):
    first = step(case, 0)
    service = case[1]["CRUDEOILM"]
    case[3].at = AT+timedelta(seconds=16)
    missed, _ = service.observe(raw(seconds=15))  # V1 committed; V2 process died.
    restarted = CommodityPaperCoordinator(case[0], settings(), service.config, service.spec, clock=lambda: case[3].at)
    class ReadMarket:
        def capture(self, spec, **kwargs): return raw(seconds=30)
    case[3].at = AT+timedelta(seconds=31)
    restarted.capture_once(ReadMarket(), case[3].at)
    doc = positions(case)[0][1]
    assert doc["decision_snapshot_id"] == first and doc["entry_snapshot_id"] == missed
    verify(case[0], "CRUDEOILM")


def test_initial_paper_cursor_recovers_latest_v1_not_entire_history(case):
    service = case[1]["CRUDEOILM"]
    service.observe(raw())
    case[3].at = AT+timedelta(seconds=16)
    latest, _ = service.observe(raw(seconds=15))
    restarted = CommodityPaperCoordinator(case[0], settings(), service.config, service.spec, clock=lambda: case[3].at)
    class ReadMarket:
        def capture(self, spec, **kwargs): return raw(seconds=15)
    restarted.capture_once(ReadMarket(), case[3].at)
    doc = positions(case)[0][1]
    assert doc["decision_snapshot_id"] == latest
    assert positions(case)[0][0] == "PENDING_PAPER_ENTRY"
    verify(case[0], "CRUDEOILM")


def test_capture_requests_held_exact_contracts_outside_moving_atm_neighborhood():
    from tests.test_commodity_acts import config
    market = MarketClient()
    adapter = KotakCommodityMarketData(market, strikes_each_side=5, clock=lambda: AT)
    spec = config().specification("CRUDEOILM")
    initial = adapter.capture(spec)
    held = initial.options[0]
    market.future_price = 5250
    shifted = adapter.capture(spec, required_options=[held.model_dump(mode="json")])
    assert held.identity in {row.identity for row in shifted.options}
    assert any(held.instrument_token in {row["instrument_token"] for row in kwargs["instrument_tokens"]}
               for method, kwargs in market.calls[-2:] if method == "quotes")


def test_read_endpoints_on_v1_only_schema_are_non_mutating(case):
    with case[0].begin() as session:
        session.execute(text("DROP TABLE commodity_paper_cursors"))
    result = summary(case[0], case[1]["CRUDEOILM"].config, case[3].at)
    assert all(row["status"] == "MIGRATION_REQUIRED" for row in result["instruments"])
    assert page(case[0], "CRUDEOILM", "positions")["status"] == "MIGRATION_REQUIRED"
