"""Common one-observation PAPER transition. Persistence belongs to the caller."""

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
import json

from app.adaptive_supertrend import VERSION
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.decision import decide
from app.adaptive_supertrend.journal import ACTIVE
from app.adaptive_supertrend.models import AstTrade
from app.research.manifest import canonical, digest, json_value
from app.scalper.repository import _aware


@dataclass(frozen=True)
class PaperStep:
    decision: dict
    signal_opportunity: str | None
    transition: str | None
    pending: AstTrade | None


def valid_lot(observed, expected: int) -> bool:
    return type(observed) is int and observed == expected


def _transition(policy: AstPolicy, raw, at, price, row: AstTrade, decision, state) -> str | None:
    if row.state == "UNRESOLVED_EXPOSURE":
        return None
    cfg = policy.execution
    assert cfg is not None
    doc = json.loads(row.document)
    same_contract = (raw.future_instrument_id, str(raw.future_expiry)) == (
        doc["future_instrument_id"],
        doc["future_expiry"],
    )
    same_session = row.trading_date == at.date() and at.time() < time(15, 30, 15)
    ending = at.time() >= cfg.session_exit_time
    reason = None
    if not same_session:
        reason = "MISSED_SESSION_EXIT" if row.state != "PENDING_PAPER_ENTRY" else "SESSION_OVER"
    elif not same_contract or raw.future_expiry is None or raw.future_expiry < at.date():
        reason = "EXACT_FUTURES_CONTRACT_UNAVAILABLE"
    elif not valid_lot(raw.lot_size, doc["lot_size"]):
        reason = "FUTURES_LOT_SIZE_UNAVAILABLE_OR_MISMATCH"
    elif price is None:
        reason = "FUTURES_OBSERVATION_UNAVAILABLE"

    if row.state == "PENDING_PAPER_ENTRY":
        delay = (at - datetime.fromisoformat(doc["decision_timestamp"])).total_seconds()
        if reason is None:
            if delay <= 0 or delay > policy.max_sequence_gap_seconds:
                reason = "ENTRY_OBSERVATION_GAP"
            elif at.time() > cfg.entry_end_time:
                reason = "OUTSIDE_ENTRY_WINDOW"
            elif decision["status"] != "ENTER" or decision["opportunity_id"] != doc["opportunity_id"]:
                reason = "AST_PENDING_THESIS_INVALID"
        if reason:
            row.state = kind = "ENTRY_REJECTED"
            doc["rejection_reason"] = reason
        else:
            row.state, kind = "OPEN", "ENTRY"
            doc.update(
                future_instrument_id=raw.future_instrument_id,
                future_expiry=str(raw.future_expiry),
                entry_observed_lot_size=raw.lot_size,
                entry_timestamp=at.isoformat(),
                entry_snapshot_id=raw.id,
                entry_price=float(price),
                mark_at=at.isoformat(),
                mark_price=float(price),
                gross_mark_rupees=0,
                gross_mark_points=0,
                mae_points=0,
                mfe_points=0,
            )
            state.setdefault("consumed", []).append(doc["opportunity_id"])
    else:
        features = decision["features"]
        reversed_at = features.get("ast_reversal_at")
        reversal = (
            reversed_at
            and datetime.fromisoformat(reversed_at) > datetime.fromisoformat(doc["entry_timestamp"])
            and features["ast_direction"] == ("BEAR" if doc["side"] == "LONG" else "BULL")
            and "AST_CURRENT_COMPLETED_BAR_UNAVAILABLE" not in decision["blockers"]
        )
        if reason:
            doc.update(
                mark_price=None,
                gross_mark_rupees=None,
                gross_mark_points=None,
                mark_at=at.isoformat(),
                mark_unavailable_reason=reason,
            )
            if ending or reason != "FUTURES_OBSERVATION_UNAVAILABLE":
                row.state = kind = "UNRESOLVED_EXPOSURE"
                doc["unresolved_reason"] = reason
            else:
                kind = "MARK_UNAVAILABLE"
        else:
            points = (price - Decimal(str(doc["entry_price"]))) * (1 if doc["side"] == "LONG" else -1)
            gross = points * doc["lot_size"]
            doc.update(
                mark_at=at.isoformat(),
                mark_price=float(price),
                gross_mark_points=float(points),
                gross_mark_rupees=float(gross),
                mark_unavailable_reason=None,
                mfe_points=max(doc["mfe_points"], float(points)),
                mae_points=min(doc["mae_points"], float(points)),
            )
            kind = "MARK"
            if ending or row.state == "PENDING_PAPER_EXIT":
                reason = doc.get("pending_exit_reason", "SESSION_END")
                row.state, kind, row.closed_date = "CLOSED", "EXIT", at.date()
                cost = cfg.round_trip_cost_per_lot_inr
                doc.update(
                    exit_timestamp=at.isoformat(),
                    exit_snapshot_id=raw.id,
                    exit_price=float(price),
                    exit_reason=reason,
                    gross_points=float(points),
                    gross_pnl=float(gross),
                    round_trip_cost_inr=cost,
                    net_pnl=None if cost is None else float(gross - Decimal(str(cost))),
                )
        if reversal and row.state == "OPEN":
            row.state = kind = "PENDING_PAPER_EXIT"
            doc.update(
                pending_exit_reason="COMPLETED_5M_REVERSAL",
                reversal_bar_at=reversed_at,
                exit_decision_at=at.isoformat(),
                exit_decision_snapshot_id=raw.id,
            )

    row.document = canonical(doc)
    return kind


