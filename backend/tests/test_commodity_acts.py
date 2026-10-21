"""Synthetic MCX-only evidence; token/lot/strike values are NOT live identities."""
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select

from app.alpha.clock import IST, SessionClock, session_id, trading_minutes_between, using_session
from app.acts_paper import acts_v2
from app.acts_paper.models import ActsPaperTrade
from app.commodity_acts.broker import CommodityDataError, KotakCommodityMarketData, assert_market_only, resolve_master
from app.commodity_acts.config import CommoditySettings, INSTRUMENTS
from app.commodity_acts.domain import CommoditySnapshot
from app.commodity_acts.geometry import geometry
from app.commodity_acts.models import (CommodityActsCursor, CommodityActsDecision, CommodityActsEvent,
                                      CommodityMarketSnapshotRecord, CommodityOptionQuoteRecord)
from app.commodity_acts.repository import save_snapshot, verify_journal
from app.commodity_acts.service import CommodityResearch
from app.parallel_paper.models import ParallelPaperTrade
from tests.test_scalper import settings

AT = datetime(2099, 10, 1, 10, tzinfo=IST)


def config(**kwargs):
    return CommoditySettings(_env_file=None, crudeoilm_session_start=time(9), crudeoilm_session_end=time(22),
        crudeoilm_forced_exit=time(21, 55), crudeoilm_session_source="SYNTHETIC_TEST_SESSION",
        natgasmini_session_start=time(9), natgasmini_session_end=time(22), natgasmini_forced_exit=time(21, 55),
        natgasmini_session_source="SYNTHETIC_TEST_SESSION", **kwargs)


def master(instrument="CRUDEOILM", *, step=25):
    expiry = date(2099, 10, 15)
    common = {"pExchSeg": "mcx_fo", "pSymbolName": instrument, "pExpiryDate": "15Oct2099",
              "lExpiryDate": int(datetime.combine(expiry, time(), timezone.utc).timestamp()), "lLotSize": 10, "dTickSize": 5}
    rows = [{**common, "pSymbol": f"TEST_{instrument}_F", "pTrdSymbol": f"TEST_{instrument}_FUT",
             "pOptionType": "XX", "pInstType": "FUTCOM", "dStrikePrice;": -1}]
    for index in range(-10, 11):
        strike = 5000 + index * step
        for side in ("CE", "PE"):
            rows.append({**common, "pSymbol": f"TEST_{instrument}_{strike}_{side}",
                "pTrdSymbol": f"TEST_{instrument}_{strike}_{side}", "pOptionType": side, "pInstType": "OPTFUT",
                "dStrikePrice;": strike * 100})
    return rows


class MarketClient:
    """Only documented read methods. Full SDK objects must not be passed in."""
    def __init__(self, instrument="CRUDEOILM", at=AT):
        self.rows, self.at = master(instrument), at
        self.calls, self.missing, self.extra = [], set(), None
        self.future_price = 5000

    def search_scrip(self, **kwargs):
        self.calls.append(("search", kwargs))
        return self.rows

    def quotes(self, **kwargs):
        self.calls.append(("quotes", kwargs))
        result = []
        for item in kwargs["instrument_tokens"]:
            token = item["instrument_token"]
            if token in self.missing:
                continue
            result.append({"exchange": "mcx_fo", "exchange_token": token, "ltp": str(self.future_price if token.endswith("_F") else 40),
                "last_volume": "1000", "open_int": "5000", "lstup_time": str(int(self.at.timestamp())),
                "depth": {"buy": [{"price": "39", "quantity": "20"}], "sell": [{"price": "41", "quantity": "30"}]}})
        if self.extra and result:
            result = self.extra(result)
        return result


@pytest.fixture
def research_case(session_factory, monkeypatch):
    with session_factory.begin() as session:
        session.add_all([CommodityActsCursor(instrument_id=instrument) for instrument in INSTRUMENTS])
    clock = SimpleNamespace(at=AT + timedelta(seconds=1))
    import app.alpha.engine as alpha_engine
    class TestDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.at if tz is None else clock.at.astimezone(tz)
    monkeypatch.setattr(alpha_engine, "datetime", TestDateTime)
    services = {instrument: CommodityResearch(session_factory, settings(), config(), config().specification(instrument),
                                            clock=lambda: clock.at) for instrument in INSTRUMENTS}
    return session_factory, services, clock


