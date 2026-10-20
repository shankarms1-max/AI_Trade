"""Instrument-keyed raw capture and append-only research evidence."""
from datetime import datetime
import json

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.alpha.clock import IST
from app.commodity_acts.config import VERSION
from app.commodity_acts.domain import CommoditySnapshot
from app.commodity_acts.models import (CommodityActsCursor, CommodityActsDecision, CommodityActsEvent,
                                      CommodityMarketSnapshotRecord, CommodityOptionQuoteRecord)
from app.research.manifest import canonical, digest


def bucket(at: datetime) -> datetime:
    local = at.astimezone(IST)
    return local.replace(second=local.second // 15 * 15, microsecond=0)


def capture_key(snapshot: CommoditySnapshot) -> str:
    return digest({"version": VERSION, "instrument_id": snapshot.instrument_id,
                   "session_id": snapshot.session_id, "bucket": bucket(snapshot.timestamp_ist)})


def save_snapshot(sessions, snapshot: CommoditySnapshot, persisted_at: datetime) -> tuple[int, bool]:
    snapshot = CommoditySnapshot.model_validate(snapshot.model_dump())
    snapshot = snapshot.model_copy(update={"snapshot_persisted_at": persisted_at})
    key = capture_key(snapshot)
    try:
        with sessions.begin() as session:
            existing = session.scalar(select(CommodityMarketSnapshotRecord.id).where(CommodityMarketSnapshotRecord.capture_key == key))
            if existing is not None:
                return existing, True
            row = CommodityMarketSnapshotRecord(instrument_id=snapshot.instrument_id, capture_key=key,
                captured_at=snapshot.timestamp_ist, snapshot_json=canonical(snapshot))
            session.add(row)
            session.flush()
            session.add_all([CommodityOptionQuoteRecord(instrument_id=snapshot.instrument_id, snapshot_id=row.id,
                identity_key=digest({"instrument_id": snapshot.instrument_id, "identity": quote.identity}), quote_json=canonical(quote))
                for quote in snapshot.options])
            return row.id, False
    except IntegrityError:
        with sessions() as session:
            found = session.scalar(select(CommodityMarketSnapshotRecord.id).where(CommodityMarketSnapshotRecord.capture_key == key))
            if found is None:
                raise
            return found, True


def locked_cursor(session, instrument: str) -> CommodityActsCursor:
    result = session.execute(update(CommodityActsCursor).where(CommodityActsCursor.instrument_id == instrument).values(
        lock_version=CommodityActsCursor.lock_version + 1))
    if result.rowcount != 1:
        raise RuntimeError("COMMODITY_MIGRATION_REQUIRED")
    cursor = session.get(CommodityActsCursor, instrument)
    assert cursor is not None
    return cursor


def append_event(session, cursor, key: str, kind: str, latest: dict, state: dict, **evidence):
    payload = {"version": VERSION, "instrument_id": cursor.instrument_id, "latest": latest,
               "state": state, **evidence}
    sequence = cursor.sequence + 1
    hashed = digest({"instrument_id": cursor.instrument_id, "sequence": sequence,
                     "event_key": key, "event_type": kind, "previous_hash": cursor.head_hash, "payload": payload})
    session.add(CommodityActsEvent(instrument_id=cursor.instrument_id, sequence=sequence, event_key=key,
        event_type=kind, previous_hash=cursor.head_hash, event_hash=hashed, payload=canonical(payload)))
    cursor.sequence, cursor.head_hash = sequence, hashed
    cursor.latest_json, cursor.state_json = canonical(latest), canonical(state)


def verify_journal(sessions, instrument: str) -> None:
    with sessions() as session:
        cursor = session.get(CommodityActsCursor, instrument)
        if cursor is None:
            raise RuntimeError("COMMODITY_MIGRATION_REQUIRED")
        previous, sequence, latest, state = "GENESIS", 0, {}, {}
        decisions = {}
        for row in session.scalars(select(CommodityActsEvent).where(CommodityActsEvent.instrument_id == instrument).order_by(CommodityActsEvent.sequence)):
            payload = json.loads(row.payload)
            expected = digest({"instrument_id": instrument, "sequence": row.sequence, "event_key": row.event_key,
                "event_type": row.event_type, "previous_hash": previous, "payload": payload})
            if row.sequence != sequence + 1 or row.previous_hash != previous or row.event_hash != expected or payload["instrument_id"] != instrument:
                raise RuntimeError("COMMODITY_JOURNAL_HASH_INVALID")
            if row.event_type == "SHADOW_DECISION":
                snapshot_id = payload["snapshot_id"]
                captured = session.get(CommodityMarketSnapshotRecord, snapshot_id)
                if captured is None or captured.instrument_id != instrument:
                    raise RuntimeError("COMMODITY_JOURNAL_SNAPSHOT_INVALID")
                raw = CommoditySnapshot.model_validate_json(captured.snapshot_json)
                if capture_key(raw) != captured.capture_key or digest(raw) != payload["snapshot_hash"]:
                    raise RuntimeError("COMMODITY_CAPTURE_HASH_INVALID")
                quote_rows = list(session.scalars(select(CommodityOptionQuoteRecord).where(
                    CommodityOptionQuoteRecord.instrument_id == instrument, CommodityOptionQuoteRecord.snapshot_id == snapshot_id)))
                stored_quotes = [quote.quote_json for quote in quote_rows]
                if sorted(stored_quotes) != sorted(canonical(quote) for quote in raw.options):
                    raise RuntimeError("COMMODITY_CAPTURE_QUOTES_INVALID")
                expected_keys = {digest({"instrument_id": instrument, "identity": quote.identity}) for quote in raw.options}
                if {quote.identity_key for quote in quote_rows} != expected_keys:
                    raise RuntimeError("COMMODITY_CAPTURE_QUOTE_IDENTITY_INVALID")
                decisions[snapshot_id] = payload["latest"]
            previous, sequence, latest, state = row.event_hash, row.sequence, payload["latest"], payload["state"]
        stored = {row.snapshot_id: json.loads(row.document) for row in session.scalars(select(CommodityActsDecision).where(CommodityActsDecision.instrument_id == instrument))}
        if (cursor.sequence, cursor.head_hash, json.loads(cursor.latest_json), json.loads(cursor.state_json)) != (sequence, previous, latest, state) or stored != decisions:
            raise RuntimeError("COMMODITY_JOURNAL_PROJECTION_INVALID")
