"""Transactional commodity paper journal. Projections are never the source of truth."""
import json

from sqlalchemy import select

from app.commodity_acts.domain import CommoditySnapshot
from app.commodity_acts.models import CommodityMarketSnapshotRecord
from app.commodity_acts.paper_models import (CommodityPaperControl, CommodityPaperCursor, CommodityPaperEvent, CommodityPaperFill,
                                           CommodityPaperOrder, CommodityPaperPosition)
from app.research.manifest import canonical, digest, json_value

PROJECTIONS = {"position": CommodityPaperPosition, "order": CommodityPaperOrder, "fill": CommodityPaperFill}


def emit(session, cursor, kind, snapshot_id, snapshot, processed_at, *, changes=(), **details):
    rows = [{"table": key, "id": row.id, "instrument_id": row.instrument_id,
             "observed_at": row.observed_at,
             "document": json.loads(row.document),
             **({"state": row.state} if hasattr(row, "state") else {}),
             **({"position_id": row.position_id} if hasattr(row, "position_id") else {}),
             **({"order_id": row.order_id} if hasattr(row, "order_id") else {})} for key, row in changes]
    payload = json_value({"kind": kind, "snapshot_id": snapshot_id, "instrument_id": cursor.instrument_id,
        "observed_at": snapshot.timestamp_ist, "processed_at": processed_at, "changes": rows, **details})
    sequence = cursor.sequence + 1
    key = digest({"instrument_id": cursor.instrument_id, "snapshot_id": snapshot_id, "sequence": sequence, "kind": kind})
    envelope = {"instrument_id": cursor.instrument_id, "sequence": sequence, "event_key": key,
                "event_type": kind, "previous_hash": cursor.head_hash, "payload": payload}
    hashed = digest(envelope)
    session.add(CommodityPaperEvent(instrument_id=cursor.instrument_id, sequence=sequence, event_key=key,
        event_type=kind, previous_hash=cursor.head_hash, event_hash=hashed, payload=canonical(payload)))
    cursor.sequence, cursor.head_hash = sequence, hashed
    if kind == "PAPER_OBSERVATION":
        cursor.snapshot_id, cursor.observed_at = snapshot_id, snapshot.timestamp_ist.isoformat()
        cursor.state_json, cursor.latest_json = canonical(details["state"]), canonical(details["latest"])


def verify(sessions, instrument):
    with sessions() as session:
        cursor = session.get(CommodityPaperCursor, instrument)
        if cursor is None:
            raise RuntimeError("COMMODITY_PAPER_MIGRATION_REQUIRED")
        previous, sequence, observation = "GENESIS", 0, None
        global_sequence, previous_snapshot = 0, None
        expected_rows = {key: {} for key in PROJECTIONS}
        for row in session.scalars(select(CommodityPaperEvent).where(CommodityPaperEvent.instrument_id == instrument).order_by(
                CommodityPaperEvent.sequence)):
            payload = json.loads(row.payload)
            envelope = {"instrument_id": instrument, "sequence": row.sequence, "event_key": row.event_key,
                        "event_type": row.event_type, "previous_hash": previous, "payload": payload}
            if (row.sequence != sequence + 1 or row.previous_hash != previous or digest(envelope) != row.event_hash
                    or payload["instrument_id"] != instrument or payload["kind"] != row.event_type):
                raise RuntimeError("COMMODITY_PAPER_JOURNAL_INVALID")
            if (payload["global_sequence"] < global_sequence or payload["global_sequence"] <= 0
                    or payload["global_sequence"] == global_sequence and payload["snapshot_id"] != previous_snapshot):
                raise RuntimeError("COMMODITY_PAPER_GLOBAL_SEQUENCE_INVALID")
            global_sequence, previous_snapshot = payload["global_sequence"], payload["snapshot_id"]
            for change in payload["changes"]:
                if change["instrument_id"] != instrument:
                    raise RuntimeError("COMMODITY_PAPER_PROJECTION_OWNER_INVALID")
                expected_rows[change["table"]][change["id"]] = change
            if row.event_type == "PAPER_OBSERVATION":
                observation = payload
                raw = session.get(CommodityMarketSnapshotRecord, payload["snapshot_id"])
                if raw is None or raw.instrument_id != instrument or digest(CommoditySnapshot.model_validate_json(raw.snapshot_json)) != payload["snapshot_hash"]:
                    raise RuntimeError("COMMODITY_PAPER_CAPTURE_INVALID")
            previous, sequence = row.event_hash, row.sequence
        if (cursor.sequence, cursor.head_hash) != (sequence, previous):
            raise RuntimeError("COMMODITY_PAPER_CURSOR_INVALID")
        if observation is not None and (cursor.snapshot_id, cursor.observed_at, json.loads(cursor.state_json), json.loads(cursor.latest_json)) != (
                observation["snapshot_id"], observation["observed_at"], observation["state"], observation["latest"]):
            raise RuntimeError("COMMODITY_PAPER_CURSOR_PROJECTION_INVALID")
        if observation is None and (cursor.snapshot_id is not None or cursor.observed_at is not None
                                    or cursor.state_json != "{}" or cursor.latest_json != "{}"):
            raise RuntimeError("COMMODITY_PAPER_CURSOR_PROJECTION_INVALID")
        for key, model in PROJECTIONS.items():
            rows = list(session.scalars(select(model).where(model.instrument_id == instrument)))
            if {row.id for row in rows} != set(expected_rows[key]):
                raise RuntimeError("COMMODITY_PAPER_PROJECTION_INVALID")
            for row in rows:
                expected = expected_rows[key][row.id]
                if json.loads(row.document) != expected["document"] or any(
                        getattr(row, field) != expected[field] for field in ("state", "position_id", "order_id", "observed_at") if field in expected):
                    raise RuntimeError("COMMODITY_PAPER_PROJECTION_INVALID")
        policies = {canonical(json.loads(row.document)["global_policy"]) for row in session.scalars(select(CommodityPaperPosition))}
        control = session.get(CommodityPaperControl, 1)
        if (control is None or control.lock_version < global_sequence
                or (policies and policies != {control.policy_json}) or (not policies and control.policy_json != "{}")):
            raise RuntimeError("COMMODITY_PAPER_GLOBAL_POLICY_INVALID")
