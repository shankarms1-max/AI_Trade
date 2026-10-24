"""Read-only paper dashboard projections. No strategy, capture or broker objects."""

from datetime import datetime
import json
from math import isfinite

from sqlalchemy import inspect, select, text

from app.commodity_acts.config import INSTRUMENTS
from app.commodity_acts.paper_models import (
    CommodityPaperCursor,
    CommodityPaperEvent,
    CommodityPaperFill,
    CommodityPaperOrder,
    CommodityPaperPosition,
)
from app.commodity_acts.paper_policy import ACTIVE
from app.commodity_acts.paper_naked import VERSION, NakedPaperPolicy, load_policy, reservation
from app.alpha.clock import IST


def readonly(session):
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


def _sum_known(rows, field):
    values = [row.get(field) for row in rows]
    return (
        sum(values)
        if values
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) for value in values
        )
        else None
    )


def _today(document, field, at):
    try:
        stamp = datetime.fromisoformat(document[field])
        return stamp.tzinfo is not None and stamp <= at and stamp.astimezone(IST).date() == at.astimezone(IST).date()
    except (KeyError, ValueError, TypeError):
        return False


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
            configuration_blocks = []
            try:
                spec = config.specification(instrument)
                market_open = spec.market_session.contains(at)
            except (ValueError, TypeError, KeyError):
                market_open = None
                configuration_blocks.append("SESSION_UNCONFIGURED")
            try:
                policy = config.trading_policy(instrument)
                evidence = policy.evidence.model_dump(mode="json") if policy else None
                if policy:
                    max_age = policy.max_quote_age_seconds
            except (ValueError, TypeError, KeyError):
                policy, evidence = None, None
                configuration_blocks.append("POLICY_INVALID")
            configured = policy is not None
            if not configured and "POLICY_INVALID" not in configuration_blocks:
                configuration_blocks.append("POLICY_UNCONFIGURED")
            risk_limit = (
                config.global_max_stress_risk
                if isinstance(policy, NakedPaperPolicy)
                else config.global_max_defined_risk
            )
            if risk_limit is None or config.global_daily_loss_limit is None:
                configuration_blocks.append("GLOBAL_RISK_UNCONFIGURED")
            fresh_marks = bool(open_rows) and all(
                doc.get("mark_at")
                and doc.get("unrealized_gross") is not None
                and 0
                <= (at - datetime.fromisoformat(doc["mark_at"])).total_seconds()
                <= doc["policy"]["max_quote_age_seconds"]
                for doc in open_rows
            )
            latest = (json.loads(cursor.latest_json) or None) if cursor else None
            if config.auto_contract_renewal:
                evidence = latest.get("contract_evidence") if latest else None
            observed_at = (
                datetime.fromisoformat(latest["observed_at"]) if latest and latest.get("observed_at") else None
            )
            age = (at - observed_at).total_seconds() if observed_at else None
            freshness = (
                "UNAVAILABLE"
                if age is None
                else "FUTURE_OBSERVATION" if age < 0 else "FRESH" if age <= max_age else "STALE"
            )
            gross = _sum_known(closed, "gross_realized_pnl")
            net = _sum_known(closed, "net_realized_pnl")
            closed_today = [doc for doc in closed if _today(doc, "exit_timestamp", at)]
            readiness = (
                "MIGRATION_REQUIRED"
                if not present
                else (
                    "PAPER_DISABLED"
                    if not enabled
                    else (
                        configuration_blocks[0]
                        if configuration_blocks
                        else (
                            "NO_OBSERVATIONS_YET"
                            if not latest
                            else "OUTSIDE_SESSION" if not market_open else "ACTIVE_PAPER_RESEARCH"
                        )
                    )
                )
            )
            items.append(
                {
                    "instrument_id": instrument,
                    "paper_enabled": enabled,
                    "worker_state": "PROCESS_STATE_UNAVAILABLE",
                    "market_open": market_open,
                    "status": (
                        "MIGRATION_REQUIRED"
                        if not present
                        else (
                            "DISABLED"
                            if not enabled
                            else (
                                "POLICY_UNCONFIGURED"
                                if not configured
                                else "WAITING_FOR_OBSERVATIONS" if not latest else "ACTIVE_PAPER_RESEARCH"
                            )
                        )
                    ),
                    "readiness": readiness,
                    "configuration_blockers": configuration_blocks,
                    "session_configured": market_open is not None,
                    "policy_configured": configured,
                    "execution_evidence": evidence,
                    "contract_renewal_status": latest.get("contract_renewal_status") if latest else None,
                    "latest": latest,
                    "observation_freshness": freshness,
                    "observation_age_seconds": age,
                    "pending_entries": sum(state == "PENDING_PAPER_ENTRY" for state, _ in rows),
                    "open_positions": len(open_rows),
                    "pending_exits": sum(bool(doc.get("exit_order_id")) for doc in open_rows),
                    "unresolved_exposure": sum(state == "UNRESOLVED_EXPOSURE" for state, _ in rows),
                    "rejected_entries": sum(state == "ENTRY_REJECTED" for state, _ in rows),
                    "closed_trades": len(closed),
                    "wins": (
                        sum(doc["gross_realized_pnl"] > 0 for doc in closed)
                        if not closed or gross is not None
                        else None
                    ),
                    "losses": (
                        sum(doc["gross_realized_pnl"] < 0 for doc in closed)
                        if not closed or gross is not None
                        else None
                    ),
                    "trades_today": sum(_today(doc, "entry_timestamp", at) for _, doc in rows),
                    "realized_gross_today": _sum_known(closed_today, "gross_realized_pnl"),
                    "realized_net_today": _sum_known(closed_today, "net_realized_pnl"),
                    "sample_sessions": len({doc["session_id"] for _, doc in rows if doc.get("entry_timestamp")}),
                    "realized_gross": gross,
                    "realized_net": net,
                    "unrealized_gross": sum(doc["unrealized_gross"] for doc in open_rows) if fresh_marks else None,
                    "unrealized_net": (
                        sum(doc["unrealized_net"] for doc in open_rows)
                        if fresh_marks and all(doc.get("unrealized_net") is not None for doc in open_rows)
                        else None
                    ),
                    "strategy": "NAKED_SHORT_OPTION",
                    "maximum_loss": None,
                    "loss_is_bounded": False,
                    "reserved_stress_risk": sum(
                        reservation(doc)
                        for state, doc in rows
                        if state in ACTIVE and doc.get("strategy") == "NAKED_SHORT_OPTION"
                    ),
                    "reserved_defined_risk": sum(
                        doc["defined_risk"] for state, doc in rows if state in ACTIVE and "defined_risk" in doc
                    ),
                    "expiry_exit_overdue": sum(
                        isinstance(p := load_policy(doc["policy"]), NakedPaperPolicy) and at >= p.expiry_cutoff()
                        for doc in open_rows
                    ),
                    "margin_status": (
                        "SEE_TRADE_EVIDENCE"
                        if any(doc.get("entry_timestamp") for _, doc in rows)
                        and all(
                            doc.get("margin_evidence") is not None for _, doc in rows if doc.get("entry_timestamp")
                        )
                        else "MARGIN_UNVERIFIED"
                    ),
                    "profitability_claim": False,
                }
            )
        return {
            "version": VERSION,
            "strategy": "NAKED_SHORT_OPTION",
            "stage": "AUTOMATED_PAPER",
            "as_of": at.isoformat(),
            "broker_orders_enabled": False,
            "instruments": items,
        }


