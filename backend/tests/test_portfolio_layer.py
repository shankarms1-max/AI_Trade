"""Portfolio read layer on the SYNTHETIC scenario in portfolio_support (hand-computed expectations)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import csv
import io

from fastapi.testclient import TestClient
import pytest

from app.api import portfolio as portfolio_api
from app.db.session import get_session_factory
from app.main import app
from app.portfolio import analytics, service
from app.portfolio.charges import ChargeModel
from tests.portfolio_support import IST, NOW, build, t

TODAY = date(2026, 11, 24)


@pytest.fixture(scope="module")
def world():
    S, truth = build()
    return S, truth, service.load(S, now=NOW.astimezone(), charges=ChargeModel(Decimal(0)))


def pos(pf, tid):
    return next(p for p in pf.positions if p["trade_id"].startswith(tid))


# -------------------------------------------------------------------------------------------------- charges
def test_option_charges_match_hand_computation():
    c = ChargeModel(Decimal(0))
    entry = c.options(date(2026, 11, 24), [(100.0, -1), (20.0, 1)], 65)
    exit_ = c.options(date(2026, 11, 24), [(90.0, 1), (15.0, -1)], 65)
    # entry: STT 6500x0.15%=9.75, exch 7800x0.03503%=2.73234, SEBI .0078, stamp 1300x0.003%=.039, GST 18%x(2.73234+.0078)
    assert entry["items"]["stt"] == 9.75 and entry["total"] == 13.02
    assert exit_["total"] == 4.47 and exit_["items"]["brokerage"] == 0.0


def test_stt_is_date_effective():
    c = ChargeModel(Decimal(0))
    old = c.options(date(2025, 6, 1), [(100.0, -1)], 75)["items"]["stt"]    # 0.10% (2024-10-01 .. 2026-03-31)
    new = c.options(date(2026, 6, 1), [(100.0, -1)], 75)["items"]["stt"]    # 0.15% from 2026-04-01
    assert (old, new) == (7.5, 11.25)


def test_futures_charges_ctt_on_sell_stamp_on_buy():
    c = ChargeModel(Decimal(0))
    buy, sell = c.futures(5600.0, 10, "BUY"), c.futures(5615.0, 10, "SELL")
    assert buy["items"]["stamp_duty"] == 1.12 and buy["items"]["ctt"] == 0.0
    assert sell["items"]["ctt"] == 5.62 and sell["items"]["stamp_duty"] == 0.0


def test_zero_brokerage_is_reported_as_unverified_not_free():
    st = ChargeModel(Decimal(0)).status()
    assert "PUBLISHED_SCHEDULE" in st["brokerage"] and "not confirmed" in st["brokerage"] and ChargeModel(Decimal(10)).status()["brokerage"] == "CONFIGURED_SCENARIO"
    assert ChargeModel(Decimal(0)).options(date(2026, 11, 24), [(100.0, -1)], 65)["total"] > 0


# ---------------------------------------------------------------------------------------------- normalisation
def test_closed_spread_gross_recomputed_from_fills_and_reconciles_with_engine(world):
    _, truth, pf = world
    for tid, key in (("T1", "T1_gross"), ("T2", "T2_gross")):
        p = pos(pf, tid)
        assert p["realized_gross"] == truth[key] == p["realized_gross_engine"]
        assert p["quantity"] == 65 and p["status"] == "CLOSED"
    t1 = pos(pf, "T1")
    assert t1["charges"]["incurred"]["total"] == 17.49 and t1["realized_net"] == 307.51


def test_multileg_parent_has_two_legs_and_is_not_double_counted(world):
    _, _, pf = world
    t1 = pos(pf, "T1")
    assert [l["role"] for l in t1["legs"]] == ["SHORT", "HEDGE"]
    assert (t1["legs"][0]["entry_price"], t1["legs"][1]["entry_price"], t1["legs"][0]["exit_price"],
            t1["legs"][1]["exit_price"]) == (100.0, 20.0, 90.0, 15.0)
    assert t1["entry_price"] == 80.0 and t1["exit_price"] == 75.0   # net credit / net debit per unit
    ids = [p["position_id"] for p in pf.positions]
    assert len(ids) == len(set(ids))
    s = service.summary(pf, start=TODAY, end=TODAY)
    assert s["counts"]["closed_trades"] == 4 and s["pnl"]["realized_gross"] == 325 - 910 + 150 + 375   # T1 T2 M1 M3


def test_futures_pnl_uses_contract_multiplier_and_direction(world):
    _, truth, pf = world
    m1, m3 = pos(pf, "M1"), pos(pf, "M3")
    assert (m1["quantity"], m1["multiplier"], m1["realized_gross"]) == (10, 10, truth["M1_gross"])
    assert (m3["quantity"], m3["realized_gross"]) == (250, truth["M3_gross"])      # SHORT 310.0 -> 308.5 x 250
    assert m3["legs"][0]["side_entry"] == "SELL" and m1["legs"][0]["side_entry"] == "BUY"
    assert abs(m1["realized_net"] - (150 - 9.64)) < 0.02                        # charges at brokerage 0


def test_fresh_mark_counts_stale_or_expired_mark_does_not(world):
    _, truth, pf = world
    t3, m2, t4 = pos(pf, "T3"), pos(pf, "M2"), pos(pf, "T4")
    assert t3["unrealized_gross"] == truth["T3_unrealized"] and t3["mark"]["basis"] == "EXECUTABLE_BID_ASK"
    assert m2["unrealized_gross"] == truth["M2_unrealized"]
    assert t4["unrealized_gross"] is None and t4["mark"]["basis"] == "STALE"
    assert t4["mark"]["last_known_price"] == 28.15 and t4["mark"]["reason"] == "EXPIRED_CONTRACT_NOT_SETTLED"


def test_expired_open_position_is_flagged_not_idle(world):
    _, _, pf = world
    t4 = pos(pf, "T4")
    assert "EXPIRED_NOT_SETTLED" in t4["flags"] and "PAST_FORCED_EXIT" in t4["flags"] and t4["status"] == "OPEN"
    assert t4["defined_max_loss"] == round((400 - 26.55) * 65, 2)


# ------------------------------------------------------------------------------------------------------ summary
def test_summary_totals_partial_and_exclusions(world):
    _, _, pf = world
    s = service.summary(pf, start=TODAY, end=TODAY)
    p = s["pnl"]
    assert p["unrealized_gross"] == -130 + 100                      # T3 + M2 only
    assert p["partial"] is True and p["excluded_open_positions"] == 1 and "PARTIAL" in p["note"]   # T4 excluded
    assert s["counts"]["open_positions"] == 3 and s["counts"]["unresolved_positions"] == 2   # T4 + expired-unpriced T6
    assert s["counts"]["rejected_orders"] == 2                     # T5 short + hedge entry orders
    # total_net = realized_net + unrealized - entry charges already paid on open positions
    open_entry = sum(x["charges"]["entry"]["total"] for x in (pos(pf, "T3"), pos(pf, "M2")))
    assert abs(p["total_net_pnl"] - (p["realized_net"] + p["unrealized_gross"] - open_entry)) < 0.011
    assert p["net_liquidation_pnl"] < p["total_net_pnl"] and p["estimated_exit_charges"] > 0


def test_unpriced_expired_closed_position_is_excluded_and_counted(world):
    _, _, pf = world
    t6 = pos(pf, "T6")
    assert t6["status"] == "EXPIRED_UNPRICED" and t6["realized_gross"] is None and t6["realized_net"] is None
    s = service.summary(pf, start=date(2026, 11, 19), end=date(2026, 11, 19))
    assert s["pnl"]["realized_gross"] == 0.0     # not silently counted as a zero-priced exit


def test_paper_and_live_are_isolated(world):
    _, _, pf = world
    assert service.summary(pf, start=TODAY, end=TODAY, mode="LIVE")["counts"]["open_positions"] == 0
    assert service.positions_list(pf, tab="all", mode="LIVE")["total"] == 0
    assert service.summary(pf, start=TODAY, end=TODAY, mode="PAPER")["counts"]["open_positions"] == 3


def test_ist_date_boundaries():
    S, _ = build()
    late = t(24, 23, 59, 30)
    pf = service.load(S, now=datetime.fromisoformat(t(25, 0, 0, 30)), charges=ChargeModel(Decimal(0)))
    p = pos(pf, "T1")
    assert service._in_window(p["exit_at"], TODAY, TODAY) and not service._in_window(late, date(2026, 11, 25), None)
    assert service._in_window(t(25, 0, 0, 5), date(2026, 11, 25), date(2026, 11, 25))


def test_engine_filter_isolates_strategies(world):
    _, _, pf = world
    s = service.summary(pf, start=TODAY, end=TODAY, engine="mcx-natgasmini")
    assert s["counts"]["closed_trades"] == 1 and s["pnl"]["realized_gross"] == 375.0
    assert service.summary(pf, start=TODAY, end=TODAY, instrument="CRUDEOILM")["pnl"]["realized_gross"] == 150.0


# --------------------------------------------------------------------------------------------- orders / fills
def test_orders_are_simulated_unique_and_never_have_broker_ids(world):
    _, _, pf = world
    assert all(o["simulated"] and o["broker_order_id"] is None and o["mode"] == "PAPER" for o in pf.orders)
    ids = [o["order_id"] for o in pf.orders]
    assert len(ids) == len(set(ids))
    t1 = [o for o in pf.orders if o["trade_id"].startswith("T1")]
    assert sorted((o["intent"], o["side"], o["avg_fill_price"]) for o in t1) == sorted([
        ("ENTRY", "SELL", 100.0), ("ENTRY", "BUY", 20.0), ("EXIT", "BUY", 90.0), ("EXIT", "SELL", 15.0)])
    assert all(o["filled_quantity"] == 65 and o["remaining_quantity"] == 0 for o in t1)


def test_rejected_entry_is_an_order_with_exact_reason_and_no_position_or_fill(world):
    _, _, pf = world
    rej = [o for o in pf.orders if o["trade_id"].startswith("T5")]
    assert {o["status"] for o in rej} == {"REJECTED"} and rej[0]["rejection_reason"] == "PENDING_ACTS_V2_DIRECTION_REVERSED"
    assert rej[0]["last_update_at"] == t(24, 13, 0, 30)             # from the journal event, not invented
    assert not [f for f in pf.fills if f["position_id"].endswith(pos(pf, "T5")["trade_id"])]
    assert pos(pf, "T5")["status"] == "REJECTED"
    assert "T5" not in " ".join(p["trade_id"] for p in service.positions_list(pf, tab="all")["items"])


def test_unpriced_expiry_exit_order_is_expired_not_filled(world):
    _, _, pf = world
    exits = [o for o in pf.orders if o["trade_id"].startswith("T6") and o["intent"] == "EXIT"]
    assert {o["status"] for o in exits} == {"EXPIRED"} and exits[0]["avg_fill_price"] is None
    assert not [f for f in pf.fills if f["order_id"] == exits[0]["order_id"]]


def test_fills_only_exist_for_filled_orders(world):
    _, _, pf = world
    filled = {o["order_id"] for o in pf.orders if o["status"] == "FILLED"}
    assert {f["order_id"] for f in pf.fills} == filled


# ---------------------------------------------------------------------------------------------- lists / csv
def test_positions_tabs_and_unresolved_first(world):
    _, _, pf = world
    opn = service.positions_list(pf, tab="open")
    assert opn["tab_counts"] == {"open": 3, "unresolved": 2, "closed": 5, "closed_priced": 4, "expired_unpriced": 1}
    assert opn["items"][0]["trade_id"].startswith("T4")             # attention first
    assert {p["status"] for p in service.positions_list(pf, tab="closed")["items"]} == {"CLOSED", "EXPIRED_UNPRICED"}


def test_pagination_and_sorting(world):
    _, _, pf = world
    page = service.trades_page(pf, limit=2, offset=0, sort="net_pnl", desc=True)
    assert page["total"] == 4 and len(page["items"]) == 2
    assert page["items"][0]["net_pnl"] >= page["items"][1]["net_pnl"]
    assert service.trades_page(pf, limit=2, offset=2, sort="net_pnl")["items"][0]["trade_id"] != page["items"][0]["trade_id"]


def test_csv_export_reconciles_with_visible_filters_and_totals(world):
    _, _, pf = world
    for kw in ({}, {"engine": "nifty-acts_v3"}, {"result": "loss"}, {"instrument": "NATGASMINI"}):
        rows = list(csv.DictReader(io.StringIO(service.trades_csv(pf, **kw))))
        page = service.trades_page(pf, limit=200, **kw)
        assert len(rows) == page["total"]
        assert round(sum(float(r["net_pnl"]) for r in rows), 2) == page["totals"]["net_pnl"]
        assert round(sum(float(r["gross_pnl"]) for r in rows), 2) == page["totals"]["gross_pnl"]


def test_same_realized_figure_on_every_surface(world):
    S, _, pf = world
    summ = service.summary(pf, start=TODAY, end=TODAY)["pnl"]["realized_net"]
    perf = analytics.performance(pf, start=TODAY, end=TODAY)["overall"]["net_pnl"]
    trades = service.trades_page(pf, start=TODAY, end=TODAY, limit=200)["totals"]["net_pnl"]
    positions = round(sum(p["realized_net"] for p in service.positions_list(pf, tab="closed", limit=200)["items"]
                          if p["realized_net"] is not None and service._in_window(p["exit_at"], TODAY, TODAY)), 2)
    eq = analytics.equity(S, pf, TODAY, engine="nifty-acts_v3")
    assert summ == perf == trades == positions
    assert eq["points"][-1]["realized_net"] == round(pos(pf, "T1")["realized_net"] + pos(pf, "T2")["realized_net"], 2)


# ---------------------------------------------------------------------------------------------- analytics
def test_performance_metrics_on_hand_numbers(world):
    _, _, pf = world
    o = analytics.performance(pf, engine="nifty-acts_v3")["overall"]
    n1, n2 = pos(pf, "T1")["realized_net"], pos(pf, "T2")["realized_net"]
    assert o["trades"] == 2 and o["wins"] == 1 and o["losses"] == 1 and o["win_rate"] == 0.5
    assert o["net_pnl"] == round(n1 + n2, 2) and o["expectancy_net"] == round((n1 + n2) / 2, 2)
    assert o["profit_factor"] == round(n1 / -n2, 3) and o["max_drawdown"] == round(-n2, 2)
    assert o["max_consecutive_losses"] == 1 and o["largest_win"] == n1 and o["largest_loss"] == n2


def test_performance_by_strategy_does_not_mix_engines(world):
    _, _, pf = world
    rows = {r["engine_id"]: r for r in analytics.performance(pf)["by_strategy"]}
    assert set(rows) == {"nifty-acts_v3", "mcx-crudeoilm", "mcx-natgasmini"}
    assert rows["mcx-crudeoilm"]["trades"] == 1 and rows["mcx-natgasmini"]["trades"] == 1
    assert "NOT risk-adjusted" in analytics.performance(pf)["comparison_note"]


def test_cost_sensitivity_changes_with_brokerage(world):
    _, _, pf = world
    s = analytics.performance(pf, engine="nifty-acts_v3")["cost_sensitivity_net_pnl"]
    assert s["brokerage_0_per_order"] > s["brokerage_10_per_order"] > s["brokerage_20_per_order"]
    assert round(s["brokerage_0_per_order"] - s["brokerage_10_per_order"], 2) == round(8 * 10 * 1.18, 2)   # 2 trades x 4 orders


def test_equity_is_event_sourced_and_flags_unmarked_minutes(world):
    S, _, pf = world
    eq = analytics.equity(S, pf, TODAY, engine="nifty-acts_v3")
    assert eq["available"] and [m["type"] for m in eq["markers"]].count("ENTRY") == 3
    by_t = {p["t"][11:16]: p for p in eq["points"]}
    assert by_t["10:20"]["unrealized_gross"] == round(260.0 - pos(pf, "T1")["charges"]["entry"]["total"], 2)
    assert by_t["11:15"]["partial"] is True                        # T2's mark was UNAVAILABLE that minute: not filled in
    assert by_t["10:30"]["open_positions"] == 0 and by_t["10:30"]["realized_gross"] == 325.0
    assert analytics.equity(S, pf, date(2026, 11, 20))["available"] is False


def test_risk_uses_contract_structure_and_never_invents_margin(world):
    _, _, pf = world
    r = analytics.risk(pf)
    assert r["exposure"]["margin_utilization"] is None and "NOT margin" in r["exposure"]["notional_note"]
    nifty = next(e for e in r["engines"] if e["engine_id"] == "nifty-acts_v3")
    assert nifty["daily_loss_limit"] == 50000.0 and nifty["max_risk_per_trade"] == 25000.0   # from the stored policy
    assert nifty["open_position_risk"] == round((400 - 36) * 65 + (400 - 26.55) * 65, 2)
    mcx = next(e for e in r["engines"] if e["engine_id"] == "mcx-crudeoilm")
    assert mcx["open_position_risk"] == 100.0                      # |5600-5590| x 10, a stop distance - not notional
    assert r["unresolved_exposure"][0]["flags"] == ["EXPIRED_NOT_SETTLED", "PAST_FORCED_EXIT"]


def test_alerts_prominent_for_expired_position(world):
    _, _, pf = world
    a = service.alerts(pf)
    assert a[0]["severity"] == "CRITICAL" and a[0]["code"] == "EXPIRED_POSITION_NOT_SETTLED"
    assert "excluded from totals" in a[0]["message"] and "Do not edit the journal" in a[0]["action"]


def test_position_detail_timeline_marks_and_pnl_explanation(world):
    S, _, pf = world
    d = analytics.position_detail(S, pf, pos(pf, "T1")["position_id"])
    assert [e["type"] for e in d["timeline"]] == ["ACTS_ENTRY", "ACTS_EXIT"] and d["mark_count"] == 2
    assert d["pnl_calculation"]["engine_recorded_gross"] == 325.0 and "short_entry - hedge_entry" in d["pnl_calculation"]["formula"]
    assert analytics.position_detail(S, pf, "nope") is None


def test_effective_policy_comes_from_the_stored_trade_policy(world):
    _, _, pf = world
    cfg = analytics.strategy_config(pf, "nifty-acts_v3")
    assert cfg["available"] and cfg["effective"]["hard_daily_loss"] == 50000 and cfg["policy_hash"] == "synthetic-policy-hash"
    mc = analytics.strategy_config(pf, "mcx-crudeoilm")
    assert mc["available"] and mc["calibration_status"] == "PRE_REGISTERED_UNVALIDATED"


# ------------------------------------------------------------------------------------- degraded / contract
def test_mcx_tables_missing_degrades_to_not_configured():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
    from app.scalper.models import ScalperMarketSnapshotRecord
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for m in (ActsPaperCursor, ActsPaperTrade, ActsPaperEvent, ScalperMarketSnapshotRecord):
        m.__table__.create(eng)
    pf = service.load(sessionmaker(eng), now=NOW.astimezone(), charges=ChargeModel(Decimal(0)))
    src = {s["source"]: s for s in pf.sources}
    assert src["mcx_futures"]["status"] == "NOT_CONFIGURED" and src["mcx_futures"]["note"] == "MCX_MIGRATION_NOT_APPLIED"
    assert src["ast_futures"]["status"] == "NOT_CONFIGURED" and pf.positions == []


def test_api_is_get_only_validated_and_complete():
    S, _ = build()
    app.dependency_overrides[get_session_factory] = lambda: S
    service._CACHE.clear()
    try:
        c = TestClient(app)
        for path in ("summary?date=2026-11-24", "positions?tab=all", "orders", "fills", "trades", "performance", "risk",
                     "equity?date=2026-11-24", "strategies", "alerts", "header", "sources", "strategies/nifty-acts_v3"):
            r = c.get(f"/api/portfolio/{path}")
            assert r.status_code == 200, (path, r.text[:200])
        assert c.get("/api/portfolio/summary?date=bad").status_code == 422
        assert c.get("/api/portfolio/summary?mode=SIM").status_code == 422
        assert c.get("/api/portfolio/trades?instrument=ZZZ").status_code == 422
        assert c.get("/api/portfolio/positions?tab=weird").status_code == 422
        assert c.get("/api/portfolio/positions/does-not-exist").status_code == 404
        assert c.get("/api/portfolio/strategies/nope").status_code == 404
        assert c.get("/api/portfolio/trades.csv").headers["content-type"].startswith("text/csv")
        for path in ("summary", "positions", "orders"):
            assert c.post(f"/api/portfolio/{path}").status_code == 405
    finally:
        app.dependency_overrides.clear()
        service._CACHE.clear()


# ------------------------------------------------------------------ unpriced exposure must never disappear
def test_expired_unpriced_exposure_is_visible_in_every_window_and_marks_totals_partial(world):
    _, _, pf = world
    t6 = pos(pf, "T6")
    assert t6["flags"] == ["EXPIRED_UNPRICED"]
    # a window that contains neither its entry nor its exit, and a window far in the past, both still disclose it
    for start, end in ((TODAY, TODAY), (date(2026, 1, 5), date(2026, 1, 5)), (date(2026, 11, 19), date(2026, 11, 19))):
        s = service.summary(pf, start=start, end=end)
        assert s["pnl"]["partial"] is True and s["pnl"]["excluded_unpriced_exposure"] == 1, (start, end)
        assert "terminalised without a settlement price" in s["pnl"]["note"] and "unknown, not zero" in s["pnl"]["note"]
        assert s["counts"]["expired_unpriced"] == 1 and s["counts"]["unresolved_positions"] >= 1
        assert any(a["position_id"] == t6["position_id"] and a["flags"] == ["EXPIRED_UNPRICED"] for a in s["attention"])
    scoped = service.summary(pf, start=TODAY, end=TODAY, engine="mcx-crudeoilm")      # another engine: not disclosed there
    assert scoped["pnl"]["excluded_unpriced_exposure"] == 0


def test_expired_unpriced_position_raises_a_critical_alert_and_counts_in_the_header(world):
    _, _, pf = world
    a = [x for x in service.alerts(pf) if x["code"] == "EXPIRED_POSITION_UNPRICED"]
    assert len(a) == 1 and a[0]["severity"] == "CRITICAL" and a[0]["position_id"] == pos(pf, "T6")["position_id"]
    assert "UNKNOWN" in a[0]["message"] and "Do not edit the journal" in a[0]["action"]


def test_charge_scopes_are_explicit_and_reconcile(world):
    _, _, pf = world
    s = service.summary(pf, start=date(2026, 1, 1), end=TODAY)
    p = s["pnl"]
    assert abs((p["realized_gross"] - p["realized_net"]) - p["charges_on_closed_trades"]) < 0.011
    assert abs(p["incurred_charges"] - p["charges_on_closed_trades"] - p["charges_not_in_closed_trades"]) < 0.011
    assert "Analytics" in p["incurred_charges_scope"]
    perf = analytics.performance(pf, start=date(2026, 1, 1), end=TODAY)
    assert "closed priced trades" in perf["overall"]["charges_scope"]


def test_header_accounts_for_every_active_feed_exactly_once():
    S, _ = build()
    app.dependency_overrides[get_session_factory] = lambda: S
    service._CACHE.clear()
    try:
        h = TestClient(app).get("/api/portfolio/header").json()
        f = h["data_freshness"]
        assert f["fresh_feeds"] + f["stale_feeds"] + f["unavailable_feeds"] + f["market_closed_feeds"] == f["feeds"]
        assert len(f["by_feed"]) == f["feeds"] and all(x["classification"] in (
            "fresh", "stale", "unavailable", "market_closed") for x in f["by_feed"])
        live = f["fresh_feeds"] + f["stale_feeds"] + f["unavailable_feeds"]
        if live and f["fresh_feeds"] < live:       # a degraded feed can never be reported as plain CONNECTED
            assert h["broker_connectivity"]["state"] in ("PARTIAL_INFERRED", "DISCONNECTED_INFERRED")
    finally:
        app.dependency_overrides.clear()
        service._CACHE.clear()
