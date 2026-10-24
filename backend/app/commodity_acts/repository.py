"""Instrument-keyed raw capture and append-only research evidence."""

from datetime import datetime
import json

from sqlalchemy import JSON, bindparam, cast, func, select, type_coerce, update
from sqlalchemy.exc import IntegrityError

from app.alpha.clock import IST
from app.commodity_acts.config import VERSION
from app.commodity_acts.domain import CommoditySnapshot
from app.commodity_acts.models import (
    CommodityActsCursor,
    CommodityActsDecision,
    CommodityActsEvent,
    CommodityMarketSnapshotRecord,
    CommodityOptionQuoteRecord,
)
from app.research.manifest import canonical, digest


def bucket(at: datetime) -> datetime:
    local = at.astimezone(IST)
    return local.replace(second=local.second // 15 * 15, microsecond=0)


def capture_key(snapshot: CommoditySnapshot) -> str:
    return digest(
        {
            "version": VERSION,
            "instrument_id": snapshot.instrument_id,
            "session_id": snapshot.session_id,
            "bucket": bucket(snapshot.timestamp_ist),
        }
    )


def save_snapshot(sessions, snapshot: CommoditySnapshot, persisted_at: datetime) -> tuple[int, bool]:
    snapshot = CommoditySnapshot.model_validate(snapshot.model_dump())
    snapshot = snapshot.model_copy(update={"snapshot_persisted_at": persisted_at})
    key = capture_key(snapshot)
    try:
        with sessions.begin() as session:
            existing = session.scalar(
                select(CommodityMarketSnapshotRecord.id).where(CommodityMarketSnapshotRecord.capture_key == key)
            )
            if existing is not None:
                return existing, True
            row = CommodityMarketSnapshotRecord(
                instrument_id=snapshot.instrument_id,
                capture_key=key,
                captured_at=snapshot.timestamp_ist,
                snapshot_json=canonical(snapshot),
            )
            session.add(row)
            session.flush()
            session.add_all(
                [
                    CommodityOptionQuoteRecord(
                        instrument_id=snapshot.instrument_id,
                        snapshot_id=row.id,
                        identity_key=digest({"instrument_id": snapshot.instrument_id, "identity": quote.identity}),
                        quote_json=canonical(quote),
                    )
                    for quote in snapshot.options
                ]
            )
            return row.id, False
    except IntegrityError:
        with sessions() as session:
            found = session.scalar(
                select(CommodityMarketSnapshotRecord.id).where(CommodityMarketSnapshotRecord.capture_key == key)
            )
            if found is None:
                raise
            return found, True


def locked_cursor(session, instrument: str) -> CommodityActsCursor:
    result = session.execute(
        update(CommodityActsCursor)
        .where(CommodityActsCursor.instrument_id == instrument)
        .values(lock_version=CommodityActsCursor.lock_version + 1)
    )
    if result.rowcount != 1:
        raise RuntimeError("COMMODITY_MIGRATION_REQUIRED")
    cursor = session.get(CommodityActsCursor, instrument)
    assert cursor is not None
    return cursor


def append_event(session, cursor, key: str, kind: str, latest: dict, state: dict, **evidence):
    payload = {"version": VERSION, "instrument_id": cursor.instrument_id, "latest": latest, "state": state, **evidence}
    sequence = cursor.sequence + 1
    hashed = digest(
        {
            "instrument_id": cursor.instrument_id,
            "sequence": sequence,
            "event_key": key,
            "event_type": kind,
            "previous_hash": cursor.head_hash,
            "payload": payload,
        }
    )
    session.add(
        CommodityActsEvent(
            instrument_id=cursor.instrument_id,
            sequence=sequence,
            event_key=key,
            event_type=kind,
            previous_hash=cursor.head_hash,
            event_hash=hashed,
            payload=canonical(payload),
        )
    )
    cursor.sequence, cursor.head_hash = sequence, hashed
    cursor.latest_json, cursor.state_json = canonical(latest), canonical(state)


def verify_journal(sessions, instrument: str) -> None:
    with sessions() as session:
        connection = session.connection()
        cursor = connection.execute(
            select(CommodityActsCursor.sequence, CommodityActsCursor.head_hash,
                   CommodityActsCursor.latest_json, CommodityActsCursor.state_json)
            .where(CommodityActsCursor.instrument_id == instrument)
        ).first()
        if cursor is None:
            raise RuntimeError("COMMODITY_MIGRATION_REQUIRED")
        # Stream one historical payload at a time, including at the DB-driver
        # level on PostgreSQL. Column rows never enter the ORM identity map.
        events = connection.execute(
            select(CommodityActsEvent.sequence, CommodityActsEvent.event_key,
                   CommodityActsEvent.event_type, CommodityActsEvent.previous_hash,
                   CommodityActsEvent.event_hash, CommodityActsEvent.payload)
            .where(CommodityActsEvent.instrument_id == instrument)
            .order_by(CommodityActsEvent.sequence)
            .execution_options(yield_per=1)
        )
        try:
            _verify_events(connection, events, instrument, cursor)
        finally:
            events.close()


def _verify_events(connection, events, instrument, cursor) -> None:
    previous, sequence, latest, state = "GENESIS", 0, {}, {}
    # Reuse Core statements: no per-event ORM result contexts or query graphs.
    snapshot_query = select(
        CommodityMarketSnapshotRecord.instrument_id, CommodityMarketSnapshotRecord.capture_key,
        CommodityMarketSnapshotRecord.snapshot_json,
    ).where(CommodityMarketSnapshotRecord.id == bindparam("snapshot_id"))
    quotes_query = select(CommodityOptionQuoteRecord.quote_json, CommodityOptionQuoteRecord.identity_key).where(
        CommodityOptionQuoteRecord.instrument_id == instrument,
        CommodityOptionQuoteRecord.snapshot_id == bindparam("snapshot_id"),
    )
    decision_query = select(CommodityActsDecision.document).where(
        CommodityActsDecision.instrument_id == instrument,
        CommodityActsDecision.snapshot_id == bindparam("snapshot_id"),
    )
    for row in events:
        payload = json.loads(row.payload)
        expected = digest(
            {
                "instrument_id": instrument,
                "sequence": row.sequence,
                "event_key": row.event_key,
                "event_type": row.event_type,
                "previous_hash": previous,
                "payload": payload,
            }
        )
        if (
            row.sequence != sequence + 1
            or row.previous_hash != previous
            or row.event_hash != expected
            or payload["instrument_id"] != instrument
        ):
            raise RuntimeError("COMMODITY_JOURNAL_HASH_INVALID")
        if row.event_type == "SHADOW_DECISION":
            snapshot_id = payload["snapshot_id"]
            parameters = {"snapshot_id": snapshot_id}
            captured = connection.execute(snapshot_query, parameters).first()
            if captured is None or captured.instrument_id != instrument:
                raise RuntimeError("COMMODITY_JOURNAL_SNAPSHOT_INVALID")
            raw = CommoditySnapshot.model_validate_json(captured.snapshot_json)
            if capture_key(raw) != captured.capture_key or digest(raw) != payload["snapshot_hash"]:
                raise RuntimeError("COMMODITY_CAPTURE_HASH_INVALID")
            with connection.execute(quotes_query, parameters) as quotes:
                quote_rows = list(quotes)  # Only the current snapshot's chain, never journal history.
            stored_quotes = [quote.quote_json for quote in quote_rows]
            if sorted(stored_quotes) != sorted(canonical(quote) for quote in raw.options):
                raise RuntimeError("COMMODITY_CAPTURE_QUOTES_INVALID")
            expected_keys = {
                digest({"instrument_id": instrument, "identity": quote.identity}) for quote in raw.options
            }
            if {quote.identity_key for quote in quote_rows} != expected_keys:
                raise RuntimeError("COMMODITY_CAPTURE_QUOTE_IDENTITY_INVALID")
            document = connection.execute(decision_query, parameters).scalar()
            if document is None or json.loads(document) != payload["latest"]:
                raise RuntimeError("COMMODITY_JOURNAL_PROJECTION_INVALID")
        previous, sequence, latest, state = row.event_hash, row.sequence, payload["latest"], payload["state"]
    # Every referenced projection was checked above. Compare distinct snapshot
    # counts in SQL to detect extras without retaining a Python set/dict. DISTINCT
    # also prevents repeated snapshot references from masking an extra projection.
    payload_column = CommodityActsEvent.payload
    document_json = (cast(payload_column, JSON) if connection.dialect.name == "postgresql"
                     else type_coerce(payload_column, JSON))
    expected_count = connection.scalar(
        select(func.count(func.distinct(document_json["snapshot_id"].as_integer()))).where(
            CommodityActsEvent.instrument_id == instrument, CommodityActsEvent.event_type == "SHADOW_DECISION")
    )
    stored_count = connection.scalar(
        select(func.count()).select_from(CommodityActsDecision).where(CommodityActsDecision.instrument_id == instrument)
    )
    if (cursor.sequence, cursor.head_hash, json.loads(cursor.latest_json), json.loads(cursor.state_json)) != (
        sequence,
        previous,
        latest,
        state,
    ) or stored_count != expected_count:
        raise RuntimeError("COMMODITY_JOURNAL_PROJECTION_INVALID")