def captured(instrument="CRUDEOILM", at=AT, client=None):
    client = client or MarketClient(instrument, at)
    return KotakCommodityMarketData(client, clock=lambda: at).capture(config().specification(instrument))


def test_original_nse_clock_ids_and_scoped_mcx_clock_are_isolated():
    assert session_id(AT) == "NSE_2099-10-01"
    start, end = AT.replace(hour=9), AT.replace(hour=20)
    assert trading_minutes_between(start, end, {AT.date()}) == 375
    with using_session(SessionClock("MCX_CRUDEOILM", time(9), time(22))):
        assert session_id(AT) == "MCX_CRUDEOILM_2099-10-01"
        assert trading_minutes_between(start, end, {AT.date()}) == 660
        with using_session(SessionClock("MCX_NATGASMINI", time(9), time(22))):
            assert session_id(AT) == "MCX_NATGASMINI_2099-10-01"
        assert session_id(AT).startswith("MCX_CRUDEOILM")
    assert session_id(AT) == "NSE_2099-10-01"


def test_clock_resets_on_failure_and_sessions_are_required():
    with pytest.raises(RuntimeError):
        with using_session(SessionClock("MCX_CRUDEOILM", time(9), time(22))):
            raise RuntimeError("fixture")
    assert session_id(AT).startswith("NSE_")
    with pytest.raises(ValueError, match="VERIFIED_SESSION"):
        CommoditySettings(_env_file=None).specification("CRUDEOILM")
    assert not config().enabled("CRUDEOILM")
    spec = config().specification("CRUDEOILM")
    assert spec.market_session.contains(AT.replace(hour=22))
    assert not spec.market_session.contains(AT.replace(hour=22, second=1))
    assert not spec.market_session.contains(AT.replace(hour=8))


@pytest.mark.parametrize("instrument", INSTRUMENTS)
def test_master_and_batch_capture_are_instrument_specific_not_nifty(instrument):
    client = MarketClient(instrument)
    data = captured(instrument, client=client)
    assert data.future_token == f"TEST_{instrument}_F" and data.future_expiry == date(2099, 10, 15)
    assert data.reference_price == data.future_price == 5000 and data.reference_basis == "SELECTED_MCX_FUTURE"
    assert data.lot_size == 10 and len(data.options) == 42
    assert not hasattr(data, "nifty_spot") and not hasattr(data, "india_vix")
    assert all(q.depth_unit == "UNITS" and q.tick_size is None and q.raw_master_tick_size == 5 for q in data.options)
    assert data.options[0].bid == 39 and data.options[0].volume == 1000 and data.options[0].open_interest == 5000
    assert client.calls[0] == ("search", {"exchange_segment": "mcx_fo", "symbol": instrument, "ignore_50multiple": True})
    assert all(len(kwargs["instrument_tokens"]) <= 50 for method, kwargs in client.calls if method == "quotes")
    assert len([call for call in client.calls if call[0] == "quotes"]) == 2


@pytest.mark.parametrize("problem,reason", [("future", "FUTURE_UNRESOLVED"), ("duplicate_future", "AMBIGUOUS_COMMODITY_FUTURE"),
    ("options", "OPTION_EXPIRY_UNRESOLVED"), ("duplicate_option", "AMBIGUOUS_COMMODITY_OPTION"), ("lot", "LOT_SIZE_AMBIGUOUS")])
def test_ambiguous_or_missing_resolution_fails_closed(problem, reason):
    rows = master()
    if problem == "future": rows = rows[1:]
    if problem == "duplicate_future": rows.append({**rows[0], "pSymbol": "OTHER_TOKEN"})
    if problem == "options": rows = rows[:1]
    if problem == "duplicate_option": rows.append({**rows[1], "pSymbol": "OTHER_TOKEN"})
    if problem == "lot": rows[1]["lLotSize"] = 9
    with pytest.raises(CommodityDataError, match=reason):
        resolve_master(rows, config().specification("CRUDEOILM"), AT.date())


