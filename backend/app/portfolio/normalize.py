"""Normalise the strategy-specific paper journals into ONE read model: positions (parent + legs), simulated orders,
fills. Pure functions over stored documents; nothing here writes, replays an engine or touches a broker.

Authoritative sources (never recomputed by the frontend):
  NIFTY ACTS V3/V2   acts_paper_trades.document (+ acts_paper_events for marks / journal references)
  ACTS V3 MCX futures mcx_futures_trades.document (projection of the hash-chained mcx_futures_events journal)
Orders are SIMULATED paper orders reconstructed from journal lifecycle fields; broker order ids are always null.
Money is Decimal-computed and serialised as 2-dp numbers (4-dp for prices).
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
import json
from typing import Any
from zoneinfo import ZoneInfo

from app.portfolio.charges import ChargeModel, add, money

IST = ZoneInfo("Asia/Kolkata")
D = Decimal
MARK_FRESH_SECONDS = 60
NIFTY_FORCED_EXIT = time(15, 20)
NIFTY_CLOSE = time(15, 30)

ENGINES = {
    "ACTS_V3": {"engine_id": "nifty-acts_v3", "label": "NIFTY ACTS V3", "family": "ACTS", "instrument": "NIFTY",
                "structure": "CREDIT_SPREAD", "version": "ACTS_V3"},
    "ACTS_V2": {"engine_id": "nifty-acts_v2", "label": "NIFTY ACTS V2 (retired)", "family": "ACTS",
                "instrument": "NIFTY", "structure": "CREDIT_SPREAD", "version": "ACTS_V2"},
    "CRUDEOILM": {"engine_id": "mcx-crudeoilm", "label": "ACTS V3 CRUDEOILM Futures", "family": "MCX_FUTURES",
                  "instrument": "CRUDEOILM", "structure": "FUTURES_OUTRIGHT", "version": "ACTS_V3_MCX_FUTURES"},
    "NATGASMINI": {"engine_id": "mcx-natgasmini", "label": "ACTS V3 NATGASMINI Futures", "family": "MCX_FUTURES",
                   "instrument": "NATGASMINI", "structure": "FUTURES_OUTRIGHT", "version": "ACTS_V3_MCX_FUTURES"},
}


def dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(IST).isoformat() if value else None


def num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        return out if out == out and abs(out) != float("inf") else None
    except (TypeError, ValueError):
        return None


def px(value: Any) -> float | None:
    n = num(value)
    return None if n is None else round(n, 4)


def _fill(q: Any) -> float | None:
    return px((q or {}).get("fill_price")) if isinstance(q, dict) else None


def _bidask(q: Any) -> dict[str, float | None]:
    quote = (q or {}).get("quote") if isinstance(q, dict) else None
    quote = quote or {}
    return {"bid": px(quote.get("bid")), "ask": px(quote.get("ask")), "bid_qty": num(quote.get("bid_quantity")),
            "ask_qty": num(quote.get("ask_quantity"))}


# ------------------------------------------------------------------------------------------------------ NIFTY ACTS
def acts_position(row: Any, doc: dict[str, Any], now: datetime, charges: ChargeModel, mark: dict[str, Any] | None,
                  refs: dict[str, Any] | None) -> dict[str, Any]:
    version = doc.get("strategy_version") or "ACTS_V3"
    eng = ENGINES.get(version, ENGINES["ACTS_V3"])
    cand = doc.get("candidate") or {}
    short_leg, long_leg = cand.get("short_leg") or {}, cand.get("long_leg") or {}
    lot = int(num(doc.get("lot_size")) or 0)
    lots = int(num(doc.get("lots")) or 1)
    qty = lot * lots
    width = num(cand.get("spread_width")) or abs((num(short_leg.get("strike")) or 0) - (num(long_leg.get("strike")) or 0))
    entry_at, exit_at, decided_at = dt(doc.get("entry_timestamp")), dt(doc.get("exit_timestamp")), dt(
        doc.get("decision_timestamp"))
    expiry = date.fromisoformat(str(short_leg["expiry"])) if short_leg.get("expiry") else None
    unpriced = doc.get("expiry_terminalization") is True and doc.get("final_outcome") is None
    es, el = _fill((doc.get("entry_quotes") or {}).get("short")), _fill((doc.get("entry_quotes") or {}).get("long"))
    xs, xl = _fill((doc.get("exit_quotes") or {}).get("short")), _fill((doc.get("exit_quotes") or {}).get("long"))
    state = row.state
    status = {"PENDING_PAPER_ENTRY": "PENDING", "OPEN": "OPEN", "UNRESOLVED_EXPOSURE": "UNRESOLVED",
              "ENTRY_REJECTED": "REJECTED"}.get(state, "EXPIRED_UNPRICED" if unpriced else "CLOSED")
    flags: list[str] = []
    local = now.astimezone(IST)
    if status == "EXPIRED_UNPRICED":
        # The worker terminalised the position at expiry without inventing a settlement price: its P&L is UNKNOWN.
        flags.append("EXPIRED_UNPRICED")
    if status in ("OPEN", "UNRESOLVED"):
        if expiry and (expiry < local.date() or (expiry == local.date() and local.time() >= NIFTY_CLOSE)):
            flags.append("EXPIRED_NOT_SETTLED")
        if entry_at and (entry_at.astimezone(IST).date() < local.date() or (
                entry_at.astimezone(IST).date() == local.date() and local.time() > NIFTY_FORCED_EXIT)):
            flags.append("PAST_FORCED_EXIT")
        if status == "UNRESOLVED":
            flags.append("UNRESOLVED_EXPOSURE")
    entry_credit = px(doc.get("entry_credit")) if entry_at else None
    exit_debit = px(doc.get("exit_debit")) if status == "CLOSED" else None
    realized_gross = realized_gross_engine = None
    entry_ch = exit_ch = None
    if status == "CLOSED" and None not in (es, el, xs, xl):
        realized_gross = money((D(str(es)) - D(str(el)) - D(str(xs)) + D(str(xl))) * qty)
        realized_gross_engine = num((doc.get("final_outcome") or {}).get("gross_rupees"))
    if entry_at and None not in (es, el):
        entry_ch = charges.options(entry_at.astimezone(IST).date(), [(es, -1), (el, 1)], qty)
    if status == "CLOSED" and exit_at and None not in (xs, xl):
        exit_ch = charges.options(exit_at.astimezone(IST).date(), [(xs, 1), (xl, -1)], qty)
    incurred = add(entry_ch, exit_ch) if entry_ch else None
    realized_net = money(D(str(realized_gross)) - D(str(incurred["total"]))) if (
        realized_gross is not None and incurred) else None
    # --- mark (executable, fresh only) ---
    mk = {"basis": "UNAVAILABLE", "price": None, "at": None, "age_seconds": None, "bid": None, "ask": None,
          "reason": None, "last_known_price": None}
    unrealized = est_exit = est_net = None
    if status in ("OPEN", "UNRESOLVED") and mark:
        at = dt(mark.get("at"))
        age = (now - at).total_seconds() if at else None
        last_debit = px(doc.get("current_debit"))
        mk.update(at=iso(at), age_seconds=round(age, 1) if age is not None else None, last_known_price=last_debit)
        if mark.get("available") and age is not None and -5 <= age <= MARK_FRESH_SECONDS and last_debit is not None \
                and not flags:
            mk.update(basis="EXECUTABLE_BID_ASK", price=last_debit, bid=px(mark.get("long_px")),
                      ask=px(mark.get("short_px")))
            if entry_credit is not None:
                unrealized = money((D(str(entry_credit)) - D(str(last_debit))) * qty)
                if mark.get("short_px") is not None and mark.get("long_px") is not None:
                    est_exit = charges.options(local.date(), [(float(mark["short_px"]), 1),
                                                              (float(mark["long_px"]), -1)], qty)["total"]
                    est_net = money(D(str(unrealized)) - D(str(est_exit)) - D(str((incurred or {"total": 0})["total"])))
        else:
            mk.update(basis="STALE" if mark.get("available") else "UNAVAILABLE",
                      reason=("EXPIRED_CONTRACT_NOT_SETTLED" if "EXPIRED_NOT_SETTLED" in flags else
                              "MARK_OLDER_THAN_%ds" % MARK_FRESH_SECONDS if mark.get("available") else
                              mark.get("reason") or "LAST_MARK_ATTEMPT_UNAVAILABLE"))
    elif status in ("OPEN", "UNRESOLVED"):
        mk["reason"] = "NO_MARK_EVENT"
    risk = money((D(str(width)) - D(str(entry_credit or 0))) * qty) if width and entry_credit is not None else None
    policy_cfg = (doc.get("policy") or {}).get("config") or {}
    stop_mult = num(policy_cfg.get("stop_credit_multiple")) or 1.5
    legs = [
        {"role": "SHORT", "side_entry": "SELL", "option_type": short_leg.get("option_type"),
         "strike": num(short_leg.get("strike")), "symbol": short_leg.get("trading_symbol"), "quantity": qty,
         "entry_price": es, "exit_price": xs if status == "CLOSED" else None,
         **_bidask((doc.get("entry_quotes") or {}).get("short"))},
        {"role": "HEDGE", "side_entry": "BUY", "option_type": long_leg.get("option_type"),
         "strike": num(long_leg.get("strike")), "symbol": long_leg.get("trading_symbol"), "quantity": qty,
         "entry_price": el, "exit_price": xl if status == "CLOSED" else None,
         **_bidask((doc.get("entry_quotes") or {}).get("long"))},
    ]
    otype = short_leg.get("option_type") or ""
    contract = (f"NIFTY {num(short_leg.get('strike')):.0f}/{num(long_leg.get('strike')):.0f} {otype} "
                f"{'bear call' if otype == 'CE' else 'bull put'} spread · exp {expiry}" if short_leg and long_leg else None)
    return {
        "position_id": f"{eng['engine_id']}:{row.id}", "trade_id": row.id, "source": "ACTS_PAPER",
        "engine_id": eng["engine_id"], "strategy_id": version, "strategy_label": eng["label"],
        "strategy_version": version, "family": "ACTS", "instrument": "NIFTY", "structure": "CREDIT_SPREAD",
        "contract": contract, "expiry": str(expiry) if expiry else None, "direction": doc.get("direction"),
        "mode": "PAPER", "status": status, "flags": flags, "lots": lots, "lot_size": lot, "quantity": qty,
        "multiplier": 1, "decided_at": iso(decided_at), "entry_at": iso(entry_at), "exit_at": iso(exit_at),
        "entry_price": entry_credit, "entry_price_basis": "NET_CREDIT_PER_UNIT", "exit_price": exit_debit,
        "exit_price_basis": "NET_DEBIT_PER_UNIT", "mark": mk, "spread_width": width, "defined_max_loss": risk,
        "stop": {"basis": "SPREAD_DEBIT", "level": money(D(str(entry_credit)) * D(str(stop_mult)), "0.0001")
                 if entry_credit is not None else None, "rule": f"SPREAD_STOP when debit >= {stop_mult:g}x credit; "
                 "HARD_MAX_LOSS per policy"}, "trailing_stop": None, "target": None,
        "unrealized_gross": unrealized, "est_exit_charges": est_exit, "est_net_liquidation": est_net,
        "realized_gross": realized_gross, "realized_gross_engine": realized_gross_engine,
        "charges": {"incurred": incurred, "entry": entry_ch, "exit": exit_ch}, "realized_net": realized_net,
        "holding_seconds": num(doc.get("holding_seconds")) or ((exit_at or now) - entry_at).total_seconds()
        if entry_at else None, "entry_reason": doc.get("entry_reason"), "exit_reason": doc.get("exit_reason"),
        "score": num(doc.get("opportunity_score")), "score_band": doc.get("score_band"),
        "score_components": doc.get("score_components"), "cycle_id": doc.get("cycle_id"),
        "rearm_reason": doc.get("rearm_reason"), "rejection_reason": doc.get("rejection_reason"),
        "unresolved_reason": doc.get("unresolved_reason"), "mae_points": num(doc.get("mae")),
        "mfe_points": num(doc.get("mfe")), "mae": money(D(str(num(doc.get("mae")) or 0)) * qty)
        if doc.get("mae") is not None else None, "mfe": money(D(str(num(doc.get("mfe")) or 0)) * qty)
        if doc.get("mfe") is not None else None,
        "legs": legs, "journal": {"table": "acts_paper_trades", "id": row.id, **(refs or {})},
        "provenance": {"source": "acts_paper_trades.document", "pnl_basis": "GROSS_ENGINE_RECORD_RECOMPUTED_FROM_FILLS",
                       "charges_status": charges.status()},
        "decision_credit": px(doc.get("decision_executable_credit")),
    }


def acts_orders(pos: dict[str, Any], doc: dict[str, Any], events: dict[str, str]) -> list[dict[str, Any]]:
    """Simulated per-leg orders. `events`: lifecycle event type -> market timestamp (from the journal)."""
    if pos["status"] not in ("PENDING", "OPEN", "UNRESOLVED", "CLOSED", "EXPIRED_UNPRICED", "REJECTED"):
        return []
    qty, base = pos["quantity"], pos["position_id"]
    dq = doc.get("decision_entry_quotes") or {}
    legs = pos["legs"]
    entry_status = {"REJECTED": "REJECTED", "PENDING": "PENDING"}.get(pos["status"], "FILLED")
    created = pos["decided_at"]
    updated_entry = pos["entry_at"] or events.get("ACTS_ENTRY_REJECTED") or created
    out = []
    for leg, side, key in ((legs[0], "SELL", "short"), (legs[1], "BUY", "long")):
        out.append({
            "order_id": f"{base}:ENTRY:{leg['role']}", "broker_order_id": None, "simulated": True, "mode": "PAPER",
            "position_id": pos["position_id"], "trade_id": pos["trade_id"], "engine_id": pos["engine_id"],
            "strategy_id": pos["strategy_id"], "strategy_version": pos["strategy_version"],
            "strategy_label": pos["strategy_label"], "instrument": "NIFTY", "contract": leg["symbol"],
            "side": side, "intent": "ENTRY", "order_type": "SIMULATED_NEXT_OBSERVATION_BID_ASK",
            "requested_quantity": qty, "filled_quantity": qty if entry_status == "FILLED" else 0,
            "remaining_quantity": 0 if entry_status == "FILLED" else qty,
            "requested_price": px(((dq.get(key) or {}).get("quote") or {}).get("bid" if side == "SELL" else "ask")),
            "avg_fill_price": leg["entry_price"] if entry_status == "FILLED" else None, "trigger_price": None,
            "submitted_at": created, "last_update_at": updated_entry, "status": entry_status,
            "rejection_reason": pos["rejection_reason"] if entry_status == "REJECTED" else None})
    if pos["status"] in ("CLOSED", "EXPIRED_UNPRICED"):
        for leg, side in ((legs[0], "BUY"), (legs[1], "SELL")):
            filled = pos["status"] == "CLOSED"
            out.append({
                "order_id": f"{base}:EXIT:{leg['role']}", "broker_order_id": None, "simulated": True, "mode": "PAPER",
                "position_id": pos["position_id"], "trade_id": pos["trade_id"], "engine_id": pos["engine_id"],
                "strategy_id": pos["strategy_id"], "strategy_version": pos["strategy_version"],
                "strategy_label": pos["strategy_label"], "instrument": "NIFTY", "contract": leg["symbol"],
                "side": side, "intent": "EXIT", "order_type": "SIMULATED_NEXT_OBSERVATION_BID_ASK",
                "requested_quantity": qty, "filled_quantity": qty if filled else 0,
                "remaining_quantity": 0 if filled else qty, "requested_price": None,
                "avg_fill_price": leg["exit_price"] if filled else None, "trigger_price": None,
                "submitted_at": pos["exit_at"] or pos["entry_at"], "last_update_at": pos["exit_at"],
                "status": "FILLED" if filled else "EXPIRED",
                "rejection_reason": None if filled else "CONTRACT_EXPIRED_UNPRICED"})
    return out


# ----------------------------------------------------------------------------------------------------- MCX futures
def mcx_position(row: Any, doc: dict[str, Any], now: datetime, charges: ChargeModel,
                 cfg: dict[str, Any] | None) -> dict[str, Any]:
    inst = row.instrument
    eng = ENGINES[inst]
    lot = int(num(doc.get("lot_size")) or 0)
    lots = int(num(doc.get("quantity_lots")) or 1)
    qty = lot * lots
    direction = doc.get("direction")
    sign = 1 if direction == "LONG" else -1
    entry_at, exit_at, decided = dt(doc.get("entry_at")), dt(doc.get("exit_at")), dt(doc.get("at"))
    entry_p, exit_p = px(doc.get("entry_price")), px(doc.get("exit_price"))
    state = row.state
    status = {"PENDING_PAPER_ENTRY": "PENDING", "OPEN": "OPEN", "UNRESOLVED_EXPOSURE": "UNRESOLVED",
              "ENTRY_REJECTED": "REJECTED", "CLOSED": "CLOSED"}[state]
    realized_gross = realized_net = None
    entry_side, exit_side = ("BUY", "SELL") if direction == "LONG" else ("SELL", "BUY")
    entry_ch = exit_ch = None
    if entry_at and entry_p is not None:
        entry_ch = charges.futures(entry_p, qty, entry_side)
    if status == "CLOSED" and entry_p is not None and exit_p is not None:
        realized_gross = money((D(str(exit_p)) - D(str(entry_p))) * sign * qty)
        exit_ch = charges.futures(exit_p, qty, exit_side)
    incurred = add(entry_ch, exit_ch) if entry_ch else None
    if realized_gross is not None and incurred:
        realized_net = money(D(str(realized_gross)) - D(str(incurred["total"])))
    lm = doc.get("last_mark") or {}
    mark_at = dt(lm.get("at"))
    age = (now - mark_at).total_seconds() if mark_at else None
    mk = {"basis": "UNAVAILABLE", "price": None, "at": iso(mark_at), "age_seconds": round(age, 1) if age is not None
          else None, "bid": None, "ask": None, "reason": None, "last_known_price": px(lm.get("mark_price"))}
    unrealized = est_exit = est_net = None
    if status in ("OPEN", "UNRESOLVED"):
        if lm.get("mark_price") is not None and age is not None and -5 <= age <= MARK_FRESH_SECONDS:
            mk.update(basis="EXECUTABLE_BID_ASK", price=px(lm.get("mark_price")))
            unrealized = money((D(str(lm["mark_price"])) - D(str(entry_p))) * sign * qty)
            est_exit = charges.futures(float(lm["mark_price"]), qty, exit_side)["total"]
            est_net = money(D(str(unrealized)) - D(str(est_exit)) - D(str((incurred or {"total": 0})["total"])))
        else:
            mk.update(basis="STALE" if lm.get("mark_price") is not None else "UNAVAILABLE",
                      reason="MARK_OLDER_THAN_%ds" % MARK_FRESH_SECONDS if lm.get("mark_price") is not None
                      else "NO_MARK_EVENT")
    stop = px(doc.get("stop_price"))
    risk = money(abs(D(str(entry_p)) - D(str(stop))) * qty) if entry_p is not None and stop is not None else None
    contract = doc.get("contract")
    return {
        "position_id": f"{eng['engine_id']}:{row.trade_id}", "trade_id": row.trade_id, "source": "MCX_FUTURES_PAPER",
        "engine_id": eng["engine_id"], "strategy_id": "ACTS_V3_MCX_FUTURES", "strategy_label": eng["label"],
        "strategy_version": "ACTS_V3_MCX_FUTURES", "family": "MCX_FUTURES", "instrument": inst,
        "structure": "FUTURES_OUTRIGHT", "contract": contract, "expiry": None, "direction": direction, "mode": "PAPER",
        "status": status, "flags": [], "lots": lots, "lot_size": lot, "quantity": qty, "multiplier": lot,
        "decided_at": iso(decided), "entry_at": iso(entry_at), "exit_at": iso(exit_at), "entry_price": entry_p,
        "entry_price_basis": "FUTURES_PRICE", "exit_price": exit_p, "exit_price_basis": "FUTURES_PRICE", "mark": mk,
        "spread_width": None, "defined_max_loss": risk,
        "stop": {"basis": "PRICE", "level": stop, "rule": "ATR-based protective stop (engine config)"},
        "trailing_stop": {"basis": "PRICE", "level": stop} if stop is not None and doc.get("trail_active") else None,
        "target": None, "unrealized_gross": unrealized, "est_exit_charges": est_exit, "est_net_liquidation": est_net,
        "realized_gross": realized_gross, "realized_gross_engine": num(doc.get("gross_pnl")),
        "charges": {"incurred": incurred, "entry": entry_ch, "exit": exit_ch}, "realized_net": realized_net,
        "engine_recorded_net": num(doc.get("net_pnl")), "engine_recorded_fees": num(doc.get("fees")),
        "holding_seconds": num(doc.get("holding_seconds")) or (((exit_at or now) - entry_at).total_seconds()
                                                              if entry_at else None),
        "entry_reason": doc.get("entry_reason"), "exit_reason": doc.get("exit_reason"), "score": num(doc.get("score")),
        "score_band": None, "score_components": None, "cycle_id": doc.get("cycle_id"), "rearm_reason": None,
        "rejection_reason": doc.get("rejection_reason"), "unresolved_reason": doc.get("unresolved_reason"),
        "mae_points": None, "mfe_points": None, "mae": num(doc.get("mae_rupees")), "mfe": num(doc.get("mfe_rupees")),
        "legs": [{"role": "OUTRIGHT", "side_entry": entry_side, "option_type": None, "strike": None, "symbol": contract,
                  "quantity": qty, "entry_price": entry_p, "exit_price": exit_p if status == "CLOSED" else None,
                  "bid": None, "ask": None, "bid_qty": None, "ask_qty": None}],
        "journal": {"table": "mcx_futures_trades", "id": row.trade_id, "last_sequence": doc.get("journal_sequence")},
        "provenance": {"source": "mcx_futures_trades.document (projection of mcx_futures_events)",
                       "pnl_basis": "GROSS_RECOMPUTED_FROM_FILLS", "charges_status": charges.status()},
        "decision_credit": None,
    }


def mcx_orders(pos: dict[str, Any], doc: dict[str, Any]) -> list[dict[str, Any]]:
    qty, base = pos["quantity"], pos["position_id"]
    entry_status = {"REJECTED": "REJECTED", "PENDING": "PENDING"}.get(pos["status"], "FILLED")
    side_in = pos["legs"][0]["side_entry"]
    out = [{
        "order_id": f"{base}:ENTRY", "broker_order_id": None, "simulated": True, "mode": "PAPER",
        "position_id": pos["position_id"], "trade_id": pos["trade_id"], "engine_id": pos["engine_id"],
        "strategy_id": pos["strategy_id"], "strategy_version": pos["strategy_version"],
        "strategy_label": pos["strategy_label"], "instrument": pos["instrument"], "contract": pos["contract"],
        "side": side_in, "intent": "ENTRY", "order_type": "SIMULATED_NEXT_OBSERVATION_BID_ASK",
        "requested_quantity": qty, "filled_quantity": qty if entry_status == "FILLED" else 0,
        "remaining_quantity": 0 if entry_status == "FILLED" else qty, "requested_price": None,
        "avg_fill_price": pos["entry_price"] if entry_status == "FILLED" else None, "trigger_price": None,
        "submitted_at": pos["decided_at"], "last_update_at": pos["entry_at"] or pos["decided_at"],
        "status": entry_status, "rejection_reason": pos["rejection_reason"] if entry_status == "REJECTED" else None}]
    if pos["status"] == "CLOSED":
        stop_exit = str(pos["exit_reason"] or "").upper().find("STOP") >= 0
        out.append({
            "order_id": f"{base}:EXIT", "broker_order_id": None, "simulated": True, "mode": "PAPER",
            "position_id": pos["position_id"], "trade_id": pos["trade_id"], "engine_id": pos["engine_id"],
            "strategy_id": pos["strategy_id"], "strategy_version": pos["strategy_version"],
            "strategy_label": pos["strategy_label"], "instrument": pos["instrument"], "contract": pos["contract"],
            "side": "SELL" if side_in == "BUY" else "BUY", "intent": "EXIT",
            "order_type": "SIMULATED_STOP" if stop_exit else "SIMULATED_NEXT_OBSERVATION_BID_ASK",
            "requested_quantity": qty, "filled_quantity": qty, "remaining_quantity": 0, "requested_price": None,
            "avg_fill_price": pos["exit_price"], "trigger_price": pos["stop"]["level"] if stop_exit else None,
            "submitted_at": pos["exit_at"], "last_update_at": pos["exit_at"], "status": "FILLED",
            "rejection_reason": None})
    return out


def fills(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"fill_id": o["order_id"] + ":FILL", "order_id": o["order_id"], "position_id": o["position_id"],
             "engine_id": o["engine_id"], "strategy_label": o["strategy_label"], "instrument": o["instrument"],
             "contract": o["contract"], "side": o["side"], "intent": o["intent"], "quantity": o["filled_quantity"],
             "price": o["avg_fill_price"], "at": o["last_update_at"], "simulated": True, "mode": "PAPER"}
            for o in orders if o["status"] == "FILLED" and o["avg_fill_price"] is not None]


def loads(text: str) -> dict[str, Any]:
    try:
        out = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}