def page(sessions, instrument, kind, limit=50, offset=0, at=None):
    models = {
        "positions": CommodityPaperPosition,
        "orders": CommodityPaperOrder,
        "fills": CommodityPaperFill,
        "events": CommodityPaperEvent,
    }
    if instrument not in INSTRUMENTS or kind not in models or not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ValueError("INVALID_COMMODITY_PAPER_FILTER")
    model = models[kind]
    at = at or datetime.now(IST)
    with sessions() as session, session.no_autoflush:
        readonly(session)
        if not inspect(session.get_bind()).has_table("commodity_paper_cursors"):
            return {"status": "MIGRATION_REQUIRED", "items": [], "has_more": False, "limit": limit, "offset": offset}
        rows = list(
            session.scalars(
                select(model)
                .where(model.instrument_id == instrument)
                .order_by(
                    model.sequence.desc() if kind == "events" else model.observed_at.desc(),
                    model.event_key if kind == "events" else model.id,
                )
                .offset(offset)
                .limit(limit + 1)
            )
        )
        position_ids = (
            {json.loads(row.document)["position_id"] for row in rows[:limit]} if kind in ("orders", "fills") else set()
        )
        contracts = (
            {
                row.id: json.loads(row.document)["candidate"]
                for row in session.scalars(
                    select(CommodityPaperPosition).where(
                        CommodityPaperPosition.instrument_id == instrument, CommodityPaperPosition.id.in_(position_ids)
                    )
                )
            }
            if position_ids
            else {}
        )
        items = []
        for row in rows[:limit]:
            if kind == "events":
                payload = json.loads(row.payload)
                items.append(
                    {
                        "sequence": row.sequence,
                        "event_type": row.event_type,
                        "event_hash": row.event_hash,
                        "observed_at": payload["observed_at"],
                        "processed_at": payload["processed_at"],
                        "reason": payload.get("reason"),
                        "latest": payload.get("latest"),
                        "changes": [
                            {"type": change["table"], "id": change["id"], "state": change.get("state")}
                            for change in payload["changes"]
                        ],
                    }
                )
            else:
                document = json.loads(row.document)
                document.setdefault("observed_at", row.observed_at)
                if kind in ("orders", "fills"):
                    document["candidate"] = contracts.get(document["position_id"])
                if kind == "positions" and row.state in ("OPEN", "PENDING_EXIT", "UNRESOLVED_EXPOSURE"):
                    mark_at = document.get("mark_at")
                    if (
                        not mark_at
                        or not 0
                        <= (at - datetime.fromisoformat(mark_at)).total_seconds()
                        <= document["policy"]["max_quote_age_seconds"]
                    ):
                        document["unrealized_gross"] = document["unrealized_net"] = None
                        document["mark_status"] = "UNAVAILABLE_OR_STALE"
                items.append(document)
        return {
            "instrument_id": instrument,
            "items": items,
            "has_more": len(rows) > limit,
            "limit": limit,
            "offset": offset,
        }
