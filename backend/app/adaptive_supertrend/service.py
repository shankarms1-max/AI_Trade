"""Standalone one-lot AST paper lifecycle; shared utilities have no order adapter."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
import json

from sqlalchemy import select, update

from app.adaptive_supertrend import CAPTURE_MODE, VERSION
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.decision import decide
from app.adaptive_supertrend.journal import ACTIVE, emit, verify
from app.adaptive_supertrend.market import history, paper_clause, pending_ids
from app.adaptive_supertrend.models import AstCursor, AstTrade, OptimizerRun, SessionParameters
from app.adaptive_supertrend.optimizer import optimize
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
    selected = result["selected"]
    run_id = digest(result)
    document = {
        key: result[key]
        for key in (
            "trading_date",
            "selected_at",
            "data_cutoff",
            "policy_hash",
            "training_data_identity",
            "walk_forward_status",
        )
    }
    document.update(
        strategy_id=VERSION,
        strategy_version=VERSION,
        optimizer_run_id=run_id,
        atr_period=selected["atr_period"],
        multiplier_tenths=selected["multiplier_tenths"],
        metrics=selected["metrics"],
        neighbour_robustness=selected["neighbour_robustness"],
    )
    if provenance is not None:
        document["history_provenance"] = result["history_provenance"]
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

    def _transition(self, session, cursor, raw, at, price, row, decision, state):
        if row.state == "UNRESOLVED_EXPOSURE":
            return
        cfg = self.policy.execution
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
        elif price is None:
            reason = "FUTURES_OBSERVATION_UNAVAILABLE"

        if row.state == "PENDING_PAPER_ENTRY":
            delay = (at - datetime.fromisoformat(doc["decision_timestamp"])).total_seconds()
            if reason is None:
                if delay <= 0 or delay > self.policy.max_sequence_gap_seconds:
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
        emit(session, cursor, raw.id, at, kind, row)

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
            decision = decide(
                bars,
                at,
                float(raw.nifty_future) if raw.nifty_future is not None else None,
                parameters,
                state,
                self.policy,
                future_identity=(raw.future_instrument_id, str(raw.future_expiry)),
            )
            requested, responded = _aware(raw.request_started_at), _aware(raw.response_received_at)
            timing_ok = (
                requested <= responded <= at
                and (at - requested).total_seconds() <= self.policy.max_sequence_gap_seconds
            )
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
                self._transition(session, cursor, raw, at, price, active, decision, state)
            cursor.state_json = canonical(state)
            cfg = self.policy.execution
            lot = cfg.lot_size(raw.future_instrument_id, raw.future_expiry) if cfg else None
            if decision["opportunity_id"] in state.get("consumed", []):
                decision["blockers"].append("AST_OPPORTUNITY_CONSUMED")
            if active is not None and active.state in ACTIVE:
                decision["blockers"].append("MAX_OPEN_POSITIONS_REACHED")
            if cfg and at.time() > cfg.entry_end_time:
                decision["blockers"].append("OUTSIDE_ENTRY_WINDOW")
            if lot is None or raw.future_expiry is None or raw.future_expiry < at.date():
                decision["blockers"].append("AST_FUTURES_CONTRACT_UNCONFIGURED_OR_EXPIRED")
            if decision["status"] == "ENTER" and not decision["blockers"]:
                trade_id = digest(
                    {"strategy": VERSION, "snapshot_id": snapshot_id, "opportunity": decision["opportunity_id"]}
                )
                doc = json_value(
                    {
                        "trade_id": trade_id,
                        "strategy_id": VERSION,
                        "strategy_version": VERSION,
                        "mode": "PAPER",
                        "side": "LONG" if decision["direction"] == "BULL" else "SHORT",
                        "decision_timestamp": at,
                        "decision_snapshot_id": snapshot_id,
                        "strategy_features": decision["features"],
                        "session_parameters": parameters,
                        "opportunity_id": decision["opportunity_id"],
                        "lot_size": lot,
                        "lots": 1,
                        "future_instrument_id": raw.future_instrument_id,
                        "future_expiry": raw.future_expiry,
                        "price_basis": "OBSERVED_FUTURES_LTP",
                        "policy_hash": self.policy.policy_hash,
                        "policy": self.policy.model_dump(mode="json"),
                    }
                )
                trade = AstTrade(
                    id=trade_id,
                    decision_snapshot_id=snapshot_id,
                    trading_date=at.date(),
                    state="PENDING_PAPER_ENTRY",
                    document=canonical(doc),
                )
                session.add(trade)
                session.flush()
                emit(session, cursor, snapshot_id, at, "PENDING_PAPER_ENTRY", trade)
                decision["status"] = "PENDING_PAPER_ENTRY"
            if decision["blockers"]:
                decision["status"] = "NO_SETUP"
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
