"""Recoverable AST_FUTURES_V1_DIRECT PAPER service. One atomic, hash-chained transaction per observation."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
from types import SimpleNamespace

from sqlalchemy import and_, select, update

from app.adaptive_supertrend import BOOTSTRAP_MODE, CAPTURE_MODE as AST_CAPTURE_MODE
from app.adaptive_supertrend.direct import CAPTURE_MODE, VERSION
from app.adaptive_supertrend.direct.engine import ACTIVE, BarEval, Observation, evaluate, step
from app.adaptive_supertrend.direct.models import DirectCursor, DirectEvent, DirectTrade
from app.adaptive_supertrend.direct.policy import DirectPolicy
from app.adaptive_supertrend.journal import Journal
from app.adaptive_supertrend.market import history
from app.research.manifest import canonical
from app.scalper.models import ScalperMarketSnapshotRecord as Snapshot
from app.scalper.repository import IST, _aware

JOURNAL = Journal(VERSION, DirectCursor, DirectEvent, DirectTrade, ACTIVE)
# Today's indicator and signal input is the direct worker's own native capture only. Earlier sessions may be
# seeded from the shared AST capture or the validated historical bootstrap, never mixed within one date.
HISTORY_MODES = (CAPTURE_MODE, AST_CAPTURE_MODE, BOOTSTRAP_MODE)


def paper_clause():
    return and_(
        Snapshot.context_json.like(f'%"mode":"{CAPTURE_MODE}"%'),
        Snapshot.context_json.like(f'%"producer":"{VERSION}"%'),
    )


def pending_ids(sessions, after: datetime | None, limit: int = 200) -> list[int]:
    with sessions() as session:
        query = select(Snapshot.id).where(paper_clause())
        if after is not None:
            query = query.where(Snapshot.captured_at > after)
        return list(session.scalars(query.order_by(Snapshot.captured_at, Snapshot.id).limit(limit)))


def _lock(session):
    cursor = session.scalar(select(DirectCursor).where(DirectCursor.id == 1).with_for_update())
    if cursor is None:
        raise RuntimeError("AST_MIGRATION_REQUIRED")
    version = cursor.lock_version
    changed = session.execute(
        update(DirectCursor)
        .where(DirectCursor.id == 1, DirectCursor.lock_version == version)
        .values(lock_version=version + 1)
    )
    if changed.rowcount != 1:
        raise RuntimeError("AST_CONCURRENT_WRITER")
    return cursor


def _ref(doc: dict) -> SimpleNamespace:
    return SimpleNamespace(id=doc["trade_id"], state=doc["state"], document=canonical(doc))


class DirectPaperService:
    def __init__(self, sessions, policy: DirectPolicy) -> None:
        self.sessions = sessions
        self.policy = policy
        self._history_cache: dict = {}
        JOURNAL.verify(sessions)
        with sessions() as session:
            self._active(session)

    def _active(self, session) -> DirectTrade | None:
        rows = list(session.scalars(select(DirectTrade).where(DirectTrade.state.in_(ACTIVE))))
        if len(rows) > 1:
            raise RuntimeError("AST_DIRECT_MULTIPLE_ACTIVE_POSITIONS")
        active = rows[0] if rows else None
        if active is not None and json.loads(active.document)["policy_hash"] != self.policy.policy_hash:
            raise RuntimeError("AST_DIRECT_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES")
        return active

    def session_bars(self, at: datetime, identity: tuple) -> list[BarEval]:
        """Causal AST over prior sessions plus today's native prefix; returns today's evaluated bars."""
        gap = self.policy.max_sequence_gap_seconds
        key = (at.date(), identity)
        if key not in self._history_cache:
            loaded = history(
                self.sessions, at, self.policy.history_sessions + 1, gap, identity, modes=HISTORY_MODES
            )
            self._history_cache = {key: [bar for bar in loaded if bar.start.astimezone(IST).date() < at.date()]}
        today = history(self.sessions, at, 1, gap, identity, current_only=True, modes=HISTORY_MODES)
        evaluated = evaluate([*self._history_cache[key], *today])
        return [e for e in evaluated if e.bar.start.astimezone(IST).date() == at.date()]

    def process_snapshot(self, snapshot_id: int, *, kill_switch: bool = False) -> dict:
        with self.sessions() as session:
            record = session.get(Snapshot, snapshot_id)
            if record is None:
                raise ValueError("AST_DIRECT_SNAPSHOT_UNAVAILABLE")
            context = json.loads(record.context_json or "{}")
            if context.get("mode") != CAPTURE_MODE or context.get("producer") != VERSION:
                raise ValueError("AST_DIRECT_FOREIGN_CAPTURE")
            at = _aware(record.captured_at).astimezone(IST)
            cursor = session.get(DirectCursor, 1)
            if cursor is None:
                raise RuntimeError("AST_MIGRATION_REQUIRED")
            if cursor.observed_at and at <= datetime.fromisoformat(cursor.observed_at):
                return {"status": "DUPLICATE", "snapshot_id": snapshot_id}
            requested, responded = _aware(record.request_started_at), _aware(record.response_received_at)
            price = Decimal(str(record.nifty_future)) if record.nifty_future is not None else None
            obs = Observation(
                snapshot_id=snapshot_id,
                at=at,
                price=price,
                future_id=(record.future_instrument_id or "").strip() or None,
                future_expiry=record.future_expiry,
                lot_size=record.lot_size,
                timing_ok=requested <= responded <= at
                and (at - requested).total_seconds() <= self.policy.max_sequence_gap_seconds,
            )
        completed = self.session_bars(at, (obs.future_id, obs.future_expiry))
        with self.sessions.begin() as session:
            cursor = _lock(session)
            if cursor.observed_at is not None and at <= datetime.fromisoformat(cursor.observed_at):
                return {"status": "DUPLICATE", "snapshot_id": snapshot_id}
            after = datetime.fromisoformat(cursor.observed_at) if cursor.observed_at else None
            query = select(Snapshot.id).where(paper_clause())
            if after is not None:
                query = query.where(Snapshot.captured_at > after)
            # A skipped native observation must never be substituted by a later one.
            if session.scalar(query.order_by(Snapshot.captured_at, Snapshot.id).limit(1)) != snapshot_id:
                raise RuntimeError("AST_EARLIER_OBSERVATION_PENDING")
            state = json.loads(cursor.state_json)
            row = self._active(session)
            result = step(
                self.policy,
                state,
                json.loads(row.document) if row is not None else None,
                obs,
                completed,
                kill_switch=kill_switch,
            )
            if result.created is not None:
                first = next(doc for _, doc in result.events if doc["trade_id"] == result.created["trade_id"])
                session.add(
                    DirectTrade(
                        id=first["trade_id"],
                        decision_snapshot_id=snapshot_id,
                        trading_date=date.fromisoformat(first["trading_date"]),
                        state=first["state"],
                        document=canonical(first),
                    )
                )
                session.flush()
            for kind, event_doc in result.events:
                JOURNAL.emit(session, cursor, snapshot_id, at, kind, _ref(event_doc))
            for doc in (result.active, result.created):
                if doc is None:
                    continue
                trade = session.get(DirectTrade, doc["trade_id"])
                assert trade is not None
                trade.state = doc["state"]
                trade.document = canonical(doc)
                trade.closed_date = date.fromisoformat(doc["closed_date"]) if doc.get("closed_date") else None
            cursor.state_json = canonical(state)
            cursor.snapshot_id, cursor.observed_at = snapshot_id, at.isoformat()
            cursor.latest_json = canonical(result.decision)
            JOURNAL.emit(
                session,
                cursor,
                snapshot_id,
                at,
                "OBSERVATION",
                decision=result.decision,
                strategy_state=state,
                policy_hash=self.policy.policy_hash,
            )
            return result.decision

    def pending_snapshot_ids(self, limit: int = 200) -> list[int]:
        with self.sessions() as session:
            cursor = session.get(DirectCursor, 1)
            after = datetime.fromisoformat(cursor.observed_at) if cursor and cursor.observed_at else None
        return pending_ids(self.sessions, after, limit)

    def acknowledge_unresolved(self, trade_id: str, note: str) -> dict:
        """Operator acknowledgement of an unpriced paper exposure. It never invents an exit price or P&L."""
        if not note.strip():
            raise ValueError("AST_DIRECT_ACKNOWLEDGEMENT_NOTE_REQUIRED")
        with self.sessions.begin() as session:
            cursor = _lock(session)
            trade = session.get(DirectTrade, trade_id)
            if trade is None or trade.state != "UNRESOLVED_EXPOSURE":
                raise ValueError("AST_DIRECT_UNRESOLVED_TRADE_REQUIRED")
            if cursor.snapshot_id is None:
                raise RuntimeError("AST_DIRECT_NO_OBSERVATION_FOR_ACKNOWLEDGEMENT")
            doc = json.loads(trade.document)
            doc.update(
                state="UNRESOLVED_ACKNOWLEDGED",
                acknowledged_at=datetime.now(IST).isoformat(),
                acknowledgement_note=note.strip(),
                gross_points=None,
            )
            trade.state, trade.document = doc["state"], canonical(doc)
            JOURNAL.emit(
                session,
                cursor,
                cursor.snapshot_id,
                datetime.now(IST),
                "UNRESOLVED_ACKNOWLEDGED",
                _ref(doc),
            )
            return doc
