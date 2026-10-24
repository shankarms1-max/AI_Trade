"""Real database projections behind both route families; no worker or broker calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from app.alpha.clock import IST, SessionClock, session_id, trading_minutes_between, using_session
from app.api.commodity_acts import get_commodity_settings
from app.commodity_acts.config import CommoditySettings
from app.commodity_acts.paper_read import _sum_known, _today
from app.db.session import get_session_factory
from app.main import app


@pytest.fixture
def integration_client(session_factory):
    config = CommoditySettings(
        _env_file=None,
        paper_enabled=False,
        trading_enabled=False,
        crudeoilm_enabled=False,
        natgasmini_enabled=False,
        paper_policies_json="{}",
        crudeoilm_session_start=None,
        crudeoilm_session_end=None,
        crudeoilm_forced_exit=None,
        natgasmini_session_start=None,
        natgasmini_session_end=None,
        natgasmini_forced_exit=None,
        global_max_defined_risk=None,
        global_max_stress_risk=None,
        global_daily_loss_limit=None,
    )
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_commodity_settings] = lambda: config
    try:
        yield TestClient(app), config
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("instrument", ["CRUDEOILM", "NATGASMINI"])
def test_real_commodity_routes_are_200_without_capture_or_broker(integration_client, engine, monkeypatch, instrument):
    from app.commodity_acts.broker import KotakCommodityMarketData
    from app.commodity_acts.paper_coordinator import CommodityPaperCoordinator

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read API invoked capture or paper execution")

    monkeypatch.setattr(KotakCommodityMarketData, "__init__", forbidden)
    monkeypatch.setattr(CommodityPaperCoordinator, "__init__", forbidden)

    def read_only(_connection, _cursor, statement, *_args):
        assert statement.lstrip().upper().startswith(("SELECT", "PRAGMA MAIN.TABLE_INFO", "PRAGMA TEMP.TABLE_INFO"))

    event.listen(engine, "before_cursor_execute", read_only)
    try:
        client, _ = integration_client
        research = client.get("/api/commodity-acts/summary")
        assert research.status_code == 200
        selected = next(row for row in research.json()["instruments"] if row["instrument_id"] == instrument)
        assert selected["capture_state"] == "RESEARCH_DISABLED"
        assert selected["observation_count"] == 0 and selected["latest"] is None
        assert client.get(f"/api/commodity-acts/decisions?instrument={instrument}").json()["items"] == []
        response = client.get("/api/commodity-acts/paper/summary")
        assert response.status_code == 200 and response.json()["broker_orders_enabled"] is False
        row = next(row for row in response.json()["instruments"] if row["instrument_id"] == instrument)
        assert row["readiness"] == "PAPER_DISABLED"
        assert {"SESSION_UNCONFIGURED", "POLICY_UNCONFIGURED", "GLOBAL_RISK_UNCONFIGURED"} <= set(
            row["configuration_blockers"]
        )
        assert row["latest"] is row["realized_gross_today"] is row["realized_net_today"] is None
        assert row["trades_today"] == row["open_positions"] == 0
        for kind in ("positions", "orders", "fills", "events"):
            response = client.get(f"/api/commodity-acts/paper/{kind}?instrument={instrument}")
            assert response.status_code == 200 and response.json()["items"] == []
        nifty = client.get("/api/phase15/summary")
        assert nifty.status_code == 200 and len(nifty.json()["strategies"]) == 6
    finally:
        event.remove(engine, "before_cursor_execute", read_only)


def test_missing_or_invalid_policy_does_not_hide_independent_session(integration_client):
    client, config = integration_client
    config.paper_enabled = config.trading_enabled = config.crudeoilm_enabled = True
    config.crudeoilm_session_start, config.crudeoilm_session_end = time(9), time(22)
    config.crudeoilm_forced_exit, config.crudeoilm_session_source = time(21), "SYNTHETIC_LOCAL_TEST"
    row = client.get("/api/commodity-acts/paper/summary").json()["instruments"][0]
    assert row["readiness"] == "POLICY_UNCONFIGURED" and isinstance(row["market_open"], bool)
    config.paper_policies_json = "invalid-private-policy"
    response = client.get("/api/commodity-acts/paper/summary")
    assert response.status_code == 200 and "invalid-private-policy" not in response.text
    assert response.json()["instruments"][0]["readiness"] == "POLICY_INVALID"


def test_health_still_probes_the_local_database(integration_client):
    client, _ = integration_client
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["components"]["database"] == "HEALTHY"


def test_daily_accounting_never_infers_missing_costs_or_uses_future_exits():
    at = datetime(2026, 10, 23, 10, tzinfo=IST)
    assert _today({"exit_timestamp": at.astimezone().isoformat()}, "exit_timestamp", at)
    assert not _today({"exit_timestamp": (at + timedelta(seconds=1)).isoformat()}, "exit_timestamp", at)
    assert not _today({"exit_timestamp": (at - timedelta(days=1)).isoformat()}, "exit_timestamp", at)
    assert not _today({"exit_timestamp": "2026-10-23T10:00:00"}, "exit_timestamp", at)
    assert _sum_known([{"gross": 12.5}, {"gross": -4.0}], "gross") == 8.5
    assert _sum_known([], "net") is None
    assert _sum_known([{"net": 0}, {}], "net") is None
    assert _sum_known([{"net": float("nan")}], "net") is None


def test_opt_in_mcx_clock_restores_original_nse_behavior_even_after_failure():
    start = datetime(2026, 10, 23, 9, tzinfo=IST)
    end = start.replace(hour=23)
    assert trading_minutes_between(start, end, {start.date()}) == 375
    with pytest.raises(RuntimeError):
        with using_session(SessionClock("MCX_TEST", time(9), time(23))):
            assert trading_minutes_between(start, end, {start.date()}) == 840
            assert session_id(start).startswith("MCX_TEST_")
            raise RuntimeError("synthetic failure")
    assert session_id(start) == "NSE_2026-10-23"
    assert trading_minutes_between(start, end, {start.date()}) == 375


def test_instrument_clocks_do_not_cross_threads():
    at = datetime(2026, 10, 23, 10, tzinfo=IST)

    def read(instrument):
        with using_session(SessionClock(instrument, time(9), time(23))):
            return session_id(at)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(read, ["MCX_CRUDEOILM", "MCX_NATGASMINI"])) == [
            "MCX_CRUDEOILM_2026-10-23",
            "MCX_NATGASMINI_2026-10-23",
        ]
    assert session_id(at) == "NSE_2026-10-23"


def test_migration_seeded_empty_cursor_is_not_a_fictitious_observation(integration_client, session_factory):
    from app.commodity_acts.paper_models import CommodityPaperCursor

    with session_factory.begin() as session:
        session.add(
            CommodityPaperCursor(
                instrument_id="CRUDEOILM", sequence=0, head_hash="GENESIS", state_json="{}", latest_json="{}"
            )
        )
    response = integration_client[0].get("/api/commodity-acts/paper/summary")
    assert response.status_code == 200
    assert response.json()["instruments"][0]["latest"] is None
    assert response.json()["instruments"][0]["readiness"] == "PAPER_DISABLED"
