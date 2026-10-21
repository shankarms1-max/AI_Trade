"""Synthetic paper evidence only: these quantities, prices and policies are not MCX validation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json

import pytest
from sqlalchemy import func, select, text

from app.commodity_acts.paper_engine import CommodityPaperEngine
from app.commodity_acts.paper_execution import candidates
from app.commodity_acts.paper_journal import verify
from app.commodity_acts.paper_models import (CommodityPaperControl, CommodityPaperCursor, CommodityPaperEvent,
                                           CommodityPaperFill, CommodityPaperPosition)
from app.commodity_acts.paper_policy import PaperPolicy, margin_comparison
from tests.test_commodity_acts import AT, MarketClient, captured, config, research_case


def policy(instrument="CRUDEOILM", **updates):
    lot = 10 if instrument == "CRUDEOILM" else 250
    data = dict(instrument_id=instrument, evidence=dict(instrument_id=instrument,
        future_token=f"TEST_{instrument}_F", future_expiry="2099-10-15", option_expiry="2099-10-15",
        lot_size=lot, rupees_per_point_per_lot=lot, depth_unit="UNITS", tick_size=.25,
        option_future_link_verified=True, source="SYNTHETIC_TEST_ONLY_NOT_BROKER_EVIDENCE",
        verified_at=AT-timedelta(days=1), valid_until=AT+timedelta(days=1)),
        max_loss_per_trade=300*lot, daily_loss_limit=1000*lot, max_entry_spread_pct=20,
        max_trades_per_session=1)
    data.update(updates)
    return PaperPolicy.model_validate(data)


def raw(instrument="CRUDEOILM", seconds=0, scale=1, corrupt=None):
    at = AT + timedelta(seconds=seconds)
    client = MarketClient(instrument, at)
    if instrument == "NATGASMINI":
        client.rows = [{**row, "lLotSize": 250} for row in client.rows]
    def values(rows):
        result = []
        for row in rows:
            token = row["exchange_token"]
            if token.endswith("_F"):
                result.append(row)
                continue
            strike, side = token.split("_")[-2:]
            mid = (100 + (float(strike)-5000) * (.15 if side == "PE" else -.15)) * scale
            result.append({**row, "ltp": str(mid), "depth": {
                "buy": [{"price": str(mid-1), "quantity": "1000"}],
                "sell": [{"price": str(mid+1), "quantity": "1000"}]}})
        return corrupt(result) if corrupt else result
    client.extra = values
    return captured(instrument, at=at, client=client)


@pytest.fixture
def case(research_case, monkeypatch):
    sessions, research, clock = research_case
    policies = {name: policy(name).model_dump(mode="json") for name in research}
    cfg = config(paper_enabled=True, crudeoilm_enabled=True, natgasmini_enabled=True, trading_enabled=True,
                 paper_policies_json=json.dumps(policies), global_max_defined_risk=200000, global_daily_loss_limit=400000)
    with sessions.begin() as session:
        session.add(CommodityPaperControl(id=1))
        session.add_all(CommodityPaperCursor(instrument_id=name) for name in research)
    for service in research.values():
        service.config = cfg
    engines = {name: CommodityPaperEngine(service) for name, service in research.items()}
    import app.commodity_acts.paper_engine as module
    original = module.build_context
    def directional(current, *args):
        ctx = original(current, *args)
        sign = getattr(clock, "direction", 1)
        ctx.update(futures_returns_bps={"1m": sign*4, "3m": sign*10, "5m": sign*20, "15m": sign*40},
                   opening_high=4990 if sign > 0 else 5020, opening_low=4980 if sign > 0 else 5010)
        ctx["structure"]["range_position"] = .95 if sign > 0 else .05
        return ctx
    monkeypatch.setattr(module, "build_context", directional)
    return sessions, research, engines, clock


def step(case, seconds, *, instrument="CRUDEOILM", scale=1, corrupt=None, lag=1):
    sessions, research, engines, clock = case
    clock.at = AT + timedelta(seconds=seconds+lag)
    sid, _ = research[instrument].observe(raw(instrument, seconds, scale, corrupt))
    engines[instrument].process_snapshot(sid)
    return sid


def positions(case, instrument="CRUDEOILM"):
    with case[0]() as session:
        return [(row.state, json.loads(row.document)) for row in session.scalars(select(CommodityPaperPosition).where(
            CommodityPaperPosition.instrument_id == instrument).order_by(CommodityPaperPosition.id))]


@pytest.mark.parametrize("instrument", ["CRUDEOILM", "NATGASMINI"])
@pytest.mark.parametrize("direction", [1, -1])
def test_real_actsv2_score_next_observation_atomic_fill_and_restart(case, instrument, direction):
    sessions, research, engines, clock = case
    clock.direction = direction
    signal = step(case, 0, instrument=instrument)
    state, doc = positions(case, instrument)[0]
    assert state == "PENDING_PAPER_ENTRY" and doc["decision"]["status"] == "ENTER"
    assert doc["decision"]["opportunity_score"] == pytest.approx(sum(doc["decision"]["score_components"].values()))
    assert doc["candidate"]["spread_width"] != 400
    assert doc["candidate"]["short_leg"]["option_type"] == ("PE" if direction > 0 else "CE")
    assert engines[instrument].process_snapshot(signal) == "DUPLICATE"
    assert positions(case, instrument)[0][0] == "PENDING_PAPER_ENTRY"
    engines[instrument] = CommodityPaperEngine(research[instrument])
    fill_id = step(case, 15, instrument=instrument)
    state, doc = positions(case, instrument)[0]
    assert state == "OPEN" and doc["entry_snapshot_id"] == fill_id
    assert doc["entry_credit"] == doc["entry_execution"]["short"]["price"]-doc["entry_execution"]["long"]["price"]
    assert doc["margin_status"] == "MARGIN_UNVERIFIED" and doc["unrealized_net"] is None
    assert doc["best_gross_points"] == doc["entry_credit"]-doc["current_debit"] < 0
    assert doc["entry_execution"]["short"]["quote"]["depth_unit"] == "UNKNOWN"
    assert doc["entry_execution"]["short"]["depth_interpretation"]["depth_unit"] == "UNITS"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommodityPaperFill)) == 1
    assert engines[instrument].process_snapshot(fill_id) == "DUPLICATE"
    verify(sessions, instrument)


@pytest.mark.parametrize("bad", ["depth", "hedge_depth", "crossed", "stale", "missing_price", "tick"])
def test_bad_or_partial_entry_depth_cannot_manufacture_a_fill(case, bad):
    step(case, 0)
    def corrupt(rows):
        for row in rows:
            if row["exchange_token"].endswith("_F"): continue
            if bad == "depth": row["depth"]["buy"][0]["quantity"] = "1"
            if bad == "hedge_depth": row["depth"]["sell"][0]["quantity"] = "1"
            if bad == "crossed": row["depth"]["sell"][0]["price"] = "1"
            if bad == "stale": row["lstup_time"] = str(int((AT-timedelta(seconds=60)).timestamp()))
            if bad == "missing_price": row["depth"]["buy"][0]["price"] = "0"
            if bad == "tick": row["depth"]["buy"][0]["price"] = "10.123"
        return rows
    step(case, 15, corrupt=corrupt)
    assert positions(case)[0][0] == "PENDING_PAPER_ENTRY"
    with case[0]() as session:
        assert session.scalar(select(func.count()).select_from(CommodityPaperFill)) == 0
    step(case, 30)
    assert positions(case)[0][0] == "OPEN"
    verify(case[0], "CRUDEOILM")


def test_ttl_rejection_after_restart_never_backfills_old_quotes(case):
    step(case, 0)
    step(case, 15, lag=3600)
    assert positions(case)[0][0] == "ENTRY_REJECTED"
    assert positions(case)[0][1]["rejection_reason"] == "ENTRY_TTL_EXCEEDED"
    verify(case[0], "CRUDEOILM")


def test_order_cannot_fill_from_an_observation_preceding_its_actual_creation(case):
    step(case, 0, lag=20)
    step(case, 15, lag=6)
    assert positions(case)[0][0] == "PENDING_PAPER_ENTRY"
    step(case, 30)
    assert positions(case)[0][0] == "OPEN"


@pytest.mark.parametrize("kind,scale,seconds", [("PROFIT_CAPTURE", .2, 30), ("SPREAD_STOP", 3, 30), ("TIME_STOP", 1, 1830)])
def test_exit_signal_is_not_a_close_and_later_observation_books_set_real_pnl(case, kind, scale, seconds):
    step(case, 0)
    step(case, 15)
    step(case, seconds, scale=scale)
    state, requested = positions(case)[0]
    assert state == "PENDING_EXIT" and requested["exit_reason"] == kind
    case[2]["CRUDEOILM"] = CommodityPaperEngine(case[1]["CRUDEOILM"])
    step(case, seconds+15, scale=scale)
    state, closed = positions(case)[0]
    assert state == "CLOSED"
    assert closed["exit_debit"] == closed["exit_execution"]["short"]["price"]-closed["exit_execution"]["long"]["price"]
    assert closed["gross_realized_pnl"] == pytest.approx((closed["entry_credit"]-closed["exit_debit"])*10)
    assert closed["net_realized_pnl"] is None and closed["return_on_verified_margin"] is None
    with case[0]() as session:
        assert session.scalar(select(func.count()).select_from(CommodityPaperFill)) == 2
    verify(case[0], "CRUDEOILM")


def test_missing_exit_quotes_preserve_exposure_and_recover_without_fake_close(case):
    step(case, 0)
    step(case, 15)
    def no_book(rows):
        return [{**row, "depth": {}} if not row["exchange_token"].endswith("_F") else row for row in rows]
    step(case, 1830, corrupt=no_book)
    state, doc = positions(case)[0]
    assert state == "UNRESOLVED_EXPOSURE" and doc["unrealized_gross"] is None and doc["exit_reason"] == "TIME_STOP"
    case[2]["CRUDEOILM"] = CommodityPaperEngine(case[1]["CRUDEOILM"])
    step(case, 1845)
    assert positions(case)[0][0] == "CLOSED"
    verify(case[0], "CRUDEOILM")


def test_global_risk_reservations_serialize_simultaneous_instruments(case):
    sessions, research, engines, clock = case
    cfg = config(paper_enabled=True, crudeoilm_enabled=True, natgasmini_enabled=True, trading_enabled=True,
        paper_policies_json=engines["CRUDEOILM"].config.paper_policies_json,
        global_max_positions=1, global_max_defined_risk=200000, global_daily_loss_limit=400000)
    ids = {name: service.observe(raw(name))[0] for name, service in research.items()}
    for engine in engines.values(): engine.config = cfg
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda name: engines[name].process_snapshot(ids[name]), list(engines)))
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommodityPaperPosition)) == 1
    for name in engines: verify(sessions, name)


@pytest.mark.parametrize("table,field", [("commodity_paper_positions", "document"), ("commodity_paper_orders", "document"),
                                         ("commodity_paper_fills", "document"), ("commodity_paper_cursors", "latest_json")])
def test_projection_or_fill_tampering_is_detected(case, table, field):
    step(case, 0)
    step(case, 15)
    with case[0].begin() as session:
        session.execute(text(f"UPDATE {table} SET {field}='{{}}' WHERE instrument_id='CRUDEOILM'"))
    with pytest.raises((RuntimeError, KeyError)):
        verify(case[0], "CRUDEOILM")


def test_margin_math_requires_same_short_actual_portfolio_evidence():
    common = dict(instrument_id="CRUDEOILM", short_token="TEST_SHORT", expiry="2099-10-15", lot_size=10,
                  product="NRML", portfolio_verified=True)
    naked, hedge = {**common, "hedge_token": None, "required_margin": 10000}, {**common, "hedge_token": "TEST_HEDGE", "required_margin": 7000}
    known = margin_comparison(naked, hedge, credit=10, hedge_ask=5, width=25, multiplier=10)
    assert known["margin_saved_pct"] == 30 and known["maximum_theoretical_loss"] == 150
    assert known["hedge_premium_cost"] == 50 and known["credit_to_verified_margin_estimate"] == pytest.approx(100/7000)
    assert known["credit_to_blocked_margin"] is None
    unknown = margin_comparison(None, None, credit=10, hedge_ask=5, width=25, multiplier=10)
    assert unknown["status"] == "MARGIN_UNVERIFIED" and unknown["spread_margin"] is None
    partial = margin_comparison(None, hedge, credit=10, hedge_ask=5, width=25, multiplier=10)
    assert partial["spread_margin"] == 7000 and partial["margin_saved_pct"] is None
    assert partial["status"] == "MARGIN_UNVERIFIED" and partial["credit_to_blocked_margin"] is None


def test_adaptive_selection_respects_defined_risk_and_no_assumed_width():
    snapshot = raw()
    choice, _ = candidates(snapshot, "BULL", {}, policy(max_loss_per_trade=500), AT+timedelta(seconds=1))
    assert choice and choice.spread_width == 50 and choice.defined_max_loss_per_lot <= 500
    none, rejected = candidates(snapshot, "BULL", {}, policy(max_loss_per_trade=1), AT+timedelta(seconds=1))
    assert none is None and rejected["DEFINED_RISK_OR_CREDIT_LIMIT"] > 0


def test_supported_dated_costs_only():
    unconfigured = policy()
    assert unconfigured.net(100, AT.date()) is None
    configured = policy(all_in_cost_per_leg_execution=2, cost_source="SYNTHETIC COMPLETE TEST COSTS",
                        cost_model="COMPLETE_FLAT_PER_EXECUTED_LEG",
                        cost_effective_from=AT.date(), cost_effective_to=AT.date())
    assert configured.net(100, AT.date()) == 92 and configured.net(100, (AT+timedelta(days=1)).date()) is None
    assert configured.net(100, AT.date(), (AT-timedelta(days=1)).date()) is None


def test_lots_depth_requires_explicit_evidence_and_preserves_raw_unknown():
    from app.commodity_acts.paper_execution import book
    snapshot = raw()
    quote = snapshot.options[0].model_copy(update={"bid_quantity": 1})
    unit_book, reason = book(quote, snapshot, policy(), AT+timedelta(seconds=1), entry=True, side="bid")
    assert unit_book is None and reason == "INSUFFICIENT_DEPTH"
    lots = policy(evidence={**policy().evidence.model_dump(), "depth_unit": "LOTS"})
    lot_book, reason = book(quote, snapshot, lots, AT+timedelta(seconds=1), entry=True, side="bid")
    assert reason is None and lot_book["validation"]["available_units"] == snapshot.lot_size
    assert lot_book["quote"]["bid_quantity"] == 1 and lot_book["quote"]["depth_unit"] == "UNKNOWN"
    with pytest.raises(ValueError):
        policy(evidence={**policy().evidence.model_dump(), "depth_unit": "UNKNOWN"})


@pytest.mark.parametrize("kind", ["verified", "stale", "future", "ambiguous"])
def test_margin_evidence_is_exact_current_and_unambiguous(kind):
    from app.commodity_acts.paper_policy import margin_for
    at = AT-timedelta(seconds=301) if kind == "stale" else AT+timedelta(seconds=1) if kind == "future" else AT
    record = dict(instrument_id="CRUDEOILM", short_token="TEST_SHORT", hedge_token="TEST_HEDGE",
        expiry="2099-10-15", lot_size=10, required_margin=7000, observed_at=at,
        source="SYNTHETIC_PORTFOLIO_ESTIMATE", portfolio_verified=True)
    configured = policy(margin_evidence=[record, record] if kind == "ambiguous" else [record])
    result = margin_for(configured, "TEST_SHORT", "TEST_HEDGE", configured.evidence.option_expiry, AT)
    assert (result is not None) == (kind == "verified")
    if result:
        assert result["required_margin"] == 7000 and result["measurement_kind"] == "PORTFOLIO_ESTIMATE"
    assert margin_for(configured, "TEST_DIFFERENT_SHORT", "TEST_HEDGE", configured.evidence.option_expiry, AT) is None
