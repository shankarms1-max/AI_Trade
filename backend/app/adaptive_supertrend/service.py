"""Standalone one-lot AST paper lifecycle; shared utilities have no order adapter."""

from __future__ import annotations

from datetime import date, datetime, time
import json

from sqlalchemy import select, update

from app.adaptive_supertrend import CAPTURE_MODE, VERSION
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.lifecycle import advance
from app.adaptive_supertrend.journal import ACTIVE, emit, verify
from app.adaptive_supertrend.market import history, paper_clause, pending_ids
from app.adaptive_supertrend.models import AstCursor, AstTrade, OptimizerRun, SessionParameters
from app.adaptive_supertrend.optimizer import optimize, selected_parameters
from app.research.manifest import canonical, digest, json_value
from app.scalper.models import ScalperMarketSnapshotRecord as Snapshot
from app.scalper.repository import IST, _aware


def freeze(sessions, bars, trading_date: date, selected_at: datetime, policy: AstPolicy, *, clock=None) -> dict:
    """Insert once, before session. Existing parameters are never silently replaced."""
    with sessions() as session:
        existing = session.get(SessionParameters, trading_date)
        if existing is not None:
            document = json.loads(existing.document)
            if document["policy_hash"] != policy.policy_hash:
                raise RuntimeError("AST_SESSION_ALREADY_FROZEN_DIFFERENT_POLICY")
            return document
    result = optimize(bars, trading_date, selected_at, policy)
    provenance = getattr(bars, "history_provenance", None)
    if provenance is not None:
        result["history_provenance"] = json_value(provenance)
    if result["selected"] is None:
        raise RuntimeError("AST_NO_ELIGIBLE_OPTIMIZER_CONFIGURATION")
    if clock is not None:
        completed_at = clock()
        if completed_at >= datetime.combine(trading_date, time(9, 15), IST):
            raise RuntimeError("AST_PARAMETER_SELECTION_COMPLETED_AFTER_CUTOFF")
        result["selected_at"] = completed_at.isoformat()
    document = selected_parameters(result)
    run_id = document["optimizer_run_id"]
    with sessions.begin() as session:
        # Same lock used by paper processing serializes concurrent freezes, including SQLite CAS.
        cursor = session.scalar(select(AstCursor).where(AstCursor.id == 1).with_for_update())
        if cursor is None:
            raise RuntimeError("AST_MIGRATION_REQUIRED")
        version = cursor.lock_version
        changed = session.execute(
            update(AstCursor)
            .where(AstCursor.id == 1, AstCursor.lock_version == version)
            .values(lock_version=version + 1)
        )
        if changed.rowcount != 1:
            raise RuntimeError("AST_CONCURRENT_WRITER")
        existing = session.get(SessionParameters, trading_date)
        if existing:
            if json.loads(existing.document) != document:
                raise RuntimeError("AST_SESSION_ALREADY_FROZEN")
            return document
        if clock is not None and clock() >= datetime.combine(trading_date, time(9, 15), IST):
            raise RuntimeError("AST_PARAMETER_SELECTION_COMPLETED_AFTER_CUTOFF")
        session.add(OptimizerRun(id=run_id, document=canonical(result)))
        session.flush()
        session.add(
            SessionParameters(trading_date=trading_date, optimizer_run_id=run_id, document=canonical(document))
        )
    return document