def test_substring_search_does_not_accept_another_contract_family():
    rows = [{**row, "pSymbolName": "CRUDEOIL"} for row in master()]
    with pytest.raises(CommodityDataError, match="FUTURE_UNRESOLVED"):
        resolve_master(rows, config().specification("CRUDEOILM"), AT.date())


def test_missing_chain_and_duplicate_quote_identity_fail_closed():
    client = MarketClient()
    client.missing = {row["pSymbol"] for row in client.rows[1:]}
    with pytest.raises(CommodityDataError, match="OPTION_CHAIN_UNAVAILABLE"):
        captured(client=client)
    client = MarketClient()
    client.extra = lambda rows: rows + [dict(rows[0])]
    with pytest.raises(CommodityDataError, match="DUPLICATE_OR_UNREQUESTED"):
        captured(client=client)
    data = captured()
    with pytest.raises(ValueError, match="DUPLICATE_QUOTE_IDENTITY"):
        CommoditySnapshot.model_validate({**data.model_dump(), "options": [*data.options, data.options[0]]})


def test_future_quote_times_and_missing_values_are_not_fabricated():
    client = MarketClient(at=AT + timedelta(seconds=1))
    with pytest.raises(CommodityDataError, match="TIMESTAMP_INVALID"):
        captured(client=client)
    client = MarketClient()
    def missing(rows):
        return [{key: value for key, value in row.items() if key not in ("depth", "open_int", "last_volume", "lstup_time")} for row in rows]
    client.extra = missing
    data = captured(client=client)
    assert all(q.bid is None and q.ask is None and q.volume is None and q.open_interest is None for q in data.options)
    assert data.source_market_timestamp is None


def test_adapter_refuses_order_capable_objects_and_errors_never_leak_secrets():
    class Unsafe(MarketClient):
        def place_order(self): raise AssertionError("unreachable")
    with pytest.raises(CommodityDataError, match="ORDER_CAPABILITY_FORBIDDEN"):
        KotakCommodityMarketData(Unsafe())
    class Broken(MarketClient):
        def search_scrip(self, **kwargs): raise RuntimeError("consumer_key=SECRET sid=SECRET mpin=SECRET")
    with pytest.raises(CommodityDataError) as error:
        captured(client=Broken())
    assert str(error.value) == "COMMODITY_MARKET_REQUEST_FAILED" and "SECRET" not in repr(error.value)


def test_geometry_uses_observed_steps_not_a_400_point_policy():
    evidence = geometry(captured())
    assert evidence["modal_strike_step"] == 25 and evidence["complete_pairs"] == 21
    assert evidence["bid_ask_coverage"] == 1 and evidence["oi_coverage"] == 1 and evidence["volume_coverage"] == 1
    assert evidence["spread_execution_enabled"] is False
    assert evidence["observed_bid_ask_credits_by_steps"]["1"][0]["observed_width"] == 25
    assert "allowed_widths" not in json.dumps(evidence)


def test_same_bucket_two_instruments_no_nifty_writes_and_hash_restart(research_case):
    sessions, services, clock = research_case
    ids = []
    for instrument in INSTRUMENTS:
        ident, result = services[instrument].observe(captured(instrument))
        assert result == "SHADOW_DECISION"
        ids.append(ident)
        assert services[instrument].observe(captured(instrument)) == (ident, "DUPLICATE")
        verify_journal(sessions, instrument)
        CommodityResearch(sessions, settings(), config(), config().specification(instrument), clock=lambda: clock.at)
    assert len(set(ids)) == 2
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommodityMarketSnapshotRecord)) == 2
        assert session.scalar(select(func.count()).select_from(CommodityOptionQuoteRecord)) == 84
        assert session.scalar(select(func.count()).select_from(CommodityActsDecision)) == 2
        assert session.scalar(select(func.count()).select_from(ActsPaperTrade)) == 0
        assert session.scalar(select(func.count()).select_from(ParallelPaperTrade)) == 0
        for row in session.scalars(select(CommodityActsDecision)):
            doc = json.loads(row.document)
            assert doc["alpha"]["session_id"] == f"MCX_{row.instrument_id}_2099-10-01"
            assert doc["geometry"]["depth_unit"] == "UNITS" and doc["pnl"] is None
            assert doc["acts"]["score_components"]["execution"] == 0
            assert doc["maximum_score_without_execution"] == 86
            assert doc["score_basis"] == "ORIGINAL_100_POINT_WEIGHTS_NO_RESCALING"
            assert doc["readiness"] == "ALPHA_WARMING_UP"


