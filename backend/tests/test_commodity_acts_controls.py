"""Causality, persistence/restart, safe worker controls and observed MCX schema."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.alpha.clock import SessionClock, session_id, using_session
from app.acts_paper import acts_v2
from app.commodity_acts.broker import CommodityDataError, MarketOnlySDK, assert_market_only, integer, resolve_master
from app.commodity_acts.config import CommoditySettings, INSTRUMENTS, InstrumentAlias
from app.commodity_acts.domain import CommoditySnapshot
from app.commodity_acts.context import build_context
from app.commodity_acts.models import (
    CommodityActsCursor,
    CommodityActsDecision,
    CommodityActsEvent,
    CommodityMarketSnapshotRecord,
)
from app.commodity_acts.repository import save_snapshot, verify_journal
from app.commodity_acts.service import CommodityResearch
from app.research.manifest import canonical
from tests.test_commodity_acts import AT, MarketClient, captured, config, research_case
from tests.test_scalper import settings
from worker import commodity_acts_paper_worker as worker


@pytest.mark.parametrize("instrument", INSTRUMENTS)
def test_live_allowlisted_master_fixture_matches_exact_broker_family(instrument):
    fixture = json.loads((Path(__file__).parent / "fixtures/mcx_master_safe_20261020.json").read_text())
    future, options = resolve_master(
        fixture[instrument], InstrumentAlias(instrument_id=instrument, underlying=instrument), date(2026, 10, 20)
    )
    assert future["resolved_token"] == ("573423" if instrument == "CRUDEOILM" else "570751")
    assert future["resolved_lot"] == (10 if instrument == "CRUDEOILM" else 250)
    assert options[0]["resolved_strike"] == (7900 if instrument == "CRUDEOILM" else 290)
    assert len(options) == 2
    assert future["pInstType"] == "FUTCOM"  # Never CRUDEOILMCOM / cash dependency.


def test_live_zero_depth_and_unrecognized_1900_time_are_not_repaired():
    fixture = json.loads((Path(__file__).parent / "fixtures/mcx_quotes_safe_20261020.json").read_text())
    for instrument in INSTRUMENTS:
        source = fixture[instrument][0]
        client = MarketClient(instrument)
        client.extra = lambda rows: [
            {**row, "depth": source["depth"], "lstup_time": source["lstup_time"]} for row in rows
        ]
        data = captured(instrument, client=client)
        assert data.source_market_timestamp is None and data.raw_future_source_timestamp == source["lstup_time"]
        assert all(
            q.bid is None and q.ask is None and q.bid_quantity == 0 and q.ask_quantity == 0 for q in data.options
        )
        assert all(
            q.source_market_timestamp is None and q.raw_source_timestamp == source["lstup_time"] for q in data.options
        )


def test_alias_cannot_relabel_a_full_size_or_other_commodity_as_mini():
    with pytest.raises(ValueError, match="ALIAS_MISMATCH"):
        InstrumentAlias(instrument_id="CRUDEOILM", underlying="CRUDEOIL")


def test_quote_symbol_disagreement_is_not_accepted_by_token_alone():
    client = MarketClient()
    client.extra = lambda rows: [{**row, "display_symbol": "DIFFERENT_CONTRACT"} for row in rows]
    with pytest.raises(CommodityDataError, match="QUOTE_IDENTITY_MISMATCH"):
        captured(client=client)


def test_malformed_timestamp_evidence_reuses_safe_redaction():
    client = MarketClient()
    client.extra = lambda rows: [{**row, "lstup_time": "sid=PRIVATE consumer_key=PRIVATE"} for row in rows]
    data = captured(client=client)
    assert data.source_market_timestamp is None
    assert "PRIVATE" not in data.model_dump_json()
    assert "REDACTED" in data.raw_future_source_timestamp


def test_broker_integer_fields_do_not_pass_through_lossy_float_conversion():
    assert integer("9007199254740993") == 9007199254740993
    assert integer(str(2**63 - 1)) == 2**63 - 1
    for value in (True, "NaN", "Infinity", -1, "1.5", str(2**63)):
        assert integer(value) is None


@pytest.mark.parametrize(
    "method", ["place_order", "modify_order", "cancel_order", "cancel_all_orders", "buy", "execute"]
)
def test_market_data_executable_capabilities_are_rejected(method):
    unsafe = type("Unsafe", (), {method: lambda self: None})()
    with pytest.raises(CommodityDataError, match="ORDER_CAPABILITY_FORBIDDEN"):
        assert_market_only(unsafe)
    safe = MarketOnlySDK(lambda **_: [], lambda **_: [])
    assert_market_only(safe)
    assert not hasattr(safe, method)


def test_scoped_clock_is_thread_local_and_nifty_default_unchanged():
    def scoped(name):
        with using_session(SessionClock(f"MCX_{name}", time(9), time(22))):
            return session_id(AT)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(scoped, INSTRUMENTS)) == [f"MCX_{name}_2099-10-01" for name in INSTRUMENTS]
    assert session_id(AT) == "NSE_2099-10-01"


def test_alpha_receives_only_its_instrument_history(research_case, monkeypatch):
    sessions, services, clock = research_case
    for instrument in INSTRUMENTS:
        services[instrument].observe(captured(instrument))
    import app.commodity_acts.service as module

    original = module.build_alpha_features
    seen = []

    def inspect_history(snapshot_id, current, history, *args):
        assert all(row.instrument_id == current.instrument_id for row in history)
        seen.append((current.instrument_id, len(history), session_id(current.timestamp_ist)))
        return original(snapshot_id, current, history, *args)

    monkeypatch.setattr(module, "build_alpha_features", inspect_history)
    clock.at = AT + timedelta(minutes=3, seconds=1)
    for instrument in INSTRUMENTS:
        services[instrument].observe(captured(instrument, at=AT + timedelta(minutes=3)))
    assert seen == [(instrument, 1, f"MCX_{instrument}_2099-10-01") for instrument in INSTRUMENTS]


def test_directional_commodity_score_is_original_unscaled_and_execution_blocked(research_case):
    sessions, services, clock = research_case
    research = services["CRUDEOILM"]
    history = []
    for minute, price in ((0, 5000), (10, 5010), (12, 5020)):
        at = AT + timedelta(minutes=minute)
        market = MarketClient(at=at)
        market.future_price = price
        row = captured(at=at, client=market)
        save_snapshot(sessions, row, at)
        history.append(row)
    at = AT + timedelta(minutes=15)
    market = MarketClient(at=at)
    market.future_price = 5030
    current = captured(at=at, client=market)
    clock.at = at + timedelta(seconds=1)
    snapshot_id, _ = research.observe(current)
    with sessions() as session:
        doc = json.loads(
            session.scalar(
                select(CommodityActsDecision.document).where(CommodityActsDecision.snapshot_id == snapshot_id)
            )
        )
    ctx = build_context(current, history, research.spec, clock.at, research.config.max_quote_age_seconds)
    ctx["acts"] = {"alpha": doc["acts"]["alpha"]}
    expected = acts_v2.decide(ctx, {}, None, {"COMMODITY_EXECUTION_POLICY_NOT_VALIDATED": 1}, [], research.acts_config)
    actual = doc["acts"]
    assert actual["direction"] == expected["direction"] and actual["market_state"] == expected["market_state"]
    assert actual["raw_context_points"] == expected["opportunity_score"]
    assert actual["directional_components"] == {
        k: v for k, v in expected["score_components"].items() if k != "execution"
    }
    assert actual["directional_score"] == pytest.approx(100 * expected["opportunity_score"] / 86, abs=0.0001)
    assert expected["direction"] == "BULL" and expected["opportunity_score"] > 0
    assert expected["score_components"]["execution"] == 0
    assert expected["opportunity_score"] == pytest.approx(sum(expected["score_components"].values()))
    assert (
        expected["score_components"]["alpha"]
        == (research.acts_config.weights["alpha_1"] + research.acts_config.weights["alpha_2"])
        * research.acts_config.alpha_factors["unavailable"]
    )
    assert expected["status"] == "NO_SETUP" and "NO_EXECUTABLE_CANDIDATE" in expected["blockers"]
    assert doc["maximum_score_without_execution"] == 86 and doc["pnl"] is None


def test_out_of_order_or_future_observation_is_rejected(research_case):
    sessions, services, clock = research_case
    research = services["CRUDEOILM"]
    future_id, _ = save_snapshot(sessions, captured(at=AT + timedelta(minutes=1)), clock.at)
    with pytest.raises(ValueError, match="FUTURE_OBSERVATION"):
        research.process_snapshot(future_id)
    clock.at = AT + timedelta(minutes=1, seconds=1)
    research.process_snapshot(future_id)
    earlier_id, _ = save_snapshot(sessions, captured(), AT)
    with pytest.raises(ValueError, match="OUT_OF_ORDER"):
        research.process_snapshot(earlier_id)


def test_changed_future_token_resets_cached_alpha_in_the_same_bucket(research_case, monkeypatch):
    sessions, services, clock = research_case
    research = services["CRUDEOILM"]
    research.observe(captured())
    import app.commodity_acts.service as module

    original = module.build_alpha_features
    calls = []

    def inspect_history(snapshot_id, current, history, prior, *args):
        calls.append((current.future_token, len(history), len(prior)))
        return original(snapshot_id, current, history, prior, *args)

    monkeypatch.setattr(module, "build_alpha_features", inspect_history)
    at = AT + timedelta(seconds=15)
    clock.at = at + timedelta(seconds=1)
    current = captured(at=at).model_copy(update={"future_token": "CHANGED_TEST_FUTURE_TOKEN"})
    snapshot_id, _ = research.observe(current)
    assert calls == [("CHANGED_TEST_FUTURE_TOKEN", 0, 0)]
    with sessions() as session:
        doc = json.loads(
            session.scalar(
                select(CommodityActsDecision.document).where(CommodityActsDecision.snapshot_id == snapshot_id)
            )
        )
    assert datetime.fromisoformat(doc["alpha"]["timestamp"]) == at
    verify_journal(sessions, "CRUDEOILM")


def test_quote_time_from_future_cannot_enter_a_snapshot():
    data = captured()
    bad = data.options[0].model_copy(update={"source_market_timestamp": AT + timedelta(seconds=1)})
    with pytest.raises(ValueError, match="FUTURE_QUOTE_TIMESTAMP"):
        CommoditySnapshot.model_validate({**data.model_dump(), "options": [bad, *data.options[1:]]})


@pytest.mark.parametrize(
    "table,column",
    [
        ("commodity_acts_cursors", "latest_json"),
        ("commodity_acts_events", "payload"),
        ("commodity_acts_decisions", "document"),
        ("commodity_market_snapshots", "snapshot_json"),
    ],
)
def test_journal_verification_detects_tampering(research_case, table, column):
    sessions, services, _ = research_case
    services["CRUDEOILM"].observe(captured())
    # create_all fixtures intentionally have no DB triggers; verification must
    # still detect a damaged projection/evidence independently of those guards.
    with sessions.begin() as session:
        session.execute(text(f"UPDATE {table} SET {column}='{{}}' WHERE instrument_id='CRUDEOILM'"))
    with pytest.raises((RuntimeError, ValueError, KeyError)):
        verify_journal(sessions, "CRUDEOILM")


def test_snapshot_without_decision_is_recovered_before_new_capture(research_case):
    sessions, services, clock = research_case
    research = services["CRUDEOILM"]
    orphan, _ = save_snapshot(sessions, captured(), clock.at)
    clock.at = AT + timedelta(seconds=16)
    newer = captured(at=AT + timedelta(seconds=15))

    class Market:
        def capture(self, _spec):
            return newer

    result = research.capture_once(Market(), clock.at)
    assert result and result[1] == "SHADOW_DECISION"
    with sessions() as session:
        assert list(
            session.scalars(select(CommodityActsDecision.snapshot_id).order_by(CommodityActsDecision.snapshot_id))
        ) == [orphan, result[0]]
    verify_journal(sessions, "CRUDEOILM")


def test_orm_evidence_is_immutable(research_case):
    sessions, services, _ = research_case
    services["CRUDEOILM"].observe(captured())
    with pytest.raises(ValueError, match="EVIDENCE_IS_IMMUTABLE"):
        with sessions.begin() as session:
            session.scalar(select(CommodityActsDecision)).document = "{}"


def test_disabled_worker_never_touches_broker_database_or_scheduler(monkeypatch):
    monkeypatch.setattr(worker, "CommoditySettings", lambda: CommoditySettings(_env_file=None))
    monkeypatch.setattr(worker, "get_settings", settings)
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)

    def forbidden(*_a, **_kw):
        raise AssertionError("disabled worker touched external systems")

    monkeypatch.setattr(worker, "build_engine", forbidden)
    monkeypatch.setattr(worker, "make_adapter", forbidden)
    assert worker.main(["--mode", "PAPER", "--instrument", "CRUDEOILM"]) == 0
    with pytest.raises(SystemExit):
        worker.main(["--mode", "LIVE", "--instrument", "CRUDEOILM"])


def test_enabled_worker_refuses_missing_verified_session_before_connecting(monkeypatch):
    monkeypatch.setattr(
        worker,
        "CommoditySettings",
        lambda: CommoditySettings(_env_file=None, paper_enabled=True, crudeoilm_enabled=True),
    )
    monkeypatch.setattr(worker, "get_settings", settings)
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
    monkeypatch.setattr(worker, "build_engine", lambda *_: pytest.fail("invalid session touched database"))
    assert worker.main(["--mode", "PAPER", "--instrument", "CRUDEOILM"]) == 2


def test_worker_status_is_read_only_without_research_or_broker(research_case, engine, monkeypatch, capsys):
    sessions, _, _ = research_case
    monkeypatch.setattr(worker, "CommoditySettings", config)
    monkeypatch.setattr(worker, "get_settings", lambda: settings(database_url="sqlite://"))
    monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
    monkeypatch.setattr(worker, "build_engine", lambda *_: engine)
    monkeypatch.setattr(worker, "build_session_factory", lambda *_: sessions)

    def forbidden(*_a, **_kw):
        raise AssertionError("status invoked capture/research")

    monkeypatch.setattr(worker, "CommodityResearch", forbidden)
    monkeypatch.setattr(worker, "make_adapter", forbidden)
    assert worker.main(["--mode", "PAPER", "--instrument", "CRUDEOILM", "--status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["instruments"]) == 1 and result["instruments"][0]["readiness"] == "NOT_CAPTURED"


# Re-export test fixtures and helpers for the shared PostgreSQL validation suite.
__all__ = [
    "ThreadPoolExecutor",
    "date",
    "datetime",
    "time",
    "timedelta",
    "json",
    "Path",
    "pytest",
    "select",
    "text",
    "SessionClock",
    "session_id",
    "using_session",
    "acts_v2",
    "CommodityDataError",
    "MarketOnlySDK",
    "assert_market_only",
    "integer",
    "resolve_master",
    "CommoditySettings",
    "INSTRUMENTS",
    "InstrumentAlias",
    "CommoditySnapshot",
    "build_context",
    "CommodityActsCursor",
    "CommodityActsDecision",
    "CommodityActsEvent",
    "CommodityMarketSnapshotRecord",
    "save_snapshot",
    "verify_journal",
    "CommodityResearch",
    "canonical",
    "AT",
    "MarketClient",
    "captured",
    "config",
    "research_case",
    "settings",
    "worker",
]
