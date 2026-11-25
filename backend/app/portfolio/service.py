"""Portfolio read service: loads normalised positions/orders/fills from the strategy journals and computes the
authoritative summary, lists and alerts. READ ONLY (PostgreSQL sessions use SET TRANSACTION READ ONLY).

P&L DEFINITIONS (one set, used by every page; the frontend never recomputes money):
  realized_gross        closed positions in the window: sum((short_entry - hedge_entry - short_exit + hedge_exit) x qty)
                        for spreads, (exit - entry) x sign x qty for futures, recomputed from the recorded fills and
                        reconciled against the engine's own record (discrepancies raise an alert).
  charges (incurred)    entry-side charges of positions entered in the window + exit-side charges of positions exited
                        in the window (app.portfolio.charges; brokerage is a configured scenario, never assumed).
  realized_net          realized_gross - charges for those closed positions.
  unrealized_gross      open positions with a FRESH executable mark only (<= 60 s old, no expiry/forced-exit flag).
                        Stale / unavailable marks are EXCLUDED and counted - never silently treated as zero.
  total_net_pnl         realized_net + unrealized_gross - entry charges already incurred on open positions.
  net_liquidation_pnl   total_net_pnl - estimated exit charges of open positions (a mark-based estimate).
Any total that excludes open/unresolved exposure is flagged `partial` with the excluded count.
"""
from __future__ import annotations

import csv
import io
import time as _time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.mcx_futures.models import McxFuturesCursor, McxFuturesTrade
from app.portfolio import normalize as N
from app.portfolio.charges import ChargeModel, add, money

IST = ZoneInfo("Asia/Kolkata")
D = Decimal
_CACHE: dict[int, tuple[float, "Portfolio"]] = {}
CACHE_SECONDS = 8.0


class Portfolio:
    """One consistent in-memory snapshot of all normalised records."""

    def __init__(self, positions: list[dict[str, Any]], orders: list[dict[str, Any]], sources: list[dict[str, Any]],
                 cursors: dict[str, Any], now: datetime, charges: ChargeModel):
        self.positions, self.orders, self.sources, self.cursors, self.now, self.charges = (
            positions, orders, sources, cursors, now, charges)
        self.policies: dict[str, dict[str, Any]] = {}
        self.fills = N.fills(orders)


