"""Standalone one-lot AST paper lifecycle; shared utilities have no order adapter."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json

from sqlalchemy import or_, select, update

from app.adaptive_supertrend import VERSION
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.decision import decide
from app.adaptive_supertrend.journal import ACTIVE, emit, verify
from app.adaptive_supertrend.market import history, pending_ids
from app.adaptive_supertrend.models import AstCursor, AstTrade, OptimizerRun, SessionParameters
from app.adaptive_supertrend.optimizer import optimize
from app.data.models import OptionContractSnapshot
from app.paper import exit_book
from app.research.manifest import canonical, digest, json_value
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.scalper.models import ScalperCandidate, ScalperDirection, ScalperSignal, ScalperStrength
from app.scalper.paper import executable_pair
from app.scalper.repository import IST, ScalperRepository
from app.scalper.strategy import build_candidates


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
        self.settings = settings
        self.policy = AstPolicy.model_validate_json(settings.ast_config_json)
        self.config = self.policy.execution.scalper(settings) if self.policy.execution else None
        self.costs = self.policy.cost_schedule()
        self.events = ConfiguredMarketEventProvider.from_json(
            self.policy.execution.market_events_json if self.policy.execution else "[]"
        )
        self.market = ScalperRepository(sessions)
        self._history_cache: dict = {}
        self._parameter_cache: dict = {}
        verify(sessions)
        with sessions() as session:
            for row in session.scalars(select(AstTrade).where(AstTrade.state.in_(ACTIVE))):
                doc = json.loads(row.document)
                if doc["policy_hash"] != self.policy.policy_hash:
                    original = json.loads(canonical(doc["policy"]))
                    current = self.policy.model_dump(mode="json")
                    # A one-way emergency kill tightens safety without replacing the frozen policy.
                    if (
                        original.get("execution", {}).get("kill_switch") is False
                        and current.get("execution", {}).get("kill_switch") is True
                    ):
                        original["execution"]["kill_switch"] = True
                    if original != current:
                        raise RuntimeError("AST_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES")

    def _rows(self, session, at) -> list[AstTrade]:
        cooldown = (
            max(self.config.cooldown_after_loss_seconds, self.config.cooldown_after_exit_seconds) if self.config else 0
        )
        earliest_close = (at - timedelta(seconds=cooldown)).date()
        return list(
            session.scalars(
                select(AstTrade).where(
                    or_(
                        AstTrade.state.in_(ACTIVE),
                        AstTrade.trading_date == at.date(),
                        AstTrade.closed_date >= earliest_close,
                    )
                )
            )
        )

    def _risk(self, rows, at, blocked, *, excluding=None) -> list[str]:
        if self.config is None:
            return ["AST_POLICY_UNCONFIGURED"]
        config = self.config
        docs = [json.loads(row.document) for row in rows if row.id != excluding]
        closed = sorted([doc for doc in docs if doc.get("exit_timestamp")], key=lambda doc: doc["exit_timestamp"])
        today = [doc for doc in closed if datetime.fromisoformat(doc["exit_timestamp"]).date() == at.date()]
        reasons = []
        if config.kill_switch:
            reasons.append("KILL_SWITCH_ACTIVE")
        if blocked:
            reasons.append("MARKET_EVENT_BLOCKED")
        if not config.start_time <= at.time().replace(tzinfo=None) <= config.entry_end_time:
            reasons.append("OUTSIDE_ENTRY_WINDOW")
        if any(row.state in ACTIVE and row.id != excluding for row in rows):
            reasons.append("MAX_OPEN_POSITIONS_REACHED")
        if (
            sum(
                bool(doc.get("entry_timestamp")) and datetime.fromisoformat(doc["entry_timestamp"]).date() == at.date()
                for doc in docs
            )
            >= config.max_trades_per_day
        ):
            reasons.append("MAX_TRADES_PER_DAY_REACHED")
        if sum(doc["gross_pnl"] for doc in today) <= -config.hard_daily_loss:
            reasons.append("HARD_DAILY_LOSS_REACHED")
        streak = 0
        for doc in reversed(today):
            if doc["gross_pnl"] >= 0:
                break
            streak += 1
        if streak >= config.max_consecutive_losses:
            reasons.append("MAX_CONSECUTIVE_LOSSES_REACHED")
        if (
            closed
            and (at - datetime.fromisoformat(closed[-1]["exit_timestamp"])).total_seconds()
            < config.cooldown_after_exit_seconds
        ):
            reasons.append("ANY_EXIT_COOLDOWN_ACTIVE")
        losses = [doc for doc in closed if doc["gross_pnl"] < 0]
        if (
            losses
            and (at - datetime.fromisoformat(losses[-1]["exit_timestamp"])).total_seconds()
            < config.cooldown_after_loss_seconds
        ):
            reasons.append("LOSS_COOLDOWN_ACTIVE")
        return reasons

    def _save(self, session, cursor, raw, snapshot_id, row, doc, kind, **details):
        row.document = canonical(doc)
        emit(session, cursor, snapshot_id, raw.captured_at, kind, row, **details)

    def _transition(self, session, cursor, raw, snapshot_id, row, rows, decision, blocked):
        doc = json.loads(row.document)
        if row.state == "UNRESOLVED_EXPOSURE":
            return
        assert self.config is not None and self.policy.execution is not None
        config = self.config
        candidate = ScalperCandidate.model_validate(doc["candidate"])
        pending = row.state == "PENDING_PAPER_ENTRY"
        at = raw.captured_at
        if candidate.short_leg.expiry < at.date():
            row.state = "ENTRY_REJECTED" if pending else "UNRESOLVED_EXPOSURE"
            doc["rejection_reason" if pending else "unresolved_reason"] = "EXPIRED_CONTRACT_NO_EXECUTABLE_EXIT"
            self._save(session, cursor, raw, snapshot_id, row, doc, row.state)
            return
        if pending and at <= datetime.fromisoformat(doc["decision_timestamp"]):
            return
        book = raw.model_copy(update={"lot_size": doc["lot_size"]})
        pair, failure = executable_pair(book, candidate, config, entry=pending, exit_book_policy=exit_book.VERSION)
        if pending:
            age = (at - datetime.fromisoformat(doc["decision_timestamp"])).total_seconds()
            risk = self._risk(rows, at, blocked, excluding=row.id)
            reason = (
                "ENTRY_TTL_EXCEEDED"
                if age > config.entry_ttl_seconds
                else (
                    "FUTURES_OR_LOT_IDENTITY_CHANGED"
                    if (
                        raw.lot_size != doc["lot_size"]
                        or raw.future_instrument_id != doc["future_instrument_id"]
                        or str(raw.future_expiry) != doc["future_expiry"]
                    )
                    else (
                        risk[0]
                        if risk
                        else failure
                        or (
                            "AST_PENDING_THESIS_INVALID"
                            if decision["status"] != "ENTER" or decision["opportunity_id"] != doc["opportunity_id"]
                            else None
                        )
                    )
                )
            )
            if reason is None and pair is not None:
                if (
                    pair.value < config.min_credit
                    or pair.value / candidate.spread_width < config.min_credit_to_width
                    or (candidate.spread_width - pair.value) * doc["lot_size"] > config.max_loss_per_trade
                ):
                    reason = "FILL_ECONOMICS_OUTSIDE_RISK"
            if reason:
                row.state = "ENTRY_REJECTED"
                doc["rejection_reason"] = reason
                self._save(session, cursor, raw, snapshot_id, row, doc, "ENTRY_REJECTED")
                return
            assert pair is not None
            row.state = "OPEN"
            doc.update(
                entry_timestamp=at.isoformat(),
                entry_snapshot_id=snapshot_id,
                entry_credit=pair.value,
                entry_quotes=pair.evidence,
                defined_max_risk=(candidate.spread_width - pair.value) * doc["lot_size"],
                best_gross_points=0,
            )
            state = json.loads(cursor.state_json)
            state.setdefault("consumed", []).append(doc["opportunity_id"])
            cursor.state_json = canonical(state)
            self._save(session, cursor, raw, snapshot_id, row, doc, "ENTRY", fill_method="NEXT_OBSERVATION_BID_ASK")
            return
        if failure:
            doc.update(current_debit=None, gross_mark_rupees=None, mark_unavailable_reason=failure)
            if failure == "MISSING_EXACT_CONTRACT" or at.time().replace(tzinfo=None) >= config.forced_exit_time:
                row.state = "UNRESOLVED_EXPOSURE"
                doc["unresolved_reason"] = failure
            self._save(
                session,
                cursor,
                raw,
                snapshot_id,
                row,
                doc,
                "UNRESOLVED_EXPOSURE" if row.state == "UNRESOLVED_EXPOSURE" else "MARK_UNAVAILABLE",
            )
            return
        assert pair is not None
        pnl = doc["entry_credit"] - pair.value
        best = max(doc.get("best_gross_points", 0), pnl)
        doc.update(
            current_debit=pair.value,
            gross_mark_rupees=pnl * doc["lot_size"],
            best_gross_points=best,
            mark_quotes=pair.evidence,
            mark_at=at.isoformat(),
            mark_unavailable_reason=None,
        )
        safety = self.policy.execution.safety
        reason = (
            "FORCED_CLOSE"
            if at.time().replace(tzinfo=None) >= safety.forced_exit_time
            else (
                "KILL_SWITCH"
                if config.kill_switch
                else (
                    "MARKET_EVENT_EXIT"
                    if blocked
                    else "HARD_MAX_LOSS" if -pnl * doc["lot_size"] >= config.max_loss_per_trade else None
                )
            )
        )
        if reason is None:
            realized_today = sum(
                json.loads(other.document).get("gross_pnl", 0) for other in rows if other.closed_date == at.date()
            )
            if realized_today + pnl * doc["lot_size"] <= -config.hard_daily_loss:
                reason = "HARD_DAILY_LOSS_EXIT"
        # Deliberately no invented strategy thesis exit. Optional legacy baseline is named and fully explicit.
        if reason is None and safety.label == "LEGACY_SAFETY_BASELINE":
            credit = doc["entry_credit"]
            assert safety.stop_credit_multiple is not None and safety.profit_capture_pct is not None
            assert safety.trailing_activation_pct is not None and safety.trailing_giveback_pct is not None
            assert safety.time_stop_minutes is not None
            reason = (
                "LEGACY_SPREAD_STOP"
                if pair.value >= credit * safety.stop_credit_multiple
                else (
                    "LEGACY_PROFIT_CAPTURE"
                    if pair.value <= credit * (1 - safety.profit_capture_pct / 100)
                    else (
                        "LEGACY_TRAILING_EXIT"
                        if best >= credit * safety.trailing_activation_pct / 100
                        and pnl <= best - credit * safety.trailing_giveback_pct / 100
                        else (
                            "LEGACY_TIME_STOP"
                            if (at - datetime.fromisoformat(doc["entry_timestamp"])).total_seconds()
                            >= safety.time_stop_minutes * 60
                            else None
                        )
                    )
                )
            )
        if reason == "KILL_SWITCH" and doc["policy_hash"] != self.policy.policy_hash:
            doc["emergency_safety_override"] = {
                "reason": "KILL_SWITCH_ONLY",
                "applied_policy_hash": self.policy.policy_hash,
            }
        if reason:
            outcome = self.costs.calculate(
                day=at.date(),
                lot_size=doc["lot_size"],
                lots=1,
                entry_short=doc["entry_quotes"]["short"]["fill_price"],
                entry_long=doc["entry_quotes"]["long"]["fill_price"],
                exit_short=pair.short_price,
                exit_long=pair.long_price,
            )
            row.state, row.closed_date = "CLOSED", at.date()
            doc.update(
                exit_timestamp=at.isoformat(),
                exit_snapshot_id=snapshot_id,
                exit_debit=pair.value,
                exit_quotes=pair.evidence,
                exit_reason=reason,
                final_outcome=outcome,
                gross_pnl=outcome["gross_rupees"],
                net_pnl=outcome["net_rupees"],
            )
        self._save(session, cursor, raw, snapshot_id, row, doc, "EXIT" if reason else "MARK")

    def process_snapshot(self, snapshot_id: int) -> str:
        raw = self.market.get_snapshot(snapshot_id)
        if raw is None:
            raise ValueError("AST_SNAPSHOT_UNAVAILABLE")
        at = raw.captured_at.astimezone(IST)
        raw = raw.model_copy(update={"captured_at": at})
        with self.sessions() as session:
            from app.scalper.models import ScalperMarketSnapshotRecord

            record = session.get(ScalperMarketSnapshotRecord, snapshot_id)
            if record is None or json.loads(record.context_json or "{}").get("mode") != "AST_PAPER":
                raise ValueError("AST_FOREIGN_CAPTURE_MODE")
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
                raw.nifty_future,
                parameters,
                state,
                self.policy,
                future_identity=(raw.future_instrument_id, str(raw.future_expiry)),
            )
            if not raw.request_started_at <= raw.response_received_at <= at:
                decision["blockers"].append("AST_OBSERVATION_TIMING_INVALID")
                decision["status"] = "NO_SETUP"
            decision["features"].update(
                futures_source_timestamp=None,
                futures_source_timestamp_status="UNAVAILABLE_IN_SHARED_CAPTURE_SCHEMA",
                futures_observation_time=at.isoformat(),
                futures_observation_time_basis="BROKER_RESPONSE_CAPTURE_TIME",
                snapshot_request_started_at=raw.request_started_at.isoformat(),
                snapshot_response_received_at=raw.response_received_at.isoformat(),
                future_instrument_id=raw.future_instrument_id,
                future_expiry=str(raw.future_expiry),
            )
            cursor.state_json = canonical(state)
            rows = self._rows(session, at)
            blocked = any(event.block_entries for event in self.events.events_at(at))
            for row in rows:
                if row.state in ("OPEN", "PENDING_PAPER_ENTRY"):
                    self._transition(session, cursor, raw, snapshot_id, row, rows, decision, blocked)
            state = json.loads(cursor.state_json)
            # A fill consumes the opportunity before evaluating another entry on this observation.
            if decision["opportunity_id"] in state.get("consumed", []):
                decision["blockers"].append("AST_OPPORTUNITY_CONSUMED")
            decision["blockers"].extend(self._risk(rows, at, blocked))
            if decision["status"] == "ENTER" and not decision["blockers"] and self.config is not None:
                signal = ScalperSignal(
                    timestamp=at,
                    direction=ScalperDirection(decision["direction"]),
                    strength=ScalperStrength.NONE,
                    score=0,
                    components={},
                    contradiction_penalty=0,
                    reasons=["AST_ORB_REGIME_ALIGNED"],
                    warnings=[],
                    confirmation_count=0,
                    confirmed=True,
                )
                built = build_candidates(raw, signal, self.config)
                if raw.expiry < at.date():
                    decision["blockers"].append("EXPIRED_CONTRACT_ENTRY_FORBIDDEN")
                elif not built.candidates:
                    decision["blockers"].append("AST_EXECUTABLE_SPREAD_UNAVAILABLE")
                    decision["candidate_rejections"] = built.rejection_counts
                else:
                    candidate = built.candidates[0]
                    trade_id = digest(
                        {
                            "strategy_id": VERSION,
                            "strategy_version": VERSION,
                            "snapshot_id": snapshot_id,
                            "opportunity": decision["opportunity_id"],
                        }
                    )
                    doc = json_value(
                        {
                            "trade_id": trade_id,
                            "strategy_id": VERSION,
                            "strategy_version": VERSION,
                            "mode": "PAPER",
                            "decision_timestamp": at,
                            "candidate": candidate,
                            "strategy_features": decision["features"],
                            "session_parameters": parameters,
                            "opportunity_id": decision["opportunity_id"],
                            "lot_size": raw.lot_size,
                            "lots": 1,
                            "future_instrument_id": raw.future_instrument_id,
                            "future_expiry": raw.future_expiry,
                            "policy": self.policy.model_dump(mode="json"),
                            "policy_hash": self.policy.policy_hash,
                            "exit_book_policy": exit_book.policy(),
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

    def required_contracts(self, at: datetime | None = None) -> list[OptionContractSnapshot]:
        with self.sessions() as session:
            rows = session.scalars(select(AstTrade).where(AstTrade.state.in_(ACTIVE)))
            contracts: list[OptionContractSnapshot] = []
            for row in rows:
                doc = json.loads(row.document)
                contracts.extend(
                    OptionContractSnapshot.model_validate(doc["candidate"][key]) for key in ("short_leg", "long_leg")
                )
            return list(
                {
                    (
                        item.exchange,
                        item.instrument_token,
                        item.expiry,
                        item.strike,
                        item.option_type,
                        item.trading_symbol,
                    ): item
                    for item in contracts
                }.values()
            )
