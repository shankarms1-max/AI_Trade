"""Read-only commodity status/journal projections; no service or broker imports."""
from datetime import datetime
import json

from sqlalchemy import func, select, text

from app.commodity_acts.config import INSTRUMENTS, RESEARCH_SCOPE, STAGE, VERSION
from app.commodity_acts.models import CommodityActsCursor, CommodityActsDecision, CommodityMarketSnapshotRecord


def view(document: dict) -> dict:
    keys = ("version", "instrument_id", "session_id", "snapshot_id", "observed_at", "decided_at", "decision_kind",
            "readiness", "reference_price", "future_price", "reference_basis", "future_instrument_id", "future_token",
            "future_exchange", "future_expiry", "option_expiry", "lot_size", "atm", "policy_hash", "unavailable_components",
            "score_basis", "maximum_score_without_execution", "timestamp_provenance", "data_provenance", "blockers")
    result = {key: document[key] for key in keys if key in document}
    result.update(stage=STAGE, research_scope=RESEARCH_SCOPE, mode="PAPER",
                  broker_orders_enabled=False, spread_execution_enabled=False, pnl=None)
    geometry = document.get("geometry", {})
    result["geometry"] = {key: value for key, value in geometry.items() if key != "observed_bid_ask_credits_by_steps"}
    result["geometry"]["credit_geometry_summary"] = [
        {"observed_steps": step, "comparisons": len(rows),
         "min_observed_credit": min(row["observed_credit"] for row in rows),
         "max_observed_credit": max(row["observed_credit"] for row in rows)}
        for step, rows in geometry.get("observed_bid_ask_credits_by_steps", {}).items() if rows]
    alpha = document.get("alpha", {})
    result["alpha"] = {key: alpha[key] for key in ("timestamp", "feature_calculated_at", "alpha_1", "alpha_2",
        "alpha_1_direction", "alpha_2_direction", "status", "validity_state", "rank_observations_alpha1", "rank_observations_alpha2",
        "warnings", "session_id", "reference_instrument_id", "reference_expiry") if key in alpha}
    decision = document.get("acts", {})
    result["acts"] = {key: decision[key] for key in ("timestamp", "status", "market_state", "direction", "opportunity_score", "score_band",
        "score_components", "score_evidence", "positioning", "blockers", "candidate_rejections") if key in decision}
    return result


def _readonly(session):
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION READ ONLY"))


def summary(sessions, config, at: datetime) -> dict:
    with sessions() as session, session.no_autoflush:
        _readonly(session)
        cursors = {row.instrument_id: row for row in session.scalars(select(CommodityActsCursor))}
        counts = dict(session.execute(select(CommodityMarketSnapshotRecord.instrument_id, func.count()).group_by(
            CommodityMarketSnapshotRecord.instrument_id)).all())
        decisions = dict(session.execute(select(CommodityActsDecision.instrument_id, func.count()).group_by(CommodityActsDecision.instrument_id)).all())
        instruments = []
        for instrument in INSTRUMENTS:
            cursor = cursors.get(instrument)
            latest = json.loads(cursor.latest_json) if cursor else {}
            try:
                config.specification(instrument)
                configured = True
            except ValueError:
                configured = False
            instruments.append({"instrument_id": instrument, "display_name": instrument,
                "configured_enabled": config.enabled(instrument), "session_configured": configured,
                "worker_state": "PROCESS_STATE_UNAVAILABLE", "observation_count": counts.get(instrument, 0),
                "decision_count": decisions.get(instrument, 0), "journal_sequence": cursor.sequence if cursor else 0,
                "readiness": latest.get("readiness", "NOT_CAPTURED"), "latest": view(latest) if latest else None})
    return {"version": VERSION, "stage": STAGE, "research_scope": RESEARCH_SCOPE,
            "as_of": at.isoformat(), "mode": "PAPER", "broker_orders_enabled": False,
            "spread_execution_enabled": False, "pnl": None, "instruments": instruments}


def decisions(sessions, instrument: str, limit=50, offset=0):
    if instrument not in INSTRUMENTS or not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ValueError("COMMODITY_DECISION_FILTER_INVALID")
    with sessions() as session, session.no_autoflush:
        _readonly(session)
        rows = list(session.scalars(select(CommodityActsDecision).where(CommodityActsDecision.instrument_id == instrument).order_by(
            CommodityActsDecision.snapshot_id.desc()).offset(offset).limit(limit + 1)))
        return {"instrument_id": instrument, "limit": limit, "offset": offset, "has_more": len(rows) > limit,
                "items": [view(json.loads(row.document)) for row in rows[:limit]]}
