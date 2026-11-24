"""Persistence for ACTS_V3_MCX_FUTURES: per-instrument hash-chained journal, engine snapshot cursor, trade projection.

`commit_step` writes, in ONE transaction, the events produced by one observation, the engine snapshot taken after
them, the trade projection and the latest status. An optimistic lock_version guard refuses a concurrent second
writer, so a restarted or duplicated worker cannot double-execute: the engine itself rejects observations at or
before the last processed timestamp, and the cursor stores that state atomically with the journal.
"""
from __future__ import annotations

from datetime import date, datetime
import json
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.mcx_futures.models import INSTRUMENTS, McxFuturesCursor, McxFuturesEvent, McxFuturesTrade
from app.research.manifest import canonical, digest, json_value

TRADE_EVENTS = {"ENTRY_PENDING", "ENTRY_FILLED", "ENTRY_REJECTED", "MARK", "MARK_UNAVAILABLE", "EXIT"}
STATE_BY_EVENT = {"ENTRY_PENDING": "PENDING_PAPER_ENTRY", "ENTRY_FILLED": "OPEN", "ENTRY_REJECTED": "ENTRY_REJECTED",
                  "EXIT": "CLOSED"}


class StoreConflict(RuntimeError):
    pass


def _check(instrument: str) -> None:
    if instrument not in INSTRUMENTS:
        raise ValueError("MCX_FUTURES_INSTRUMENT_INVALID")


def load_cursor(sessions: sessionmaker[Session], instrument: str) -> dict[str, Any]:
    _check(instrument)
    with sessions() as session:
        row = session.get(McxFuturesCursor, instrument)
        if row is None:
            raise RuntimeError("MCX_FUTURES_MIGRATION_REQUIRED")
        return {"sequence": row.sequence, "head_hash": row.head_hash, "lock_version": row.lock_version,
                "observed_at": row.observed_at, "engine_version": row.engine_version, "config_hash": row.config_hash,
                "contract": json.loads(row.contract_json), "state": json.loads(row.state_json),
                "latest": json.loads(row.latest_json)}


def _trade_date(at: str) -> date:
    return datetime.fromisoformat(at).date()


def _project(session: Session, instrument: str, event: dict[str, Any], sequence: int) -> None:
    trade_id = event.get("trade_id")
    if not trade_id or event["type"] not in TRADE_EVENTS:
        return
    row = session.get(McxFuturesTrade, trade_id)
    if row is None:
        doc: dict[str, Any] = {"trade_id": trade_id, "instrument": instrument, "mode": "PAPER", "events": []}
        row = McxFuturesTrade(trade_id=trade_id, instrument=instrument, trading_date=_trade_date(event["at"]),
                              state="PENDING_PAPER_ENTRY", document="{}")
        session.add(row)
    else:
        doc = json.loads(row.document)
        if doc.get("instrument") != instrument:
            raise StoreConflict("MCX_FUTURES_TRADE_INSTRUMENT_MISMATCH")
    proj = event.get("projection") or {}
    if event["type"] in ("MARK", "MARK_UNAVAILABLE"):
        doc["last_mark"] = proj
    else:
        doc.update(proj)
        doc["events"].append({"type": event["type"], "at": event["at"], "journal_sequence": sequence})
    doc["journal_sequence"] = sequence
    row.state = STATE_BY_EVENT.get(event["type"], row.state)
    row.document = canonical(json_value(doc))


def commit_step(sessions: sessionmaker[Session], instrument: str, expected_lock: int, events: Iterable[dict[str, Any]],
                *, state: dict[str, Any], latest: dict[str, Any], observed_at: str | None,
                contract: dict[str, Any] | None = None, engine_version: str | None = None,
                config_hash: str | None = None) -> int:
    """Append events + snapshot atomically. Returns the new lock_version; raises StoreConflict on a lost race."""
    _check(instrument)
    events = [json_value(e) for e in events]
    with sessions.begin() as session:
        changed = session.execute(
            update(McxFuturesCursor)
            .where(McxFuturesCursor.instrument == instrument, McxFuturesCursor.lock_version == expected_lock)
            .values(lock_version=McxFuturesCursor.lock_version + 1))
        if changed.rowcount != 1:
            raise StoreConflict("MCX_FUTURES_CONCURRENT_WRITER_OR_STALE_CURSOR")
        cursor = session.get(McxFuturesCursor, instrument)
        assert cursor is not None
        sequence, head = cursor.sequence, cursor.head_hash
        for event in events:
            if event.get("instrument") not in (None, instrument):
                raise StoreConflict("MCX_FUTURES_EVENT_INSTRUMENT_MISMATCH")
            sequence += 1
            payload = canonical({**event, "instrument": instrument})
            event_hash = digest({"instrument": instrument, "sequence": sequence, "previous_hash": head,
                                 "payload": json.loads(payload)})
            session.add(McxFuturesEvent(instrument=instrument, sequence=sequence, event_type=event["type"],
                                        at=event["at"], trade_id=event.get("trade_id"), previous_hash=head,
                                        event_hash=event_hash, payload=payload))
            session.flush()
            _project(session, instrument, event, sequence)
            head = event_hash
        cursor.sequence, cursor.head_hash = sequence, head
        cursor.state_json = canonical(json_value(state))
        cursor.latest_json = canonical(json_value(latest))
        cursor.observed_at = observed_at
        if contract is not None:
            cursor.contract_json = canonical(json_value(contract))
        if engine_version is not None:
            cursor.engine_version = engine_version
        if config_hash is not None:
            cursor.config_hash = config_hash
        return cursor.lock_version


def verify_chain(sessions: sessionmaker[Session], instrument: str) -> dict[str, Any]:
    """Recompute the per-instrument chain; any break is reported, never repaired."""
    _check(instrument)
    with sessions() as session:
        cursor = session.get(McxFuturesCursor, instrument)
        rows = session.scalars(select(McxFuturesEvent).where(McxFuturesEvent.instrument == instrument)
                               .order_by(McxFuturesEvent.sequence)).all()
        head = "GENESIS"
        for expected, row in enumerate(rows, 1):
            recomputed = digest({"instrument": instrument, "sequence": row.sequence, "previous_hash": head,
                                 "payload": json.loads(row.payload)})
            if row.sequence != expected or row.previous_hash != head or row.event_hash != recomputed:
                return {"ok": False, "broken_at": row.sequence}
            head = row.event_hash
        ok = cursor is not None and cursor.sequence == len(rows) and cursor.head_hash == head
        return {"ok": ok, "events": len(rows), "head_hash": head}
