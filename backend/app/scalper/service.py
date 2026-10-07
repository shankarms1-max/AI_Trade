"""Capture orchestration and atomic deterministic scalper paper transitions."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import threading
from time import perf_counter
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.data.models import MarketSnapshot
from app.research.manifest import canonical, digest, json_value
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.scalper import SCALPER_VERSION
from app.scalper.config import ScalperConfig
from app.scalper.features import build_features
from app.scalper.models import (ScalperCursor, ScalperEvent, ScalperFeatures,
                                ScalperMarketSnapshot, ScalperMarketSnapshotRecord,
                                ScalperOptionQuote, ScalperSignal, ScalperTrade,
                                ScalperCandidate)
from app.scalper.paper import cost_schedule, executable_pair, exit_trigger
from app.scalper.repository import ScalperRepository
from app.scalper.risk import ScalperRiskState, evaluate_entry
from app.scalper.signals import build_signal
from app.scalper.strategy import build_candidates

logger = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")
ACTIVE = ("PENDING_ENTRY", "OPEN", "UNRESOLVED")


def to_scalper_snapshot(snapshot: MarketSnapshot) -> ScalperMarketSnapshot:
    """Strictly adapt read-only broker data; incomplete identities fail capture."""
    if snapshot.lot_size is None:
        raise ValueError("SCALPER_CONFIRMED_LOT_SIZE_REQUIRED")
    if any(not item.instrument_token for item in snapshot.options):
        raise ValueError("SCALPER_INCOMPLETE_QUOTE_IDENTITY")
    captured = snapshot.response_received_at or snapshot.timestamp_ist
    started = snapshot.request_started_at or snapshot.timestamp_ist
    quotes = [ScalperOptionQuote(
        expiry=item.expiry, strike=item.strike,
        option_type=item.option_type.value, exchange=item.exchange,
        trading_symbol=item.trading_symbol,
        instrument_token=item.instrument_token or "",
        source_market_timestamp=item.source_market_timestamp,
        bid=item.bid, ask=item.ask, bid_quantity=item.bid_quantity,
        ask_quantity=item.ask_quantity, depth_unit=item.depth_unit,
        tick_size=item.tick_size, ltp=item.ltp, volume=item.volume,
        open_interest=item.open_interest)
        for item in snapshot.options]
    source_times = [item.source_market_timestamp for item in quotes
                    if item.source_market_timestamp is not None]
    return ScalperMarketSnapshot(
        captured_at=captured, request_started_at=started,
        response_received_at=captured,
        source_market_timestamp=max(source_times) if source_times else None,
        nifty_spot=snapshot.nifty_spot, nifty_future=snapshot.nifty_future,
        future_instrument_id=snapshot.future_instrument_id,
        future_expiry=snapshot.future_expiry, india_vix=snapshot.india_vix,
        lot_size=snapshot.lot_size, atm_strike=snapshot.atm_strike,
        expiry=snapshot.expiry, source=snapshot.source, quotes=quotes)


class ScalperPaperEngine:
    def __init__(self, sessions, settings) -> None:
        self.sessions = sessions
        self.settings = settings
        self.config = ScalperConfig.from_settings(settings)
        self.repository = ScalperRepository(sessions)
        self.events = ConfiguredMarketEventProvider.from_json(
            self.config.event_configuration_json)
        self.costs = cost_schedule(settings)
        self.policy = json_value({"version": SCALPER_VERSION,
                                  "config": self.config.payload(),
                                  "costs": self.costs.payload(),
                                  "execution_mode": "PAPER",
                                  "risk_accounting_basis": "GROSS_REALIZED",
                                  "profitability_claim": False})
        self.policy_hash = digest(self.policy)
        verify_scalper_journal(sessions)

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as session:
            changed = session.execute(update(ScalperCursor).where(
                ScalperCursor.id == 1).values(
                    lock_version=ScalperCursor.lock_version + 1)).rowcount
            if changed != 1:
                raise RuntimeError("SCALPER_MIGRATION_REQUIRED")
            cursor = session.get(ScalperCursor, 1)
            yield session, cursor

    @staticmethod
    def _snapshot(session, snapshot_id: int) -> tuple[ScalperMarketSnapshotRecord,
                                                      ScalperMarketSnapshot,
                                                      ScalperFeatures,
                                                      ScalperSignal]:
        row = session.scalar(select(ScalperMarketSnapshotRecord).options(
            selectinload(ScalperMarketSnapshotRecord.quotes)).where(
                ScalperMarketSnapshotRecord.id == snapshot_id))
        if row is None:
            raise LookupError("SCALPER_SNAPSHOT_NOT_FOUND")
        raw = ScalperRepository._row_to_snapshot(row)
        return (row, raw, ScalperFeatures.model_validate(json.loads(row.feature_json)),
                ScalperSignal.model_validate(json.loads(row.signal_json)))

    def _emit(self, session, cursor: ScalperCursor, kind: str,
              row: ScalperMarketSnapshotRecord, raw: ScalperMarketSnapshot,
              trade: ScalperTrade | None = None, **details) -> None:
        key = f"{row.id}:{trade.id if trade else 'SESSION'}:{kind}"
        if session.scalar(select(ScalperEvent.sequence).where(
                ScalperEvent.event_key == key)) is not None:
            raise ValueError("DUPLICATE_SCALPER_TRANSITION")
        document = json.loads(trade.document) if trade else None
        payload = json_value({
            "event_type": kind, "execution_mode": "PAPER",
            "snapshot_id": row.id, "market_timestamp": raw.captured_at,
            "source_market_timestamp": raw.source_market_timestamp,
            "ingestion_timestamp": raw.response_received_at,
            "policy_hash": self.policy_hash,
            "trade_id": trade.id if trade else None,
            "state": trade.state if trade else None,
            "trade_document": document, **details})
        cursor.sequence += 1
        event_hash = digest({"sequence": cursor.sequence, "event_key": key,
                             "previous_hash": cursor.head_hash, "payload": payload})
        session.add(ScalperEvent(sequence=cursor.sequence, event_key=key,
                                 event_type=kind, snapshot_id=row.id,
                                 trade_id=trade.id if trade else None,
                                 previous_hash=cursor.head_hash,
                                 event_hash=event_hash, payload=canonical(payload)))
        cursor.head_hash = event_hash
        session.flush()

    @staticmethod
    def _save(trade: ScalperTrade, document: dict, state: str | None = None) -> None:
        if state is not None:
            trade.state = state
        trade.document = canonical(json_value(document))

    @staticmethod
    def _risk_state(session, day) -> ScalperRiskState:
        rows = session.scalars(select(ScalperTrade)).all()
        docs = [(row.state, json.loads(row.document)) for row in rows]
        today = [(state, doc) for state, doc in docs
                 if doc.get("decision_timestamp", "")[:10] == str(day)]
        closed = [doc for state, doc in today if state == "CLOSED"]
        ordered_closed = sorted(closed, key=lambda item: item.get("exit_timestamp", ""))
        consecutive = 0
        for doc in reversed(ordered_closed):
            if (doc.get("final_outcome") or {}).get("gross_rupees", 0) >= 0:
                break
            consecutive += 1
        exits = [datetime.fromisoformat(doc["exit_timestamp"]) for doc in closed
                 if doc.get("exit_timestamp")]
        losses = [datetime.fromisoformat(doc["exit_timestamp"]) for doc in closed
                  if doc.get("exit_timestamp") and
                  (doc.get("final_outcome") or {}).get("gross_rupees", 0) < 0]
        return ScalperRiskState(
            open_positions=sum(state in {"PENDING_ENTRY", "OPEN"} for state, _ in docs),
            unresolved_positions=sum(state == "UNRESOLVED" for state, _ in docs),
            executed_trades_today=sum("entry_timestamp" in doc for _, doc in today),
            gross_realized_today=sum((doc.get("final_outcome") or {}).get("gross_rupees", 0)
                                     for doc in closed),
            consecutive_losses=consecutive,
            last_exit_at=max(exits) if exits else None,
            last_loss_at=max(losses) if losses else None)

    def _reject_pending(self, session, cursor, row, raw, trade, document, reason,
                        started: float | None = None) -> None:
        document.update(rejection_reason=reason,
                        attempted_execution_snapshot_id=row.id)
        self._save(trade, document, "ENTRY_REJECTED")
        self._emit(session, cursor, "SCALPER_ENTRY_REJECTED", row, raw, trade,
                   reason=reason)
        latency = 0.0 if started is None else (perf_counter() - started) * 1000
        logger.info("SCALPER_ENTRY_REJECTED trade_id=%s reason=%s execution_latency_ms=%.3f",
                    trade.id, reason, latency)

    def _transition_active(self, session, cursor, row, raw, signal, trade) -> None:
        started = perf_counter()
        document = json.loads(trade.document)
        candidate = ScalperCandidate.model_validate(document["candidate"])
        if document["policy_hash"] != self.policy_hash:
            document["unresolved_reason"] = "SCALPER_POLICY_CHANGED"
            self._save(trade, document, "UNRESOLVED")
            self._emit(session, cursor, "SCALPER_ERROR", row, raw, trade,
                       reason="SCALPER_POLICY_CHANGED")
            return
        if trade.state == "UNRESOLVED":
            return
        pair, failure = executable_pair(raw, candidate, self.config,
                                        entry=trade.state == "PENDING_ENTRY")
        if trade.state == "PENDING_ENTRY":
            decision_at = datetime.fromisoformat(document["decision_timestamp"])
            age = (raw.captured_at - decision_at).total_seconds()
            reason = None
            if row.id == trade.decision_snapshot_id or raw.captured_at <= decision_at:
                reason = "SAME_OBSERVATION_FILL_FORBIDDEN"
            elif age > self.config.entry_ttl_seconds:
                reason = "ENTRY_TTL_EXCEEDED"
            elif raw.captured_at.date() >= candidate.short_leg.expiry:
                reason = "EXPIRY_DAY_ENTRY_FORBIDDEN"
            elif failure:
                reason = failure
            if reason:
                self._reject_pending(session, cursor, row, raw, trade, document, reason,
                                     started)
                return
            assert pair is not None
            actual_loss = (candidate.spread_width - pair.value) * raw.lot_size * self.config.lots
            if pair.value < self.config.min_credit or actual_loss > self.config.max_loss_per_trade:
                self._reject_pending(session, cursor, row, raw, trade, document,
                                     "FILL_ECONOMICS_OUTSIDE_RISK", started)
                return
            document.update(
                entry_credit=pair.value, entry_timestamp=raw.captured_at.isoformat(),
                execution_snapshot_id=row.id, entry_spot=raw.nifty_spot,
                entry_quotes=pair.evidence, last_snapshot_id=row.id,
                last_observation=raw.captured_at.isoformat(), current_debit=pair.value,
                best_gross_points=0.0)
            self._save(trade, document, "OPEN")
            self._emit(session, cursor, "SCALPER_ENTRY_EXECUTED", row, raw, trade,
                       fill_method="NEXT_OBSERVATION_BID_ASK", quotes=pair.evidence)
            logger.info("SCALPER_ENTRY_EXECUTED trade_id=%s execution_latency_ms=%.3f",
                        trade.id, (perf_counter() - started) * 1000)
            return
        if failure:
            if raw.captured_at.time().replace(tzinfo=None) >= self.config.forced_exit_time:
                document.update(unresolved_reason=failure, last_snapshot_id=row.id,
                                last_observation=raw.captured_at.isoformat())
                self._save(trade, document, "UNRESOLVED")
                self._emit(session, cursor, "SCALPER_ERROR", row, raw, trade,
                           reason=failure, exposure="UNRESOLVED")
            else:
                self._emit(session, cursor, "SCALPER_MARK", row, raw, trade,
                           available=False, reason=failure)
            return
        assert pair is not None
        pnl = document["entry_credit"] - pair.value
        document.update(current_debit=pair.value,
                        gross_mark_points=pnl,
                        gross_mark_rupees=pnl * raw.lot_size * self.config.lots,
                        best_gross_points=max(document.get("best_gross_points", 0), pnl),
                        last_snapshot_id=row.id,
                        last_observation=raw.captured_at.isoformat())
        self._save(trade, document)
        self._emit(session, cursor, "SCALPER_MARK", row, raw, trade,
                   available=True, quotes=pair.evidence,
                   gross_points=pnl,
                   holding_seconds=(raw.captured_at - datetime.fromisoformat(
                       document["entry_timestamp"])).total_seconds())
        trigger = exit_trigger(document, raw, signal, pair.value, self.config)
        logger.info("SCALPER_MARK trade_id=%s gross_points=%.4f marking_latency_ms=%.3f",
                    trade.id, pnl, (perf_counter() - started) * 1000)
        if trigger is None:
            return
        outcome = self.costs.calculate(
            day=raw.captured_at.date(), lot_size=raw.lot_size, lots=self.config.lots,
            entry_short=document["entry_quotes"]["short"]["fill_price"],
            entry_long=document["entry_quotes"]["long"]["fill_price"],
            exit_short=pair.short_price, exit_long=pair.long_price)
        document.update(exit_snapshot_id=row.id,
                        exit_timestamp=raw.captured_at.isoformat(),
                        exit_reason=trigger, exit_quotes=pair.evidence,
                        final_outcome=outcome,
                        holding_seconds=(raw.captured_at - datetime.fromisoformat(
                            document["entry_timestamp"])).total_seconds(),
                        profitability_claim=False)
        self._save(trade, document, "CLOSED")
        self._emit(session, cursor, "SCALPER_EXIT", row, raw, trade,
                   trigger=trigger, quotes=pair.evidence,
                   accounting=outcome, profitability_claim=False)
        logger.info("SCALPER_EXIT trade_id=%s trigger=%s execution_latency_ms=%.3f",
                    trade.id, trigger, (perf_counter() - started) * 1000)

    def process_snapshot(self, snapshot_id: int) -> str:
        with self.transaction() as (session, cursor):
            row, raw, features, signal = self._snapshot(session, snapshot_id)
            observation_key = f"{row.id}:SESSION:SCALPER_OBSERVATION"
            if session.scalar(select(ScalperEvent.sequence).where(
                    ScalperEvent.event_key == observation_key)) is not None:
                return "DUPLICATE"
            if cursor.observed_at and raw.captured_at <= datetime.fromisoformat(cursor.observed_at):
                raise ValueError("SCALPER_OUT_OF_ORDER_OBSERVATION")
            active = session.scalars(select(ScalperTrade).where(
                ScalperTrade.state.in_(ACTIVE)).order_by(ScalperTrade.id)).all()
            for trade in active:
                self._transition_active(session, cursor, row, raw, signal, trade)

            state = self._risk_state(session, raw.captured_at.date())
            if state.open_positions + state.unresolved_positions == 0 and signal.confirmed:
                candidate_started = perf_counter()
                built = build_candidates(raw, signal, self.config)
                logger.info(
                    "SCALPER_CANDIDATES_BUILT snapshot_id=%d count=%d "
                    "candidate_latency_ms=%.3f",
                    snapshot_id, len(built.candidates),
                    (perf_counter() - candidate_started) * 1000)
                if built.candidates:
                    candidate = built.candidates[0]
                    blocked = any(item.block_entries for item in
                                  self.events.events_at(raw.captured_at))
                    risk = evaluate_entry(candidate, signal, state, self.config,
                                          raw.captured_at, event_blocked=blocked)
                    if self.config.kill_switch:
                        logger.warning("SCALPER_KILL_SWITCH snapshot_id=%d", snapshot_id)
                    structural_reference = (features.local_low if signal.direction.value == "BULL"
                                            else features.local_high)
                    document = json_value({
                        "candidate": candidate, "signal": signal,
                        "features": features, "phase14_context": signal.phase14_context,
                        "risk_decision": risk, "policy": self.policy,
                        "policy_hash": self.policy_hash,
                        "decision_snapshot_id": row.id,
                        "decision_timestamp": raw.captured_at,
                        "decision_source_timestamp": raw.source_market_timestamp,
                        "decision_spot": raw.nifty_spot,
                        "lot_size": raw.lot_size, "lots": self.config.lots,
                        "structural_reference": structural_reference,
                        "candidate_rejections": built.rejection_counts,
                        "profitability_claim": False,
                        "cost_completeness": "GROSS_ONLY",
                        "broker_margin": None})
                    trade = ScalperTrade(
                        id=digest([SCALPER_VERSION, row.id, candidate.candidate_id]),
                        decision_snapshot_id=row.id, state="SIGNAL",
                        document=canonical(document))
                    session.add(trade)
                    session.flush()
                    self._emit(session, cursor, "SCALPER_SIGNAL", row, raw, trade)
                    if risk.approved and raw.captured_at.date() < candidate.short_leg.expiry:
                        self._save(trade, document, "PENDING_ENTRY")
                        self._emit(session, cursor, "SCALPER_ENTRY_PENDING", row, raw, trade)
                        logger.info("SCALPER_ENTRY_PENDING trade_id=%s", trade.id)
                    else:
                        reason = ("EXPIRY_DAY_ENTRY_FORBIDDEN" if
                                  raw.captured_at.date() >= candidate.short_leg.expiry else
                                  ",".join(risk.reasons))
                        document["rejection_reason"] = reason
                        self._save(trade, document, "ENTRY_REJECTED")
                        self._emit(session, cursor, "SCALPER_ENTRY_REJECTED", row, raw,
                                   trade, reason=reason)
                        logger.info(
                            "SCALPER_ENTRY_REJECTED trade_id=%s reason=%s "
                            "execution_latency_ms=0.000",
                            trade.id, reason)
                else:
                    logger.info(
                        "SCALPER_CANDIDATES_BUILT snapshot_id=%d count=0 "
                        "candidate_latency_ms=%.3f",
                        snapshot_id, (perf_counter() - candidate_started) * 1000)
            elif self.config.kill_switch:
                logger.warning("SCALPER_KILL_SWITCH snapshot_id=%d", snapshot_id)

            cursor.snapshot_id = row.id
            cursor.observed_at = raw.captured_at.isoformat()
            cursor.confirmation_direction = signal.direction.value
            cursor.confirmation_count = signal.confirmation_count
            self._emit(session, cursor, "SCALPER_OBSERVATION", row, raw,
                       score=signal.score, direction=signal.direction.value,
                       confirmed=signal.confirmed)
        return "PROCESSED"


class ScalperService:
    def __init__(self, sessions, settings,
                 snapshot_builder: Callable[[], MarketSnapshot] | None = None) -> None:
        self.settings = settings
        self.config = ScalperConfig.from_settings(settings)
        self.repository = ScalperRepository(sessions)
        self.engine = ScalperPaperEngine(sessions, settings)
        self.snapshot_builder = snapshot_builder
        self._lock = threading.Lock()

    def capture_once(
        self,
        snapshot: MarketSnapshot | ScalperMarketSnapshot | None = None,
    ) -> int | None:
        if not self.settings.scalper_enabled:
            return None
        if not self._lock.acquire(blocking=False):
            logger.warning("SCALPER_ERROR reason=overlap")
            return None
        cycle_started = perf_counter()
        try:
            logger.info("SCALPER_CAPTURE_STARTED")
            capture_started = perf_counter()
            if snapshot is None:
                if self.snapshot_builder is None:
                    raise RuntimeError("SCALPER_SNAPSHOT_BUILDER_REQUIRED")
                snapshot = self.snapshot_builder()
            raw = to_scalper_snapshot(snapshot) if isinstance(
                snapshot, MarketSnapshot) else snapshot
            capture_latency = (perf_counter() - capture_started) * 1000
            existing_id = self.repository.existing_snapshot_id(raw)
            if existing_id is not None:
                self.engine.process_snapshot(existing_id)
                logger.info(
                    "SCALPER_CAPTURE_SUCCESS snapshot_id=%d duplicate=true "
                    "capture_latency_ms=%.3f total_cycle_latency_ms=%.3f",
                    existing_id, capture_latency,
                    (perf_counter() - cycle_started) * 1000)
                return existing_id
            latest_at = self.repository.latest_captured_at()
            if latest_at is not None and raw.captured_at <= latest_at:
                raise ValueError("SCALPER_OUT_OF_ORDER_CAPTURE")
            prior = self.repository.history(before=raw.captured_at, inclusive=False,
                                            limit=max(1000, self.config.feature_lookback))
            feature_started = perf_counter()
            features = build_features([*prior, raw], self.config.feature_lookback,
                                      self.config.interval_seconds)
            feature_latency = (perf_counter() - feature_started) * 1000
            context = (self.repository.phase14_context_before(raw.request_started_at)
                       if self.config.use_phase14_context else None)
            signal_started = perf_counter()
            signal = build_signal(raw, features,
                                  self.repository.signal_history(before=raw.captured_at),
                                  min_score=self.config.signal_min_score,
                                  min_confirmations=self.config.min_confirmations,
                                  max_confirmation_gap_seconds=(
                                      self.config.interval_seconds * 1.5),
                                  phase14_context=context)
            signal_latency = (perf_counter() - signal_started) * 1000
            logger.info(
                "SCALPER_SIGNAL_BUILT score=%.3f direction=%s confirmations=%d "
                "feature_latency_ms=%.3f signal_latency_ms=%.3f",
                signal.score, signal.direction.value, signal.confirmation_count,
                feature_latency, signal_latency)
            snapshot_id, duplicate = self.repository.save_snapshot(
                raw, features, signal, context)
            self.engine.process_snapshot(snapshot_id)
            logger.info(
                "SCALPER_CAPTURE_SUCCESS snapshot_id=%d duplicate=%s "
                "capture_latency_ms=%.3f feature_latency_ms=%.3f "
                "signal_latency_ms=%.3f total_cycle_latency_ms=%.3f",
                snapshot_id, str(duplicate).lower(),
                capture_latency, feature_latency, signal_latency,
                (perf_counter() - cycle_started) * 1000)
            return snapshot_id
        except Exception as exc:
            logger.error("SCALPER_ERROR error_type=%s latency_ms=%.3f",
                         type(exc).__name__, (perf_counter() - cycle_started) * 1000)
            raise
        finally:
            self._lock.release()


def verify_scalper_journal(sessions) -> int:
    with sessions.begin() as session:
        cursor = session.get(ScalperCursor, 1)
        if cursor is None:
            raise RuntimeError("SCALPER_MIGRATION_REQUIRED")
        head = "GENESIS"
        sequence = 0
        projections: dict[str, tuple[str, str]] = {}
        for event in session.scalars(select(ScalperEvent).order_by(ScalperEvent.sequence)):
            sequence += 1
            payload = json.loads(event.payload)
            expected = digest({"sequence": sequence, "event_key": event.event_key,
                               "previous_hash": head, "payload": payload})
            if (event.sequence != sequence or event.previous_hash != head
                    or event.event_hash != expected):
                raise ValueError("SCALPER_JOURNAL_INTEGRITY_FAILURE")
            head = expected
            if payload.get("trade_id") and payload.get("trade_document") is not None:
                projections[payload["trade_id"]] = (
                    payload["state"], canonical(payload["trade_document"]))
        if cursor.sequence != sequence or cursor.head_hash != head:
            raise ValueError("SCALPER_CURSOR_INTEGRITY_FAILURE")
        rows = {row.id: (row.state, canonical(json.loads(row.document)))
                for row in session.scalars(select(ScalperTrade))}
        if rows != projections:
            raise ValueError("SCALPER_PROJECTION_INTEGRITY_FAILURE")
        return sequence