def advance(policy: AstPolicy, raw, at, bars, parameters, state, active: AstTrade | None) -> PaperStep:
    decision = decide(
        bars,
        at,
        float(raw.nifty_future) if raw.nifty_future is not None else None,
        parameters,
        state,
        policy,
        future_identity=(raw.future_instrument_id, str(raw.future_expiry)),
    )
    requested, responded = _aware(raw.request_started_at), _aware(raw.response_received_at)
    timing_ok = requested <= responded <= at and (at - requested).total_seconds() <= policy.max_sequence_gap_seconds
    price = (
        raw.nifty_future
        if timing_ok and raw.nifty_future and raw.nifty_future.is_finite() and raw.nifty_future > 0
        else None
    )
    if not timing_ok:
        decision["blockers"].append("AST_OBSERVATION_TIMING_INVALID")
        decision["status"] = "NO_SETUP"
    decision["features"].update(
        futures_source_timestamp=None,
        futures_source_timestamp_status="UNAVAILABLE_IN_SHARED_CAPTURE_SCHEMA",
        futures_observation_time=at.isoformat(),
        futures_observation_time_basis="BROKER_RESPONSE_CAPTURE_TIME",
        snapshot_request_started_at=requested.isoformat(),
        snapshot_response_received_at=responded.isoformat(),
        future_instrument_id=raw.future_instrument_id,
        future_expiry=str(raw.future_expiry),
    )
    signal_opportunity = decision["opportunity_id"] if decision["status"] == "ENTER" else None
    transition = _transition(policy, raw, at, price, active, decision, state) if active is not None else None
    cfg = policy.execution
    if decision["opportunity_id"] in state.get("consumed", []):
        decision["blockers"].append("AST_OPPORTUNITY_CONSUMED")
    if active is not None and active.state in ACTIVE:
        decision["blockers"].append("MAX_OPEN_POSITIONS_REACHED")
    if cfg and at.time() > cfg.entry_end_time:
        decision["blockers"].append("OUTSIDE_ENTRY_WINDOW")
    if not (raw.future_instrument_id or "").strip() or raw.future_expiry is None or raw.future_expiry < at.date():
        decision["blockers"].append("AST_FUTURES_IDENTITY_UNAVAILABLE_OR_EXPIRED")
    if cfg and not valid_lot(raw.lot_size, cfg.lot_size):
        decision["blockers"].append("FUTURES_LOT_SIZE_UNAVAILABLE_OR_MISMATCH")
    trade = None
    if decision["status"] == "ENTER" and not decision["blockers"]:
        assert cfg is not None
        trade_id = digest({"strategy": VERSION, "snapshot_id": raw.id, "opportunity": decision["opportunity_id"]})
        doc = json_value(
            {
                "trade_id": trade_id,
                "strategy_id": VERSION,
                "strategy_version": VERSION,
                "mode": "PAPER",
                "side": "LONG" if decision["direction"] == "BULL" else "SHORT",
                "decision_timestamp": at,
                "decision_snapshot_id": raw.id,
                "strategy_features": decision["features"],
                "session_parameters": parameters,
                "opportunity_id": decision["opportunity_id"],
                "lot_size": cfg.lot_size,
                "decision_observed_lot_size": raw.lot_size,
                "lots": 1,
                "future_instrument_id": raw.future_instrument_id,
                "future_expiry": raw.future_expiry,
                "price_basis": "OBSERVED_FUTURES_LTP",
                "policy_hash": policy.policy_hash,
                "policy": policy.model_dump(mode="json"),
            }
        )
        trade = AstTrade(
            id=trade_id,
            decision_snapshot_id=raw.id,
            trading_date=at.date(),
            state="PENDING_PAPER_ENTRY",
            document=canonical(doc),
        )
        decision["status"] = "PENDING_PAPER_ENTRY"
    if decision["blockers"]:
        decision["status"] = "NO_SETUP"
    return PaperStep(decision, signal_opportunity, transition, trade)
