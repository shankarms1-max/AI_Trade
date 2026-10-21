"""Read-only paper dashboard projections. No strategy, capture or broker objects."""
from datetime import datetime
import json

from sqlalchemy import inspect, select, text

from app.commodity_acts.config import INSTRUMENTS
from app.commodity_acts.paper_models import (CommodityPaperCursor, CommodityPaperEvent, CommodityPaperFill,
                                           CommodityPaperOrder, CommodityPaperPosition)
from app.commodity_acts.paper_policy import ACTIVE, PAPER_VERSION
from app.alpha.clock import IST


def readonly(session):
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


def summary(sessions, config, at):
    with sessions() as session, session.no_autoflush:
        readonly(session)
        present = inspect(session.get_bind()).has_table("commodity_paper_cursors")
        cursors = {row.instrument_id: row for row in session.scalars(select(CommodityPaperCursor))} if present else {}
        positions = list(session.scalars(select(CommodityPaperPosition))) if present else []
        items = []
        for instrument in INSTRUMENTS:
            cursor = cursors.get(instrument)
            rows = [(row.state, json.loads(row.document)) for row in positions if row.instrument_id == instrument]
            closed = [doc for state, doc in rows if state == "CLOSED"]
            open_rows = [doc for state, doc in rows if state in ("OPEN", "PENDING_EXIT", "UNRESOLVED_EXPOSURE")]
            enabled = config.trading_enabled and config.enabled(instrument)
            max_age = config.max_quote_age_seconds
            try:
                spec, policy = config.specification(instrument), config.trading_policy(instrument)
                market_open = spec.market_session.contains(at)
                configured = policy is not None
                evidence = policy.evidence.model_dump(mode="json") if policy else None
                if policy: max_age = policy.max_quote_age_seconds
            except (ValueError, TypeError, KeyError):
                market_open, configured, evidence = None, False, None
            fresh_marks = bool(open_rows) and all(doc.get("mark_at") and doc.get("unrealized_gross") is not None
                and 0 <= (at-datetime.fromisoformat(doc["mark_at"])).total_seconds() <= doc["policy"]["max_quote_age_seconds"] for doc in open_rows)
            latest = json.loads(cursor.latest_json) if cursor else None
            observed_at = datetime.fromisoformat(latest["observed_at"]) if latest and latest.get("observed_at") else None
            age = (at-observed_at).total_seconds() if observed_at else None
            freshness = ("UNAVAILABLE" if age is None else "FUTURE_OBSERVATION" if age < 0 else
                         "FRESH" if age <= max_age else "STALE")
            gross = sum(doc["gross_realized_pnl"] for doc in closed) if closed else None
            net = sum(doc["net_realized_pnl"] for doc in closed) if closed and all(doc["net_realized_pnl"] is not None for doc in closed) else None
            items.append({"instrument_id": instrument, "paper_enabled": enabled,
                "worker_state": "PROCESS_STATE_UNAVAILABLE", "market_open": market_open,
                "status": "MIGRATION_REQUIRED" if cursor is None else "DISABLED" if not enabled else
                          "POLICY_UNCONFIGURED" if not configured else "WAITING_FOR_OBSERVATIONS" if not latest else "ACTIVE_PAPER_RESEARCH",
                "execution_evidence": evidence, "latest": latest,
                "observation_freshness": freshness, "observation_age_seconds": age,
                "pending_entries": sum(state == "PENDING_PAPER_ENTRY" for state, _ in rows),
                "open_positions": len(open_rows), "pending_exits": sum(bool(doc.get("exit_order_id")) for doc in open_rows),
                "unresolved_exposure": sum(state == "UNRESOLVED_EXPOSURE" for state, _ in rows),
                "rejected_entries": sum(state == "ENTRY_REJECTED" for state, _ in rows),
                "closed_trades": len(closed), "wins": sum(doc["gross_realized_pnl"] > 0 for doc in closed),
                "losses": sum(doc["gross_realized_pnl"] < 0 for doc in closed),
                "sample_sessions": len({doc["session_id"] for _, doc in rows if doc.get("entry_timestamp")}),
                "realized_gross": gross, "realized_net": net,
                "unrealized_gross": sum(doc["unrealized_gross"] for doc in open_rows) if fresh_marks else None,
                "unrealized_net": sum(doc["unrealized_net"] for doc in open_rows) if fresh_marks and all(
                    doc.get("unrealized_net") is not None for doc in open_rows) else None,
                "reserved_defined_risk": sum(doc["defined_risk"] for state, doc in rows if state in ACTIVE),
                "margin_status": "SEE_TRADE_EVIDENCE" if any(doc.get("entry_timestamp") for _, doc in rows) and all(
                    doc.get("margin_evidence") is not None for _, doc in rows if doc.get("entry_timestamp")) else "MARGIN_UNVERIFIED",
                "profitability_claim": False})
        return {"version": PAPER_VERSION, "stage": "AUTOMATED_PAPER", "as_of": at.isoformat(),
                "broker_orders_enabled": False, "instruments": items}


def page(sessions, instrument, kind, limit=50, offset=0, at=None):
    models = {"positions": CommodityPaperPosition, "orders": CommodityPaperOrder,
              "fills": CommodityPaperFill, "events": CommodityPaperEvent}
    if instrument not in INSTRUMENTS or kind not in models or not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ValueError("INVALID_COMMODITY_PAPER_FILTER")
    model = models[kind]
    at = at or datetime.now(IST)
    with sessions() as session, session.no_autoflush:
        readonly(session)
        if not inspect(session.get_bind()).has_table("commodity_paper_cursors"):
            return {"status": "MIGRATION_REQUIRED", "items": [], "has_more": False, "limit": limit, "offset": offset}
        rows = list(session.scalars(select(model).where(model.instrument_id == instrument).order_by(
            model.sequence.desc() if kind == "events" else model.observed_at.desc(),
            model.event_key if kind == "events" else model.id).offset(offset).limit(limit+1)))
        position_ids = {json.loads(row.document)["position_id"] for row in rows[:limit]} if kind in ("orders", "fills") else set()
        contracts = {row.id: json.loads(row.document)["candidate"] for row in session.scalars(select(
            CommodityPaperPosition).where(CommodityPaperPosition.instrument_id == instrument,
                                         CommodityPaperPosition.id.in_(position_ids)))} if position_ids else {}
        items = []
        for row in rows[:limit]:
            if kind == "events":
                payload = json.loads(row.payload)
                items.append({"sequence": row.sequence, "event_type": row.event_type, "event_hash": row.event_hash,
                    "observed_at": payload["observed_at"], "processed_at": payload["processed_at"],
                    "reason": payload.get("reason"), "latest": payload.get("latest"),
                    "changes": [{"type": change["table"], "id": change["id"], "state": change.get("state")} for change in payload["changes"]]})
            else:
                document = json.loads(row.document)
                document.setdefault("observed_at", row.observed_at)
                if kind in ("orders", "fills"):
                    document["candidate"] = contracts.get(document["position_id"])
                if kind == "positions" and row.state in ("OPEN", "PENDING_EXIT", "UNRESOLVED_EXPOSURE"):
                    mark_at = document.get("mark_at")
                    if not mark_at or not 0 <= (at-datetime.fromisoformat(mark_at)).total_seconds() <= document["policy"]["max_quote_age_seconds"]:
                        document["unrealized_gross"] = document["unrealized_net"] = None
                        document["mark_status"] = "UNAVAILABLE_OR_STALE"
                items.append(document)
        return {"instrument_id": instrument, "items": items, "has_more": len(rows) > limit, "limit": limit, "offset": offset}
