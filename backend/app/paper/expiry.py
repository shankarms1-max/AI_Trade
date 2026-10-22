"""Paper-only expiry lifecycle. Terminal evidence is never an executable fill."""
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.scalper.models import ScalperCandidate

VERSION = "PAPER_EXPIRY_TERMINALIZATION_V1"
ACTIVE = ("PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE")
LABEL = "CONTRACT EXPIRED / EXIT PRICE UNAVAILABLE"
IST = ZoneInfo("Asia/Kolkata")


def is_unpriced(doc: dict[str, Any]) -> bool:
    return doc.get("expiry_terminalization") is True and doc.get("final_outcome") is None


def expired(doc: dict[str, Any], at: datetime) -> bool:
    candidate = ScalperCandidate.model_validate(doc["candidate"])
    if candidate.long_leg.expiry != candidate.short_leg.expiry:
        raise ValueError("PAPER_EXPIRY_LEG_MISMATCH")
    return candidate.short_leg.expiry < at.astimezone(IST).date()


def terminalize(state: str, doc: dict[str, Any], at: datetime, snapshot_id: int) -> str | None:
    if state not in ACTIVE:
        return None
    candidate = ScalperCandidate.model_validate(doc["candidate"])
    if not expired(doc, at):
        return None
    if doc.get("final_outcome") is not None or doc.get("exit_timestamp") is not None:
        raise ValueError("PAPER_ACTIVE_TRADE_ALREADY_HAS_EXIT_EVIDENCE")
    doc.update(expiry_lifecycle_version=VERSION, terminalized_at=at.isoformat(),
               terminalization_snapshot_id=snapshot_id)
    if state == "PENDING_PAPER_ENTRY":
        doc["rejection_reason"] = "CONTRACT_EXPIRED_BEFORE_FILL"
        return "ENTRY_REJECTED"
    doc.update(expiry_terminalization=True, exit_reason="CONTRACT_EXPIRED_UNPRICED",
               expiry_date=candidate.short_leg.expiry.isoformat(),
               exit_pricing_status="UNAVAILABLE", exit_debit=None, exit_quotes=None,
               exit_timestamp=None, exit_snapshot_id=None, final_outcome=None, gross_pnl=None,
               previous_unresolved_reason=doc.get("unresolved_reason"),
               current_debit=None, gross_mark_points=None, gross_mark_rupees=None,
               mark_quality_warning=None)
    return "CLOSED"