def _read_only(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION READ ONLY"))


def load(sessions: sessionmaker[Session], now: datetime | None = None, charges: ChargeModel | None = None,
         use_cache: bool = True) -> Portfolio:
    cacheable = now is None and use_cache
    if cacheable:
        hit = _CACHE.get(id(sessions))
        if hit and _time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    now = now or datetime.now(timezone.utc)
    charges = charges or ChargeModel.from_env()
    positions: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    cursors: dict[str, Any] = {}
    policies: dict[str, tuple[str, dict[str, Any]]] = {}
    with sessions() as session, session.no_autoflush:
        _read_only(session)
        # ---------------------------------------------------------------- NIFTY ACTS
        try:
            rows = session.scalars(select(ActsPaperTrade)).all()
            ids = [r.id for r in rows]
            refs = {}
            if ids:
                for tid, first, last, n in session.execute(
                        select(ActsPaperEvent.trade_id, func.min(ActsPaperEvent.sequence),
                               func.max(ActsPaperEvent.sequence), func.count()).where(
                            ActsPaperEvent.trade_id.in_(ids)).group_by(ActsPaperEvent.trade_id)):
                    refs[tid] = {"first_sequence": first, "last_sequence": last, "events": n}
            active_ids = [r.id for r in rows if r.state in ("OPEN", "UNRESOLVED_EXPOSURE")]
            marks = _latest_acts_marks(session, active_ids)
            rejected_ids = [r.id for r in rows if r.state == "ENTRY_REJECTED"]
            reject_ts = {}
            if rejected_ids:
                for tid, payload in session.execute(select(ActsPaperEvent.trade_id, ActsPaperEvent.payload).where(
                        ActsPaperEvent.trade_id.in_(rejected_ids), ActsPaperEvent.event_type == "ACTS_ENTRY_REJECTED")):
                    reject_ts[tid] = {"ACTS_ENTRY_REJECTED": N.loads(payload).get("market_timestamp")}
            for row in rows:
                doc = N.loads(row.document)
                if doc.get("strategy_version") not in ("ACTS_V3", "ACTS_V2"):
                    continue
                pos = N.acts_position(row, doc, now, charges, marks.get(row.id), refs.get(row.id))
                positions.append(pos)
                pol = doc.get("policy")
                when = doc.get("decision_timestamp") or ""
                if isinstance(pol, dict) and (pos["engine_id"] not in policies or when > policies[pos["engine_id"]][0]):
                    policies[pos["engine_id"]] = (when, {
                        "policy_hash": doc.get("policy_hash"), "from_trade": row.id, "decided_at": when,
                        "config": pol.get("config"), "parameters": pol.get("parameters"),
                        "exit_policy": pol.get("exit_policy"), "entry_policy": pol.get("entry_policy"),
                        "opportunity_cycle_policy": pol.get("opportunity_cycle_policy"),
                        "exit_policy_version": pol.get("exit_policy_version")})
                orders.extend(N.acts_orders(pos, doc, reject_ts.get(row.id, {})))
            cur = session.get(ActsPaperCursor, 1)
            cursors["acts"] = None if cur is None else {"observed_at": cur.observed_at, "sequence": cur.sequence,
                                                         "latest": N.loads(cur.latest_json)}
            sources.append({"source": "acts_paper", "status": "OK", "records": len(rows),
                            "note": "NIFTY ACTS V3/V2 paper journal (gross-only engine record; charges applied here)"})
        except SQLAlchemyError as exc:
            sources.append({"source": "acts_paper", "status": "UNAVAILABLE", "records": 0, "note": type(exc).__name__})
        # ---------------------------------------------------------------- MCX futures
        try:
            cfgs = {}
            for r in session.scalars(select(McxFuturesTrade)).all():
                pos = N.mcx_position(r, N.loads(r.document), now, charges, cfgs.get(r.instrument))
                positions.append(pos)
                orders.extend(N.mcx_orders(pos, N.loads(r.document)))
            for c in session.scalars(select(McxFuturesCursor)).all():
                cursors[c.instrument] = {"observed_at": c.observed_at, "sequence": c.sequence,
                                         "config_hash": c.config_hash, "latest": N.loads(c.latest_json),
                                         "contract": N.loads(c.contract_json)}
            sources.append({"source": "mcx_futures", "status": "OK",
                            "records": sum(1 for p in positions if p["source"] == "MCX_FUTURES_PAPER"),
                            "note": "ACTS V3 MCX futures paper journal"})
        except SQLAlchemyError as exc:
            session.rollback()
            sources.append({"source": "mcx_futures", "status": "NOT_CONFIGURED", "records": 0,
                            "note": "MCX_MIGRATION_NOT_APPLIED" if "no such table" in str(exc).lower() or
                            "does not exist" in str(exc).lower() else type(exc).__name__})
    sources.append({"source": "ast_futures", "status": "NOT_CONFIGURED", "records": 0,
                    "note": "AST runs in an isolated stack; its read-only API is not reachable from this deployment"})
    sources.append({"source": "commodity_options_legacy", "status": "NOT_NORMALISED", "records": 0,
                    "note": "Legacy Commodity ACTS options engine has no deployed worker; its pages remain available"})
    positions.sort(key=lambda p: (p["entry_at"] or p["decided_at"] or ""), reverse=True)
    orders.sort(key=lambda o: (o["submitted_at"] or ""), reverse=True)
    result = Portfolio(positions, orders, sources, cursors, now, charges)
    result.policies = {k: v[1] for k, v in policies.items()}
    if cacheable:
        _CACHE[id(sessions)] = (_time.monotonic(), result)
    return result


def _latest_acts_marks(session: Session, trade_ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tid in trade_ids:
        row = session.execute(select(ActsPaperEvent.payload).where(
            ActsPaperEvent.trade_id == tid, ActsPaperEvent.event_type == "ACTS_MARK").order_by(
            ActsPaperEvent.sequence.desc()).limit(1)).first()
        if not row:
            continue
        p = N.loads(row[0])
        q = p.get("quotes") or {}
        out[tid] = {"at": p.get("market_timestamp"), "available": p.get("available") is True,
                    "reason": p.get("reason"), "short_px": ((q.get("short") or {}).get("fill_price")),
                    "long_px": ((q.get("long") or {}).get("fill_price"))}
    return out


# ------------------------------------------------------------------------------------------------------ filtering
def _in_window(ts: str | None, start: date | None, end: date | None) -> bool:
    d = N.dt(ts)
    if d is None:
        return False
    local = d.astimezone(IST).date()
    return (start is None or local >= start) and (end is None or local <= end)


def select_positions(pf: Portfolio, *, strategy: str | None = None, engine: str | None = None,
                     instrument: str | None = None, mode: str | None = None, version: str | None = None,
                     direction: str | None = None) -> list[dict[str, Any]]:
    out = []
    for p in pf.positions:
        if strategy and strategy not in (p["strategy_id"], p["engine_id"]):
            continue
        if engine and p["engine_id"] != engine:
            continue
        if instrument and p["instrument"] != instrument:
            continue
        if mode and p["mode"] != mode.upper():
            continue
        if version and p["strategy_version"] != version:
            continue
        if direction and (p["direction"] or "").upper() != direction.upper():
            continue
        out.append(p)
    return out


def needs_attention(p: dict[str, Any]) -> bool:
    return p["status"] in ("OPEN", "UNRESOLVED", "EXPIRED_UNPRICED") and (bool(p["flags"]) or p["status"] == "UNRESOLVED")


# --------------------------------------------------------------------------------------------------------- summary
def summary(pf: Portfolio, *, start: date | None = None, end: date | None = None, **filters: Any) -> dict[str, Any]:
    pos = select_positions(pf, **filters)
    today = pf.now.astimezone(IST).date()
    start = start or today
    end = end or start
    closed = [p for p in pos if p["status"] == "CLOSED" and _in_window(p["exit_at"], start, end)]
    entered = [p for p in pos if p["entry_at"] and _in_window(p["entry_at"], start, end)]
    open_ = [p for p in pos if p["status"] in ("OPEN", "UNRESOLVED")]
    rg = sum((D(str(p["realized_gross"])) for p in closed if p["realized_gross"] is not None), D(0))
    rn = sum((D(str(p["realized_net"])) for p in closed if p["realized_net"] is not None), D(0))
    unpriced_closed = [p for p in closed if p["realized_gross"] is None]
    # Expired positions the worker terminalised WITHOUT a price. Their P&L is unknown, so they are excluded from every
    # total, but they stay visible in EVERY date window (not only the window of their entry) until reconciled.
    expired_unpriced = [p for p in pos if p["status"] == "EXPIRED_UNPRICED"]
    unpriced = unpriced_closed + expired_unpriced
    inc = None
    for p in entered:
        inc = add(inc, (p["charges"] or {}).get("entry"))
    for p in closed:
        inc = add(inc, (p["charges"] or {}).get("exit"))
    on_closed = None  # charges attributable to the closed trades counted in realized_net (entry + exit)
    for p in closed:
        on_closed = add(add(on_closed, (p["charges"] or {}).get("entry")), (p["charges"] or {}).get("exit"))
    fresh = [p for p in open_ if p["unrealized_gross"] is not None]
    stale = [p for p in open_ if p["unrealized_gross"] is None]
    unreal = sum((D(str(p["unrealized_gross"])) for p in fresh), D(0))
    open_entry = sum((D(str(((p["charges"] or {}).get("entry") or {}).get("total", 0))) for p in fresh), D(0))
    est_exit = sum((D(str(p["est_exit_charges"])) for p in fresh if p["est_exit_charges"] is not None), D(0))
    total_net = rn + unreal - open_entry
    unresolved = [p for p in open_ if needs_attention(p)] + expired_unpriced
    orders = [o for o in pf.orders if (not filters.get("engine") or o["engine_id"] == filters["engine"])
              and (not filters.get("instrument") or o["instrument"] == filters["instrument"])
              and (not filters.get("strategy") or filters["strategy"] in (o["strategy_id"], o["engine_id"]))]
    in_win = [o for o in orders if _in_window(o["submitted_at"], start, end)]
    wins = [p for p in closed if p["realized_net"] is not None and p["realized_net"] > 0]
    return {
        "as_of": N.iso(pf.now), "window": {"from": str(start), "to": str(end), "timezone": "Asia/Kolkata"},
        "mode": (filters.get("mode") or "ALL").upper(), "currency": "INR",
        "pnl": {
            "realized_gross": money(rg), "realized_net": money(rn), "unrealized_gross": money(unreal),
            "incurred_charges": (inc or {"total": 0.0})["total"], "incurred_charges_detail": inc,
            "estimated_exit_charges": money(est_exit), "total_net_pnl": money(total_net),
            "net_liquidation_pnl": money(total_net - est_exit),
            "charges_on_closed_trades": (on_closed or {"total": 0.0})["total"],
            "charges_not_in_closed_trades": money(D(str((inc or {"total": 0.0})["total"])) - D(
                str((on_closed or {"total": 0.0})["total"]))),
            "incurred_charges_scope": ("Entry charges of positions ENTERED in the window plus exit charges of positions "
                                       "CLOSED in the window. Analytics 'charges' = gross - net of closed priced trades "
                                       "only (their entry + exit). The difference is charges_not_in_closed_trades: entry "
                                       "charges of positions still open / unpriced, less entry charges of closed trades "
                                       "entered before the window."),
            "partial": bool(stale or unpriced), "excluded_open_positions": len(stale),
            "excluded_unpriced_closed": len(unpriced_closed),
            "excluded_unpriced_exposure": len(expired_unpriced),
            "note": ("PARTIAL: %d open position(s) without a fresh executable mark, %d closed position(s) without an "
                     "exit price and %d expired position(s) terminalised without a settlement price are excluded from "
                     "these totals (their P&L is unknown, not zero)." % (len(stale), len(unpriced_closed),
                                                                        len(expired_unpriced))) if (
                stale or unpriced) else None},
        "counts": {"open_positions": len(open_), "unresolved_positions": len(unresolved),
                   "expired_unpriced": len(expired_unpriced),
                   "executed_trades": len(entered), "closed_trades": len(closed), "winning_trades": len(wins),
                   "pending_orders": sum(1 for o in orders if o["status"] in ("PENDING", "PARTIALLY_FILLED")),
                   "rejected_orders": sum(1 for o in in_win if o["status"] == "REJECTED"),
                   "orders_in_window": len(in_win)},
        "charge_model": {"brokerage_per_order": float(pf.charges.brokerage_per_order), "status": pf.charges.status()},
        "attention": [{"position_id": p["position_id"], "engine_id": p["engine_id"], "label": p["strategy_label"],
                       "contract": p["contract"], "flags": p["flags"] or [p["status"]]} for p in unresolved],
    }


def _page(items: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    return {"total": len(items), "limit": limit, "offset": offset, "items": items[offset: offset + limit]}


def positions_list(pf: Portfolio, *, tab: str = "open", limit: int = 50, offset: int = 0, start: date | None = None,
                   end: date | None = None, **filters: Any) -> dict[str, Any]:
    pos = [p for p in select_positions(pf, **filters) if p["status"] not in ("PENDING", "REJECTED")]
    tab = tab.lower()
    if tab == "open":
        pos = [p for p in pos if p["status"] in ("OPEN",)]
    elif tab == "unresolved":
        pos = [p for p in pos if needs_attention(p) or p["status"] in ("UNRESOLVED", "EXPIRED_UNPRICED")]
    elif tab == "closed":
        pos = [p for p in pos if p["status"] in ("CLOSED", "EXPIRED_UNPRICED")]
        if start or end:
            pos = [p for p in pos if _in_window(p["exit_at"] or p["entry_at"], start, end)]
    # unresolved first, then newest
    pos.sort(key=lambda p: (not needs_attention(p), -(N.dt(p["entry_at"] or p["decided_at"]) or
                                                     datetime.min.replace(tzinfo=IST)).timestamp()))
    counts = {"open": sum(1 for p in pf.positions if p["status"] == "OPEN"),
              "unresolved": sum(1 for p in pf.positions if needs_attention(p) or p["status"] in (
                  "UNRESOLVED", "EXPIRED_UNPRICED")),
              "closed": sum(1 for p in pf.positions if p["status"] in ("CLOSED", "EXPIRED_UNPRICED")),
              "closed_priced": sum(1 for p in pf.positions if p["status"] == "CLOSED"),
              "expired_unpriced": sum(1 for p in pf.positions if p["status"] == "EXPIRED_UNPRICED")}
    return {"tab": tab, "tab_counts": counts, **_page(pos, limit, offset)}


def orders_list(pf: Portfolio, *, status: str | None = None, intent: str | None = None, limit: int = 50,
                offset: int = 0, start: date | None = None, end: date | None = None, **filters: Any) -> dict[str, Any]:
    allowed = {p["position_id"] for p in select_positions(pf, **filters)}
    items = [o for o in pf.orders if o["position_id"] in allowed]
    if status:
        items = [o for o in items if o["status"] == status.upper()]
    if intent:
        items = [o for o in items if o["intent"] == intent.upper()]
    if start or end:
        items = [o for o in items if _in_window(o["submitted_at"], start, end)]
    by = {}
    for o in items:
        by[o["status"]] = by.get(o["status"], 0) + 1
    return {"status_counts": by, "note": "Simulated paper orders reconstructed from journal events; broker order ids "
            "are never invented.", **_page(items, limit, offset)}


TRADE_COLUMNS = ["trade_id", "engine", "strategy", "version", "instrument", "contract", "direction", "mode",
                 "entry_at", "entry_price", "exit_at", "exit_price", "holding_seconds", "lots", "quantity",
                 "gross_pnl", "charges", "net_pnl", "entry_reason", "exit_reason", "entry_score", "cycle_id"]


def trade_rows(pf: Portfolio, *, start: date | None = None, end: date | None = None, result: str | None = None,
               exit_reason: str | None = None, **filters: Any) -> list[dict[str, Any]]:
    out = []
    for p in select_positions(pf, **filters):
        if p["status"] != "CLOSED":
            continue
        if (start or end) and not _in_window(p["exit_at"], start, end):
            continue
        net = p["realized_net"]
        if result == "win" and not (net is not None and net > 0):
            continue
        if result == "loss" and not (net is not None and net <= 0):
            continue
        if exit_reason and p["exit_reason"] != exit_reason:
            continue
        out.append({"trade_id": p["trade_id"], "position_id": p["position_id"], "engine": p["engine_id"],
                    "strategy": p["strategy_label"], "version": p["strategy_version"], "instrument": p["instrument"],
                    "contract": p["contract"], "direction": p["direction"], "mode": p["mode"],
                    "entry_at": p["entry_at"], "entry_price": p["entry_price"], "exit_at": p["exit_at"],
                    "exit_price": p["exit_price"], "holding_seconds": p["holding_seconds"], "lots": p["lots"],
                    "quantity": p["quantity"], "gross_pnl": p["realized_gross"],
                    "charges": ((p["charges"] or {}).get("incurred") or {}).get("total"), "net_pnl": net,
                    "entry_reason": p["entry_reason"], "exit_reason": p["exit_reason"], "entry_score": p["score"],
                    "cycle_id": p["cycle_id"], "market_regime": None,
                    "return_on_defined_risk_pct": round(100 * net / p["defined_max_loss"], 2)
                    if net is not None and p["defined_max_loss"] else None})
    out.sort(key=lambda r: r["exit_at"] or "", reverse=True)
    return out


def trades_page(pf: Portfolio, *, sort: str = "exit_at", desc: bool = True, limit: int = 50, offset: int = 0,
                **kw: Any) -> dict[str, Any]:
    rows = trade_rows(pf, **kw)
    if rows and sort in rows[0]:
        rows.sort(key=lambda r: (r[sort] is None, r[sort]), reverse=desc)
    totals = {"trades": len(rows), "gross_pnl": money(sum((D(str(r["gross_pnl"] or 0)) for r in rows), D(0))),
              "charges": money(sum((D(str(r["charges"] or 0)) for r in rows), D(0))),
              "net_pnl": money(sum((D(str(r["net_pnl"] or 0)) for r in rows), D(0)))}
    return {"totals": totals, **_page(rows, limit, offset)}


def trades_csv(pf: Portfolio, **kw: Any) -> str:
    rows = trade_rows(pf, **kw)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TRADE_COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------------------------------------------------- alerts
def alerts(pf: Portfolio, engine_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in pf.positions:
        base = {"engine_id": p["engine_id"], "engine": p["strategy_label"], "position_id": p["position_id"]}
        if p["status"] == "EXPIRED_UNPRICED":
            out.append({**base, "severity": "CRITICAL", "code": "EXPIRED_POSITION_UNPRICED",
                        "message": f"{p['contract']} expired {p['expiry']} and was terminalised without a settlement "
                                   "price. Its P&L is UNKNOWN and excluded from totals (not zero) until it is "
                                   "reconciled from an official settlement price.",
                        "since": p["expiry"], "action": "Do not edit the journal. Reconcile from the official NSE "
                        "settlement price; totals stay marked PARTIAL until then."})
            continue
        if p["status"] not in ("OPEN", "UNRESOLVED"):
            continue
        if "EXPIRED_NOT_SETTLED" in p["flags"]:
            out.append({**base, "severity": "CRITICAL", "code": "EXPIRED_POSITION_NOT_SETTLED",
                        "message": f"{p['contract']} expired {p['expiry']} but the position is still {p['status']} "
                                   "with no recorded exit. It is excluded from totals, not treated as zero.",
                        "since": p["expiry"], "action": "Do not edit the journal. Restart the worker with valid "
                        "credentials so it terminalises the position, or reconcile from an official NSE settlement "
                        "price."})
        elif "PAST_FORCED_EXIT" in p["flags"]:
            out.append({**base, "severity": "CRITICAL", "code": "FORCED_EXIT_MISSED",
                        "message": f"{p['contract']} is still open past the 15:20 forced-exit time.",
                        "since": p["entry_at"], "action": "Check the worker and market-data feed."})
        elif p["status"] == "UNRESOLVED":
            out.append({**base, "severity": "CRITICAL", "code": "UNRESOLVED_EXPOSURE",
                        "message": p.get("unresolved_reason") or "Exposure could not be priced.",
                        "since": p["entry_at"], "action": "Resolve before trusting totals."})
        elif p["mark"]["basis"] != "EXECUTABLE_BID_ASK":
            out.append({**base, "severity": "WARNING", "code": "OPEN_POSITION_WITHOUT_FRESH_MARK",
                        "message": f"No fresh executable mark ({p['mark']['reason']}). Unrealized P&L excludes it.",
                        "since": p["mark"]["at"], "action": "Check the market-data feed."})
    for p in pf.positions:
        eng, calc = p.get("realized_gross_engine"), p.get("realized_gross")
        if p["status"] == "CLOSED" and eng is not None and calc is not None and abs(eng - calc) > 0.05:
            out.append({"severity": "WARNING", "code": "RECONCILIATION_DISCREPANCY", "engine_id": p["engine_id"],
                        "engine": p["strategy_label"], "position_id": p["position_id"],
                        "message": f"Recomputed gross {calc} differs from engine record {eng}.",
                        "since": p["exit_at"], "action": "Inspect the trade's fills."})
    for row in engine_rows or []:
        base = {"engine_id": row["id"], "engine": row["label"]}
        if row["state"] == "DATA_STALE":
            out.append({**base, "severity": "WARNING", "code": "FEED_STALE", "message": row.get("state_reason") or
                        "No fresh observation during the session.", "since": row.get("last_update"),
                        "action": "Check the worker, broker session and API credentials."})
        elif row["state"] == "ERROR":
            out.append({**base, "severity": "CRITICAL", "code": "ENGINE_ERROR", "message": row.get("state_reason") or
                        "Engine reports an error.", "since": row.get("last_update"), "action": "Inspect worker logs."})
    order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    out.sort(key=lambda a: order[a["severity"]])
    return out