def test_history_never_crosses_instruments_future_contracts_or_causal_times(research_case):
    sessions, services, clock = research_case
    original = captured()
    services["CRUDEOILM"].observe(original)
    services["NATGASMINI"].observe(captured("NATGASMINI"))
    current = captured(at=AT + timedelta(seconds=15))
    assert [row.instrument_id for row in services["CRUDEOILM"]._history(current)] == ["CRUDEOILM"]
    changed = current.model_copy(update={"future_token": "DIFFERENT_FUTURE"})
    assert services["CRUDEOILM"]._history(changed) == []
    with pytest.raises(ValueError, match="OWNER_MISMATCH"):
        services["CRUDEOILM"].process_snapshot(2)


def test_original_acts_config_is_unchanged_and_enter_only_journals_shadow(research_case, monkeypatch):
    sessions, services, _ = research_case
    research = services["CRUDEOILM"]
    assert research.acts_config == acts_v2.ActsV2Config.from_settings(settings())
    original = acts_v2.decide
    def enter(*args, **kwargs):
        value = original(*args, **kwargs)
        return {**value, "status": "ENTER"}
    monkeypatch.setattr(acts_v2, "decide", enter)
    research.observe(captured())
    with sessions() as session:
        assert session.scalar(select(CommodityActsEvent.event_type)) == "SHADOW_DECISION"
        assert session.scalar(select(func.count()).select_from(ActsPaperTrade)) == 0
        assert session.scalar(select(func.count()).select_from(ParallelPaperTrade)) == 0


def test_capture_failure_is_safe_idempotent_research_evidence(research_case):
    sessions, services, _ = research_case
    research = services["CRUDEOILM"]
    research.record_failure("FUTURE_UNRESOLVED", AT)
    research.record_failure("FUTURE_UNRESOLVED", AT)
    verify_journal(sessions, "CRUDEOILM")
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommodityActsEvent)) == 1
        latest = json.loads(session.get(CommodityActsCursor, "CRUDEOILM").latest_json)
        assert latest["readiness"] == "FUTURE_UNRESOLVED"


def test_api_is_read_only_separate_and_bounded(research_case, engine, monkeypatch):
    sessions, services, _ = research_case
    services["CRUDEOILM"].observe(captured())
    from app.main import app
    from app.db.session import get_session_factory
    from app.api.commodity_acts import get_commodity_settings
    def forbidden(*_a, **_kw): raise AssertionError("read endpoint wrote or invoked research/broker")
    monkeypatch.setattr(CommodityResearch, "__init__", forbidden)
    monkeypatch.setattr(KotakCommodityMarketData, "__init__", forbidden)
    def sql(_conn, _cursor, statement, *_):
        assert statement.lstrip().upper().startswith("SELECT")
    event.listen(engine, "before_cursor_execute", sql)
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_commodity_settings] = lambda: config()
    try:
        client = TestClient(app)
        for path in ("status", "summary"):
            response = client.get(f"/api/commodity-acts/{path}")
            assert response.status_code == 200 and len(response.json()["instruments"]) == 2
        assert len(client.get("/api/commodity-acts/decisions?instrument=CRUDEOILM").json()["items"]) == 1
        assert client.get("/api/commodity-acts/decisions?instrument=NATGASMINI").json()["items"] == []
        for query in ("instrument=NIFTY", "instrument=CRUDEOILM&limit=101", "instrument=CRUDEOILM&offset=-1"):
            assert client.get(f"/api/commodity-acts/decisions?{query}").status_code == 422
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        app.dependency_overrides.clear()
