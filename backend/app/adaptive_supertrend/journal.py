"""AST append-only chain. Bounded startup checks; explicit streaming full verification."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from sqlalchemy import select

from app.adaptive_supertrend import VERSION
from app.adaptive_supertrend.models import AstCursor, AstEvent, AstTrade
from app.research.manifest import canonical, digest, json_value

ACTIVE = ("PENDING_PAPER_ENTRY", "OPEN", "PENDING_PAPER_EXIT", "UNRESOLVED_EXPOSURE")


@dataclass(frozen=True)
class Journal:
    """One strategy's hash chain over its own cursor, event and trade projections."""

    version: str
    cursor_model: Any
    event_model: Any
    trade_model: Any
    active: tuple[str, ...]

    def event_hash(self, sequence: int, key: str, previous: str, payload: dict) -> str:
        return digest(
            {
                "strategy_id": self.version,
                "strategy_version": self.version,
                "sequence": sequence,
                "event_key": key,
                "previous_hash": previous,
                "payload": payload,
            }
        )

    def emit(self, session, cursor, snapshot_id: int, at, kind: str, trade=None, **details) -> None:
        key = f"{snapshot_id}:{trade.id if trade else 'SESSION'}:{kind}"
        payload = json_value(
            {
                "strategy_id": self.version,
                "strategy_version": self.version,
                "mode": "PAPER",
                "event_type": kind,
                "snapshot_id": snapshot_id,
                "timestamp": at,
                "trade_id": trade.id if trade else None,
                "state": trade.state if trade else None,
                "trade_document": json.loads(trade.document) if trade else None,
                **details,
            }
        )
        cursor.sequence += 1
        hashed = self.event_hash(cursor.sequence, key, cursor.head_hash, payload)
        session.add(
            self.event_model(
                sequence=cursor.sequence,
                event_key=key,
                event_type=kind,
                snapshot_id=snapshot_id,
                trade_id=trade.id if trade else None,
                previous_hash=cursor.head_hash,
                event_hash=hashed,
                payload=canonical(payload),
            )
        )
        cursor.head_hash = hashed
        session.flush()

    def validate(self, event) -> dict:
        payload = json.loads(event.payload)
        if (
            event.event_hash != self.event_hash(event.sequence, event.event_key, event.previous_hash, payload)
            or payload.get("strategy_id") != self.version
            or payload.get("strategy_version") != self.version
            or payload.get("mode") != "PAPER"
            or payload.get("snapshot_id") != event.snapshot_id
            or payload.get("trade_id") != event.trade_id
            or payload.get("event_type") != event.event_type
        ):
            raise RuntimeError("AST_JOURNAL_INVALID")
        return payload

    def verify(self, sessions, *, full: bool = False) -> None:
        Cursor, Event, Trade = self.cursor_model, self.event_model, self.trade_model
        with sessions() as session:
            cursor = session.get(Cursor, 1)
            if cursor is None:
                raise RuntimeError("AST_MIGRATION_REQUIRED")
            tail = session.scalar(select(Event).order_by(Event.sequence.desc()).limit(1))
            if (cursor.sequence, cursor.head_hash) != ((tail.sequence, tail.event_hash) if tail else (0, "GENESIS")):
                raise RuntimeError("AST_CURSOR_HEAD_INVALID")
            if tail:
                self.validate(tail)
            observation = session.scalar(
                select(Event).where(Event.event_type == "OBSERVATION").order_by(Event.sequence.desc()).limit(1)
            )
            if observation:
                payload = self.validate(observation)
                if (
                    cursor.snapshot_id != observation.snapshot_id
                    or cursor.observed_at != payload["timestamp"]
                    or json.loads(cursor.state_json) != payload["strategy_state"]
                    or json.loads(cursor.latest_json) != payload["decision"]
                ):
                    raise RuntimeError("AST_CURSOR_PROJECTION_INVALID")
            query = select(Trade) if full else select(Trade).where(Trade.state.in_(self.active))
            for row in session.scalars(query.execution_options(yield_per=100)):
                doc = json.loads(row.document)
                last = session.scalar(
                    select(Event).where(Event.trade_id == row.id).order_by(Event.sequence.desc()).limit(1)
                )
                payload = self.validate(last) if last else {}
                if (
                    payload.get("trade_document") != doc
                    or payload.get("state") != row.state
                    or doc.get("strategy_id") != self.version
                    or doc.get("trade_id") != row.id
                    or doc.get("policy_hash") != digest(doc.get("policy"))
                ):
                    raise RuntimeError("AST_TRADE_PROJECTION_INVALID")
            if full:
                orphan = session.scalar(
                    select(Event.sequence)
                    .outerjoin(Trade, Event.trade_id == Trade.id)
                    .where(Event.trade_id.is_not(None), Trade.id.is_(None))
                    .limit(1)
                )
                if orphan is not None:
                    raise RuntimeError("AST_TRADE_PROJECTION_MISSING")
                previous, count = "GENESIS", 0
                for event in session.scalars(select(Event).order_by(Event.sequence).execution_options(yield_per=100)):
                    self.validate(event)
                    count += 1
                    if event.sequence != count or event.previous_hash != previous:
                        raise RuntimeError("AST_JOURNAL_CHAIN_INVALID")
                    previous = event.event_hash
                if count != cursor.sequence:
                    raise RuntimeError("AST_JOURNAL_COUNT_INVALID")


FUTURES_JOURNAL = Journal(VERSION, AstCursor, AstEvent, AstTrade, ACTIVE)


def event_hash(sequence: int, key: str, previous: str, payload: dict) -> str:
    return FUTURES_JOURNAL.event_hash(sequence, key, previous, payload)


def emit(
    session, cursor: AstCursor, snapshot_id: int, at, kind: str, trade: AstTrade | None = None, **details
) -> None:
    FUTURES_JOURNAL.emit(session, cursor, snapshot_id, at, kind, trade, **details)


def _validate(event: AstEvent) -> dict:
    return FUTURES_JOURNAL.validate(event)


def verify(sessions, *, full: bool = False) -> None:
    FUTURES_JOURNAL.verify(sessions, full=full)
