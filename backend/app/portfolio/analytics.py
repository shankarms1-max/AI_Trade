"""Performance analytics, risk, event-sourced equity, position drill-down and strategy dimensions (read only)."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.acts_paper.models import ActsPaperEvent
from app.mcx_futures.models import McxFuturesEvent
from app.portfolio import normalize as N
from app.portfolio.service import IST, Portfolio, _in_window, needs_attention, select_positions, summary
from app.portfolio.charges import money
from app.scalper.models import ScalperMarketSnapshotRecord

D = Decimal


def _dec(values) -> Decimal:
    return sum((D(str(v)) for v in values), D(0))


# ----------------------------------------------------------------------------------------------------- performance
def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    net = [p["realized_net"] for p in rows]
    gross = [p["realized_gross"] for p in rows]
    wins, losses = [n for n in net if n > 0], [n for n in net if n <= 0]
    seq = sorted(rows, key=lambda p: p["exit_at"] or "")
    eq = peak = dd = D(0)
    run_w = run_l = best_w = best_l = 0
    for p in seq:
        n = D(str(p["realized_net"]))
        eq += n
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        run_w, run_l = (run_w + 1, 0) if n > 0 else (0, run_l + 1)
        best_w, best_l = max(best_w, run_w), max(best_l, run_l)
    gp, gl = _dec(wins), _dec(losses)
    hold = [p["holding_seconds"] for p in rows if p["holding_seconds"] is not None]
    return {
        "trades": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(rows), 4) if rows else None,
        "gross_pnl": money(_dec(gross)), "net_pnl": money(_dec(net)),
        "charges": money(_dec(g - n for g, n in zip(gross, net))),
        "charges_scope": "closed priced trades only: entry + exit charges (gross - net)",
        "gross_profit": money(gp), "gross_loss": money(gl),
        "expectancy_net": money(_dec(net) / len(rows)) if rows else None,
        "avg_win": money(gp / len(wins)) if wins else None, "avg_loss": money(gl / len(losses)) if losses else None,
        "profit_factor": round(float(gp / -gl), 3) if gl < 0 else None,
        "largest_win": max(net) if net else None, "largest_loss": min(net) if net else None,
        "max_drawdown": money(dd), "current_drawdown": money(peak - eq) if rows else None,
        "max_consecutive_wins": best_w, "max_consecutive_losses": best_l,
        "avg_holding_seconds": round(sum(hold) / len(hold), 1) if hold else None,
        "first_exit": seq[0]["exit_at"] if seq else None, "last_exit": seq[-1]["exit_at"] if seq else None,
        "avg_defined_risk": money(_dec(p["defined_max_loss"] for p in rows if p["defined_max_loss"]) / max(
            1, sum(1 for p in rows if p["defined_max_loss"]))) if any(p["defined_max_loss"] for p in rows) else None,
        "net_to_max_drawdown": round(float(_dec(net) / dd), 3) if dd > 0 else None,
    }


def _bucket(rows, key, labels) -> list[dict[str, Any]]:
    groups: dict[str, list] = defaultdict(list)
    for p in rows:
        label = key(p)
        if label is not None:
            groups[label].append(p)
    return [{"bucket": b, "trades": len(groups[b]), "net_pnl": money(_dec(p["realized_net"] for p in groups[b])),
             "win_rate": round(sum(1 for p in groups[b] if p["realized_net"] > 0) / len(groups[b]), 3)}
            for b in labels if b in groups]


def performance(pf: Portfolio, *, start: date | None = None, end: date | None = None, **filters: Any) -> dict[str, Any]:
    pos = select_positions(pf, **filters)
    closed = [p for p in pos if p["status"] == "CLOSED" and p["realized_net"] is not None
              and (not (start or end) or _in_window(p["exit_at"], start, end))]
    excluded = sum(1 for p in pos if p["status"] in ("CLOSED", "EXPIRED_UNPRICED") and p["realized_net"] is None)
    daily: dict[str, list] = defaultdict(list)
    for p in closed:
        daily[N.dt(p["exit_at"]).astimezone(IST).date().isoformat()].append(p)
    cum = D(0)
    series = []
    for day in sorted(daily):
        rows = daily[day]
        net = _dec(p["realized_net"] for p in rows)
        cum += net
        series.append({"date": day, "trades": len(rows), "gross": money(_dec(p["realized_gross"] for p in rows)),
                       "net": money(net), "cum_net": money(cum)})
    months: dict[str, Decimal] = defaultdict(lambda: D(0))
    for s in series:
        months[s["date"][:7]] += D(str(s["net"]))
    by_strategy = []
    for key in sorted({p["engine_id"] for p in closed}):
        rows = [p for p in closed if p["engine_id"] == key]
        by_strategy.append({"engine_id": key, "label": rows[0]["strategy_label"], "instrument": rows[0]["instrument"],
                            **_stats(rows)})

    def score_b(p):
        s = p["score"]
        return None if s is None else "<60" if s < 60 else "60-65" if s < 65 else "65-70" if s < 70 else (
            "70-80" if s < 80 else ">=80")

    def hour(p):
        d = N.dt(p["entry_at"])
        return f"{d.astimezone(IST).hour:02d}:00" if d else None

    def hold(p):
        h = p["holding_seconds"]
        return None if h is None else "<5m" if h < 300 else "5-30m" if h < 1800 else "30m-2h" if h < 7200 else ">2h"

    sens = {}
    for b in (0, 10, 20):
        adj = D(0)
        for p in closed:
            inc = (p["charges"] or {}).get("incurred") or {"orders": 0, "items": {}}
            have = D(str(inc["items"].get("brokerage", 0)))
            adj += D(str(p["realized_net"])) + have * D("1.18") - D(str(inc["orders"])) * b * D("1.18")
        sens[f"brokerage_{b}_per_order"] = money(adj)
    rejects = Counter(o["rejection_reason"] for o in pf.orders if o["status"] == "REJECTED" and o["intent"] == "ENTRY"
                      and (not start and not end or _in_window(o["submitted_at"], start, end)))
    marks = [p for p in closed if p["mae"] is not None]
    return {
        "as_of": N.iso(pf.now), "window": {"from": str(start) if start else None, "to": str(end) if end else None},
        "mode": (filters.get("mode") or "ALL").upper(), "overall": _stats(closed) if closed else {"trades": 0},
        "excluded_trades_without_exit_price": excluded, "daily": series,
        "monthly": [{"month": m, "net_pnl": money(v)} for m, v in sorted(months.items())],
        "by_strategy": by_strategy,
        "by_direction": [{"direction": d, **_stats([p for p in closed if p["direction"] == d])} for d in
                         ("LONG", "SHORT", "BULL", "BEAR") if any(p["direction"] == d for p in closed)],
        "exit_reason_contribution": _bucket(closed, lambda p: p["exit_reason"], sorted({
            p["exit_reason"] for p in closed if p["exit_reason"]})),
        "entry_score_vs_outcome": _bucket(closed, score_b, ["<60", "60-65", "65-70", "70-80", ">=80"]),
        "time_of_day": _bucket(closed, hour, sorted({hour(p) for p in closed if hour(p)})),
        "holding_duration": _bucket(closed, hold, ["<5m", "5-30m", "30m-2h", ">2h"]),
        "excursions": {"trades_with_data": len(marks),
                       "avg_mae": money(_dec(p["mae"] for p in marks) / len(marks)) if marks else None,
                       "avg_mfe": money(_dec(p["mfe"] for p in marks) / len(marks)) if marks else None},
        "cost_sensitivity_net_pnl": sens,
        "opportunity_rejections": [{"reason": r or "UNSPECIFIED", "count": n} for r, n in rejects.most_common()],
        "unavailable": {"sharpe_sortino": "UNAVAILABLE: sample too short and no valid capital basis",
                        "market_regime": "UNAVAILABLE: regime is not recorded on NIFTY trade documents"},
        "comparison_note": "Absolute P&L across strategies is NOT risk-adjusted: position sizes differ. Compare "
                           "expectancy, drawdown and net-to-drawdown on their own sample periods.",
    }


# ---------------------------------------------------------------------------------------------------------- risk
def risk(pf: Portfolio, **filters: Any) -> dict[str, Any]:
    today = pf.now.astimezone(IST).date()
    pos = select_positions(pf, **filters)
    open_ = [p for p in pos if p["status"] in ("OPEN", "UNRESOLVED")]
    engines = []
    for eid in sorted({p["engine_id"] for p in pos}):
        mine = [p for p in pos if p["engine_id"] == eid]
        op = [p for p in mine if p["status"] in ("OPEN", "UNRESOLVED")]
        todays = [p for p in mine if p["status"] == "CLOSED" and _in_window(p["exit_at"], today, today)]
        realized_loss = min(0.0, float(_dec(p["realized_net"] for p in todays if p["realized_net"] is not None)))
        cur = pf.cursors.get("acts" if eid.startswith("nifty") else eid.split("-")[1].upper(), None) or {}
        limits = _limits(eid, pf.policies.get(eid), cur)
        daily_limit = limits.get("daily_loss_limit")
        open_risk = money(_dec(p["defined_max_loss"] for p in op if p["defined_max_loss"]))
        engines.append({
            "engine_id": eid, "label": mine[0]["strategy_label"], "instrument": mine[0]["instrument"],
            "open_positions": len(op), "open_position_risk": open_risk,
            "daily_realized_net": money(_dec(p["realized_net"] for p in todays if p["realized_net"] is not None)),
            "daily_realized_loss": money(D(str(realized_loss))), "daily_loss_limit": daily_limit,
            "remaining_daily_loss_budget": money(D(str(daily_limit)) + D(str(realized_loss)) - D(str(open_risk)))
            if daily_limit else None, "max_risk_per_trade": limits.get("max_risk_per_trade"),
            "entries_today": sum(1 for p in mine if p["entry_at"] and _in_window(p["entry_at"], today, today)),
            "max_entries_per_day": limits.get("max_entries"), "limits_source": limits.get("source"),
            "stop_coverage": {"with_stop": sum(1 for p in op if (p["stop"] or {}).get("level") is not None),
                              "open": len(op)}})
    notional = defaultdict(lambda: D(0))
    for p in open_:
        if p["structure"] == "FUTURES_OUTRIGHT" and p["entry_price"]:
            notional[(p["instrument"], p["direction"])] += D(str(p["entry_price"])) * p["quantity"]
    long_x = _dec(v for (i, d), v in notional.items() if d == "LONG")
    short_x = _dec(v for (i, d), v in notional.items() if d == "SHORT")
    total_risk = _dec(p["defined_max_loss"] for p in open_ if p["defined_max_loss"])
    return {
        "as_of": N.iso(pf.now), "mode": (filters.get("mode") or "ALL").upper(), "engines": engines,
        "exposure": {"defined_risk_total": money(total_risk), "futures_long_notional": money(long_x),
                     "futures_short_notional": money(short_x),
                     "notional_note": "Notional contract value, NOT margin. Margin is unavailable: broker margin "
                                      "requirements are not recorded and are never assumed.",
                     "margin_utilization": None, "capital_allocation": None,
                     "capital_note": "No paper capital is configured; utilisation is unavailable until it is."},
        "concentration": [{"engine_id": e["engine_id"], "share_of_open_risk": round(float(
            D(str(e["open_position_risk"])) / total_risk), 3) if total_risk else None} for e in engines],
        "unresolved_exposure": [{"position_id": p["position_id"], "label": p["strategy_label"],
                                 "contract": p["contract"], "flags": p["flags"] or [p["status"]],
                                 "defined_max_loss": p["defined_max_loss"]} for p in open_ if needs_attention(p)],
        "limitations": ["Portfolio-level limits are not enforced across engines; per-engine limits come from each "
                        "engine's deployed policy.", "Live risk is not shown: no live execution exists."],
    }


def _limits(engine_id: str, policy: dict[str, Any] | None, cursor: dict[str, Any]) -> dict[str, Any]:
    if engine_id.startswith("mcx"):
        r = (cursor.get("latest") or {}).get("risk") or {}
        return {"daily_loss_limit": r.get("daily_loss_limit"), "max_risk_per_trade": r.get("max_risk_per_trade"),
                "max_entries": r.get("max_entries"), "source": "engine status record (deployed config)"}
    cfg = (policy or {}).get("config") or {}
    if not cfg:
        return {"daily_loss_limit": None, "max_risk_per_trade": None, "max_entries": None,
                "source": "UNAVAILABLE: no stored policy on any trade"}
    return {"daily_loss_limit": N.num(cfg.get("hard_daily_loss")), "max_risk_per_trade": N.num(cfg.get(
        "max_loss_per_trade")), "max_entries": cfg.get("max_trades_per_day"),
        "source": f"effective policy stored on trade {str((policy or {}).get('from_trade'))[:10]} "
                  f"(hash {str((policy or {}).get('policy_hash'))[:10]})"}


# ------------------------------------------------------------------------------------------------------- equity
def equity(sessions: sessionmaker[Session], pf: Portfolio, day: date, **filters: Any) -> dict[str, Any]:
    """Event-sourced intraday P&L from recorded marks/entries/exits. No curve is fabricated: a minute with an
    unmarked open position is flagged partial instead of being filled."""
    pos = {p["trade_id"]: p for p in select_positions(pf, **filters)}
    start = datetime.combine(day, time.min, IST)
    end = start + timedelta(days=1)
    ev: list[tuple[datetime, int, str, str, float | None]] = []   # (time, order, kind, trade_id, value)
    try:
        with sessions() as session:
            q = (select(ActsPaperEvent.trade_id, ActsPaperEvent.event_type, ActsPaperEvent.sequence,
                        ActsPaperEvent.payload).join(ScalperMarketSnapshotRecord,
                                                     ScalperMarketSnapshotRecord.id == ActsPaperEvent.snapshot_id)
                 .where(ScalperMarketSnapshotRecord.captured_at >= start, ScalperMarketSnapshotRecord.captured_at < end,
                        ActsPaperEvent.trade_id.in_([t for t, p in pos.items() if p["source"] == "ACTS_PAPER"]),
                        ActsPaperEvent.event_type.in_(("ACTS_MARK", "ACTS_ENTRY", "ACTS_EXIT")))
                 .order_by(ActsPaperEvent.sequence))
            ids = [t for t, p in pos.items() if p["source"] == "ACTS_PAPER"]
            if ids:
                for tid, kind, seq, payload in session.execute(q):
                    p = N.loads(payload)
                    at = N.dt(p.get("market_timestamp"))
                    if at is None:
                        continue
                    if kind == "ACTS_MARK":
                        doc = p.get("trade_document") or {}
                        val = N.num(doc.get("gross_mark_rupees")) if p.get("available") else None
                        ev.append((at, seq, "MARK", tid, val))
                    else:
                        ev.append((at, seq, "ENTRY" if kind == "ACTS_ENTRY" else "EXIT", tid, None))
            mids = [t for t, p in pos.items() if p["source"] == "MCX_FUTURES_PAPER"]
            if mids:
                for tid, kind, seq, payload, at_s in session.execute(
                        select(McxFuturesEvent.trade_id, McxFuturesEvent.event_type, McxFuturesEvent.sequence,
                               McxFuturesEvent.payload, McxFuturesEvent.at).where(
                            McxFuturesEvent.trade_id.in_(mids), McxFuturesEvent.event_type.in_((
                                "MARK", "ENTRY_FILLED", "EXIT", "MARK_UNAVAILABLE")),
                            McxFuturesEvent.at >= start.isoformat(), McxFuturesEvent.at < end.isoformat()
                        ).order_by(McxFuturesEvent.sequence)):
                    at = N.dt(at_s)
                    p = (N.loads(payload).get("payload") or {})
                    if at is None:
                        continue
                    if kind == "MARK":
                        ev.append((at, seq, "MARK", tid, N.num(p.get("unrealised_gross"))))
                    elif kind == "MARK_UNAVAILABLE":
                        ev.append((at, seq, "MARK", tid, None))
                    else:
                        ev.append((at, seq, "ENTRY" if kind == "ENTRY_FILLED" else "EXIT", tid, None))
    except SQLAlchemyError:
        return {"date": str(day), "points": [], "markers": [], "available": False,
                "reason": "JOURNAL_QUERY_FAILED"}
    ev.sort(key=lambda e: (e[0], e[1]))
    realized_g = realized_n = D(0)
    open_t: dict[str, dict[str, Any]] = {}
    points: dict[str, dict[str, Any]] = {}
    markers = []
    for at, _, kind, tid, val in ev:
        p = pos[tid]
        if kind == "ENTRY":
            ch = ((p["charges"] or {}).get("entry") or {}).get("total", 0)
            open_t[tid] = {"unreal": None, "mark_at": None, "entry_charges": D(str(ch))}
            markers.append({"t": N.iso(at), "type": "ENTRY", "engine_id": p["engine_id"], "position_id": p[
                "position_id"], "direction": p["direction"], "price": p["entry_price"]})
        elif kind == "MARK" and tid in open_t:
            open_t[tid].update(unreal=val, mark_at=at)
        elif kind == "EXIT":
            open_t.pop(tid, None)
            if p["realized_net"] is not None:
                realized_g += D(str(p["realized_gross"]))
                realized_n += D(str(p["realized_net"]))
            markers.append({"t": N.iso(at), "type": "EXIT", "engine_id": p["engine_id"], "position_id": p[
                "position_id"], "direction": p["direction"], "price": p["exit_price"],
                "net_pnl": p["realized_net"]})
        unreal, partial = D(0), False
        for s in open_t.values():
            if s["unreal"] is None or s["mark_at"] is None or (at - s["mark_at"]).total_seconds() > 120:
                partial = True
            else:
                unreal += D(str(s["unreal"])) - s["entry_charges"]
        minute = at.astimezone(IST).replace(second=0, microsecond=0)
        points[minute.isoformat()] = {"t": minute.isoformat(), "realized_gross": money(realized_g),
                                      "realized_net": money(realized_n), "unrealized_gross": money(unreal),
                                      "combined_net": money(realized_n + unreal), "open_positions": len(open_t),
                                      "partial": partial}
    return {"date": str(day), "available": bool(points), "points": [points[k] for k in sorted(points)],
            "markers": markers, "source": "event-sourced from journal MARK/ENTRY/EXIT events; resolution 1 minute",
            "note": None if points else "No recorded marks, entries or exits for this date."}


# --------------------------------------------------------------------------------------------- position detail
def position_detail(sessions: sessionmaker[Session], pf: Portfolio, position_id: str) -> dict[str, Any] | None:
    pos = next((p for p in pf.positions if p["position_id"] == position_id), None)
    if pos is None:
        return None
    timeline: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    with sessions() as session:
        if pos["source"] == "ACTS_PAPER":
            for seq, kind, payload in session.execute(select(ActsPaperEvent.sequence, ActsPaperEvent.event_type,
                                                              ActsPaperEvent.payload).where(
                    ActsPaperEvent.trade_id == pos["trade_id"]).order_by(ActsPaperEvent.sequence)):
                p = N.loads(payload)
                at = p.get("market_timestamp")
                if kind == "ACTS_MARK":
                    doc = p.get("trade_document") or {}
                    marks.append({"t": at, "price": N.num(doc.get("current_debit")) if p.get("available") else None,
                                  "unrealized": N.num(doc.get("gross_mark_rupees")) if p.get("available") else None})
                else:
                    timeline.append({"sequence": seq, "type": kind, "at": at, "reason": p.get("reason"),
                                     "state": p.get("state")})
        else:
            for seq, kind, at, payload in session.execute(select(
                    McxFuturesEvent.sequence, McxFuturesEvent.event_type, McxFuturesEvent.at,
                    McxFuturesEvent.payload).where(McxFuturesEvent.trade_id == pos["trade_id"]).order_by(
                    McxFuturesEvent.sequence)):
                p = N.loads(payload).get("payload") or {}
                if kind == "MARK":
                    marks.append({"t": at, "price": N.num(p.get("price")), "unrealized": N.num(p.get("unrealised_gross"))})
                elif kind == "MARK_UNAVAILABLE":
                    marks.append({"t": at, "price": None, "unrealized": None})
                else:
                    timeline.append({"sequence": seq, "type": kind, "at": at, "reason": p.get("reason") or (
                        (p.get("trade") or {}).get("exit_reason")), "state": None})
    step = max(1, len(marks) // 400)
    orders = [o for o in pf.orders if o["position_id"] == position_id]
    return {"position": pos, "orders": orders, "fills": [f for f in pf.fills if f["position_id"] == position_id],
            "timeline": timeline, "marks": marks[::step], "mark_count": len(marks),
            "entry_evidence": {"score": pos["score"], "score_band": pos["score_band"],
                               "score_components": pos["score_components"], "cycle_id": pos["cycle_id"],
                               "rearm_reason": pos["rearm_reason"], "entry_reason": pos["entry_reason"]},
            "pnl_calculation": _pnl_explain(pos)}


def _pnl_explain(p: dict[str, Any]) -> dict[str, Any]:
    if p["structure"] == "CREDIT_SPREAD":
        formula = "gross = (short_entry - hedge_entry - short_exit + hedge_exit) x quantity"
    else:
        formula = "gross = (exit - entry) x (+1 LONG / -1 SHORT) x quantity"
    return {"formula": formula, "quantity": p["quantity"], "realized_gross": p["realized_gross"],
            "engine_recorded_gross": p["realized_gross_engine"], "charges": p["charges"],
            "realized_net": p["realized_net"], "engine_recorded_net": p.get("engine_recorded_net"),
            "charge_status": p["provenance"]["charges_status"]}


# ------------------------------------------------------------------------------------------------ strategy cards
def strategies(pf: Portfolio, engine_rows: list[dict[str, Any]], mcx_status: dict[str, Any] | None) -> list[dict[str, Any]]:
    by_id = {r["id"]: r for r in engine_rows}
    mcx = {r["instrument"]: r for r in (mcx_status or {}).get("instruments", [])}
    out = []
    for key, eng in N.ENGINES.items():
        eid = eng["engine_id"]
        mine = [p for p in pf.positions if p["engine_id"] == eid]
        if eng["version"] == "ACTS_V2" and not mine:
            continue
        s = summary(pf, engine=eid)
        row = by_id.get(eid, {})
        card: dict[str, Any] = {
            "engine_id": eid, "label": eng["label"], "family": eng["family"], "instrument": eng["instrument"],
            "version": eng["version"], "mode": "PAPER", "retired": eng["version"] == "ACTS_V2",
            "operational_state": row.get("state", "UNKNOWN"), "state_reason": row.get("state_reason"),
            "last_update": row.get("last_update"), "today": {
                "trades": s["counts"]["closed_trades"], "open_positions": s["counts"]["open_positions"],
                "realized_net": s["pnl"]["realized_net"], "unrealized_gross": s["pnl"]["unrealized_gross"],
                "total_net": s["pnl"]["total_net_pnl"], "partial": s["pnl"]["partial"]},
            "unresolved": sum(1 for p in mine if needs_attention(p) or p["status"] == "UNRESOLVED")}
        if eng["family"] == "MCX_FUTURES":
            m = mcx.get(eng["instrument"], {})
            card["dimensions"] = {
                "enablement": {"paper_execution": "ENABLED" if m.get("execution_state") == "PAPER_ENABLED" else (
                    "COLLECT_ONLY" if m.get("execution_state") == "COLLECT_ONLY" else "UNKNOWN"),
                    "live_execution": "NONE (no live execution path exists)"},
                "session": m.get("session"), "feed": m.get("feed"), "readiness": m.get("warmup"),
                "signal": m.get("last_decision"), "position": {"open": s["counts"]["open_positions"]},
                "risk": m.get("risk"), "contract": m.get("contract"), "calibration": "PRE_REGISTERED_UNVALIDATED"}
        else:
            cur = pf.cursors.get("acts") or {}
            latest = cur.get("latest") or {}
            card["dimensions"] = {
                "enablement": {"paper_execution": "OBSERVED_ACTIVE" if mine else "NO_RECORDS",
                               "live_execution": "NONE (no live execution path exists)"},
                "session": {"status": "OPEN" if row.get("state") == "RUNNING" else "CLOSED_OR_UNKNOWN"},
                "feed": {"last_observation": cur.get("observed_at"), "state": row.get("state")},
                "readiness": None,
                "signal": {"status": latest.get("status"), "direction": latest.get("direction"),
                           "score": latest.get("opportunity_score"), "score_band": latest.get("score_band"),
                           "components": latest.get("score_components"), "blockers": latest.get("hard_blockers"),
                           "market_state": latest.get("market_state"), "cycle_id": latest.get("cycle_id"),
                           "cycle_state": latest.get("cycle_state"), "primary_blocker": latest.get("primary_blocker")},
                "position": {"open": s["counts"]["open_positions"]},
                "risk": _limits(eid, pf.policies.get(eid), cur)}
        out.append(card)
    return out


def strategy_config(pf: Portfolio, engine_id: str) -> dict[str, Any]:
    """EFFECTIVE deployed parameters: ACTS from the policy stored on the latest trade; MCX from the deployed
    config object whose hash is compared with the hash the running worker persisted."""
    if engine_id.startswith("nifty"):
        pol = pf.policies.get(engine_id)
        if not pol:
            return {"available": False, "source": "none", "note": "No trade carries a stored policy."}
        cfg = pol.get("config") or {}
        keys = ("signal_min_score", "min_credit", "min_credit_to_width", "min_short_distance_points",
                "max_bid_ask_spread_pct", "max_quote_age_seconds", "min_open_interest", "min_volume", "allowed_widths",
                "stop_credit_multiple", "max_loss_per_trade", "hard_daily_loss", "max_consecutive_losses",
                "max_trades_per_day", "cooldown_after_exit_seconds", "cooldown_after_loss_seconds", "start_time",
                "entry_end_time", "forced_exit_time", "entry_ttl_seconds", "lots", "interval_seconds")
        return {"available": True, "source": "policy stored on the latest trade (what actually ran)",
                "policy_hash": pol["policy_hash"], "from_trade": pol["from_trade"], "decided_at": pol["decided_at"],
                "effective": {k: cfg.get(k) for k in keys}, "parameters": pol.get("parameters"),
                "exit_policy": pol.get("exit_policy"), "entry_policy": pol.get("entry_policy"),
                "opportunity_cycle_policy": pol.get("opportunity_cycle_policy"),
                "note": "Profit capture, trailing and the 30-minute time stop are DIAGNOSTICS_ONLY in V3's "
                        "MIRROR_EXIT_V1; exits are FORCED_CLOSE 15:20, SPREAD_STOP, HARD_MAX_LOSS and the opposite "
                        "ACTS V2 entry signal."}
    inst = engine_id.split("-")[1].upper()
    try:
        from app.mcx_futures.config import CONFIGS
        cfg = CONFIGS[inst]
        cur = pf.cursors.get(inst) or {}
        return {"available": True, "source": "deployed code config (app.mcx_futures.config)", "config_id": cfg.config_id,
                "persisted_config_hash": cur.get("config_hash"),
                "matches_running_worker": bool(cur.get("config_hash")) and cur.get("config_hash") == cfg.config_hash,
                "calibration_status": cfg.calibration_status, "parameters": json.loads(json.dumps(
                    asdict(cfg), default=str))}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": type(exc).__name__}