class AstPaperService:
    def __init__(self, sessions, settings) -> None:
        self.sessions = sessions
        self.policy = AstPolicy.model_validate_json(settings.ast_config_json)
        self._history_cache: dict = {}
        self._parameter_cache: dict = {}
        verify(sessions)
        with sessions() as session:
            self._active(session)

    def _active(self, session):
        active = session.scalars(select(AstTrade).where(AstTrade.state.in_(ACTIVE))).one_or_none()
        if active is not None and json.loads(active.document)["policy_hash"] != self.policy.policy_hash:
            raise RuntimeError("AST_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES")
        return active

    def process_snapshot(self, snapshot_id: int) -> str:
        with self.sessions() as session:
            record = session.get(Snapshot, snapshot_id)
            if record is None:
                raise ValueError("AST_SNAPSHOT_UNAVAILABLE")
            context = json.loads(record.context_json or "{}")
            if context.get("mode") != CAPTURE_MODE or context.get("producer") != VERSION:
                raise ValueError("AST_FOREIGN_CAPTURE_MODE")
            raw = record
            at = _aware(raw.captured_at)
            cursor = session.get(AstCursor, 1)
            if cursor is not None and cursor.observed_at and at <= datetime.fromisoformat(cursor.observed_at):
                return "DUPLICATE"
        lookback = self.policy.optimizer.lookback_sessions + 1 if self.policy.optimizer else 1
        identity = (raw.future_instrument_id, raw.future_expiry)
        cache_key = (at.date(), identity)
        if cache_key not in self._history_cache:
            loaded = history(
                self.sessions,
                at,
                lookback,
                self.policy.max_sequence_gap_seconds,
                identity,
            )
            self._history_cache = {cache_key: [bar for bar in loaded if bar.start.date() < at.date()]}
        bars = [
            *self._history_cache[cache_key],
            *history(
                self.sessions,
                at,
                1,
                self.policy.max_sequence_gap_seconds,
                identity,
                current_only=True,
            ),
        ]
        with self.sessions.begin() as session:
            cursor = session.scalar(select(AstCursor).where(AstCursor.id == 1).with_for_update())
            if cursor is None:
                raise RuntimeError("AST_MIGRATION_REQUIRED")
            if cursor.observed_at is not None and at <= datetime.fromisoformat(cursor.observed_at):
                return "DUPLICATE"
            version = cursor.lock_version
            changed = session.execute(
                update(AstCursor)
                .where(AstCursor.id == 1, AstCursor.lock_version == version)
                .values(lock_version=version + 1)
            )
            if changed.rowcount != 1:
                raise RuntimeError("AST_CONCURRENT_WRITER")
            parameters = self._parameter_cache.get(at.date())
            if parameters is None:
                frozen = session.get(SessionParameters, at.date())
                parameters = json.loads(frozen.document) if frozen else None
                if parameters and parameters.get("strategy_version") != VERSION:
                    parameters = None
                if parameters is not None:
                    cutoff = at.replace(hour=9, minute=15, second=0, microsecond=0)
                    selected_at = datetime.fromisoformat(parameters["selected_at"])
                    data_cutoff = datetime.fromisoformat(parameters["data_cutoff"])
                    run = session.get(OptimizerRun, frozen.optimizer_run_id)
                    run_doc = json.loads(run.document) if run else None
                    if (
                        selected_at >= cutoff
                        or data_cutoff > selected_at
                        or data_cutoff >= cutoff
                        or data_cutoff.astimezone(IST).date() >= at.date()
                        or run_doc is None
                        or digest(run_doc) != frozen.optimizer_run_id
                        or run_doc["training_data_identity"] != parameters["training_data_identity"]
                        or run_doc["selected"]["atr_period"] != parameters["atr_period"]
                        or run_doc["selected"]["multiplier_tenths"] != parameters["multiplier_tenths"]
                    ):
                        raise RuntimeError("AST_FROZEN_PARAMETERS_INVALID")
                    self._parameter_cache = {at.date(): parameters}
            state = json.loads(cursor.state_json)
            active = self._active(session)
            if active is not None:
                # A skipped native observation must never be substituted as the next fill.
                next_id = session.scalar(
                    select(Snapshot.id)
                    .where(
                        paper_clause(),
                        Snapshot.captured_at > datetime.fromisoformat(cursor.observed_at),
                    )
                    .order_by(Snapshot.captured_at, Snapshot.id)
                    .limit(1)
                )
                if next_id != snapshot_id:
                    raise RuntimeError("AST_EARLIER_OBSERVATION_PENDING")
            step = advance(self.policy, raw, at, bars, parameters, state, active)
            decision = step.decision
            cursor.state_json = canonical(state)
            if step.transition:
                emit(session, cursor, snapshot_id, at, step.transition, active)
            if step.pending:
                session.add(step.pending)
                session.flush()
                emit(session, cursor, snapshot_id, at, "PENDING_PAPER_ENTRY", step.pending)
            cursor.snapshot_id, cursor.observed_at = snapshot_id, at.isoformat()
            cursor.latest_json = canonical(decision)
            emit(
                session,
                cursor,
                snapshot_id,
                at,
                "OBSERVATION",
                decision=decision,
                strategy_state=json.loads(cursor.state_json),
                policy_hash=self.policy.policy_hash,
            )
            return decision["status"]

    def pending_snapshot_ids(self, limit: int = 200) -> list[int]:
        with self.sessions() as session:
            cursor = session.get(AstCursor, 1)
            after = datetime.fromisoformat(cursor.observed_at) if cursor and cursor.observed_at else None
        return pending_ids(self.sessions, after, limit)
