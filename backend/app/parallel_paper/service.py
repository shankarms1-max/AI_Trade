"""Partitioned paper journals using the existing protected spread, risk, fill and exit utilities."""
from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import get_logger
from app.data.models import OptionContractSnapshot
from app.parallel_paper.bars import Bar, MarketPoint, build_bars, contiguous_bars
from app.parallel_paper.config import CAPTURE_MODE, STRATEGIES, VERSIONS, ExperimentConfig
from app.parallel_paper.decisions import decide, entry_committed
from app.parallel_paper.indicators import ema, supertrend
from app.parallel_paper.market import ParallelMarketRepository
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperEvent, ParallelPaperTrade
from app.paper import exit_book, expiry, mirror_exit
from app.research.manifest import canonical, digest, json_value
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.scalper.config import ScalperConfig
from app.scalper.models import ScalperCandidate, ScalperMarketSnapshot, ScalperMarketSnapshotRecord
from app.scalper.paper import cost_schedule, executable_pair
from app.scalper.repository import IST
from app.scalper.strategy import build_candidates
from app.scalper_lab.engine import LabTrade, Ledger, _construction_signal, _exit_reason, _risk_blockers

logger = get_logger(__name__)
ACTIVE = ("PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE")
ENTRY_POLICY_VERSION = "PARALLEL_PAPER_EXPIRY_DAY_ENTRY_V1"


def _lab(row: ParallelPaperTrade) -> LabTrade:
    doc = json.loads(row.document)
    return LabTrade(
        row.strategy_id, ScalperCandidate.model_validate(doc["candidate"]),
        doc["decision"], row.decision_snapshot_id,
        state={"PENDING_PAPER_ENTRY": "PENDING_ENTRY", "UNRESOLVED_EXPOSURE": "UNRESOLVED"}.get(
            row.state, row.state), entry_credit=doc.get("entry_credit"),
        entry_timestamp=datetime.fromisoformat(doc["entry_timestamp"]) if doc.get("entry_timestamp") else None,
        entry_quotes=doc.get("entry_quotes"), entry_spot=doc.get("underlying_entry_price"),
        exit_timestamp=datetime.fromisoformat(doc["exit_timestamp"]) if doc.get("exit_timestamp") else None,
        exit_reason=doc.get("exit_reason"), outcome=doc.get("final_outcome"),
        expiry_terminalization=expiry.is_unpriced(doc),
        best_gross_points=doc.get("best_gross_points", 0), mae=doc.get("mae", 0), mfe=doc.get("mfe", 0))


def verify_journals(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        for strategy in STRATEGIES:
            cursor = session.get(ParallelPaperCursor, strategy)
            if cursor is None:
                raise RuntimeError("PARALLEL_PAPER_MIGRATION_REQUIRED")
            previous, latest, observation = "GENESIS", {}, None
            events = session.scalars(select(ParallelPaperEvent).where(
                ParallelPaperEvent.strategy_id == strategy).order_by(ParallelPaperEvent.sequence)).all()
            for sequence, event in enumerate(events, 1):
                payload = json.loads(event.payload)
                expected = digest({"strategy_id": strategy, "sequence": sequence,
                                   "event_key": event.event_key, "previous_hash": previous, "payload": payload})
                if (event.sequence != sequence or event.previous_hash != previous or event.event_hash != expected
                        or payload.get("strategy_id") != strategy or event.event_type != payload.get("event_type")
                        or event.snapshot_id != payload.get("snapshot_id")
                        or event.trade_id != payload.get("trade_id")):
                    raise RuntimeError(f"PARALLEL_PAPER_JOURNAL_INVALID:{strategy}")
                if event.trade_id is not None:
                    latest[event.trade_id] = (payload["state"], payload["trade_document"])
                if event.event_type == "OBSERVATION":
                    observation = payload
                previous = event.event_hash
            if cursor.sequence != len(events) or cursor.head_hash != previous:
                raise RuntimeError(f"PARALLEL_PAPER_JOURNAL_INVALID:{strategy}")
            if observation is not None and (
                    cursor.snapshot_id != observation["snapshot_id"]
                    or cursor.observed_at != observation["timestamp"]
                    or json.loads(cursor.state_json) != observation["strategy_state"]
                    or json.loads(cursor.latest_json) != observation["decision"]):
                raise RuntimeError(f"PARALLEL_PAPER_CURSOR_INVALID:{strategy}")
            trades = session.scalars(select(ParallelPaperTrade).where(
                ParallelPaperTrade.strategy_id == strategy)).all()
            if len(trades) != len(latest) or any(
                    latest.get(row.id) != (row.state, json.loads(row.document)) for row in trades):
                raise RuntimeError(f"PARALLEL_PAPER_PROJECTION_INVALID:{strategy}")
            for row in trades:
                doc = json.loads(row.document)
                if (doc.get("strategy_id") != strategy or doc.get("strategy_version") != row.strategy_version
                        or doc.get("trade_id") != row.id or doc.get("policy_hash") != digest(doc.get("policy"))):
                    raise RuntimeError(f"PARALLEL_PAPER_TRADE_IDENTITY_INVALID:{strategy}")


class ParallelPaperService:
    def __init__(self, sessions: sessionmaker[Session], settings: Any) -> None:
        self.sessions = sessions
        self.experiment = ExperimentConfig.model_validate_json(settings.parallel_paper_config_json)
        self.configs = {strategy: self.experiment.execution(settings, strategy) for strategy in STRATEGIES}
        self.costs = cost_schedule(settings)
        self.policies = {strategy: json_value({
            "strategy_id": strategy, "strategy_version": VERSIONS[strategy], "mode": "PAPER",
            "parameters": self.experiment.strategy_payload(strategy), "bars_version": "SAMPLED_FUTURES_V1",
            "entry_policy_version": ENTRY_POLICY_VERSION,
            "entry_policy": {"same_day_expiry": "ALLOWED", "expired_contracts": "FORBIDDEN"},
            "exit_policy_version": mirror_exit.VERSION, "exit_policy": mirror_exit.policy(config),
            "exit_book_policy": exit_book.policy(),
            "execution": config.payload(), "costs": self.costs.payload()})
            for strategy, config in self.configs.items()}
        self.hashes = {strategy: digest(policy) for strategy, policy in self.policies.items()}
        self.events = {strategy: ConfiguredMarketEventProvider.from_json(config.event_configuration_json)
                       for strategy, config in self.configs.items()}
        self.market = ParallelMarketRepository(sessions)
        verify_journals(sessions)
        with sessions() as session:
            self._assert_entry_policy_upgrade_safe(session)

    def _terminalize_expired(self, session: Session, cursor: ParallelPaperCursor, snapshot_id: int,
                             raw: ScalperMarketSnapshot, trade: ParallelPaperTrade) -> bool:
        doc = json.loads(trade.document)
        state = expiry.terminalize(trade.state, doc, raw.captured_at, snapshot_id)
        if state is None:
            return False
        trade.state, trade.document = state, canonical(doc)
        self._emit(session, cursor, snapshot_id, raw,
                   "ENTRY_REJECTED" if state == "ENTRY_REJECTED" else "CONTRACT_EXPIRED_UNPRICED", trade,
                   reason=doc.get("rejection_reason") if state == "ENTRY_REJECTED" else "CONTRACT_EXPIRED_UNPRICED",
                   lifecycle_version=expiry.VERSION)
        return True

    @staticmethod
    def _assert_entry_policy_upgrade_safe(session: Session, at: datetime | None = None) -> None:
        rows = session.scalars(select(ParallelPaperTrade).where(
            ParallelPaperTrade.strategy_id.in_(STRATEGIES), ParallelPaperTrade.state.in_(ACTIVE)))
        for row in rows:
            doc = json.loads(row.document)
            if expiry.expired(doc, at or datetime.now(IST)):
                continue
            policy = doc.get("policy", {})
            if policy.get("entry_policy_version") != ENTRY_POLICY_VERSION:
                raise RuntimeError("PARALLEL_PAPER_ENTRY_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES")
            if policy.get("exit_book_policy") != exit_book.policy():
                raise RuntimeError("PARALLEL_PAPER_EXIT_BOOK_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES")

    @staticmethod
    def _trades(session: Session, strategy: str) -> list[ParallelPaperTrade]:
        return list(session.scalars(select(ParallelPaperTrade).where(
            ParallelPaperTrade.strategy_id == strategy).order_by(
                ParallelPaperTrade.decision_snapshot_id)).all())

    def _emit(self, session: Session, cursor: ParallelPaperCursor, snapshot_id: int,
              raw: ScalperMarketSnapshot, kind: str, trade: ParallelPaperTrade | None = None,
              **details: Any) -> None:
        key = f"{snapshot_id}:{trade.id if trade else 'SESSION'}:{kind}"
        doc = json.loads(trade.document) if trade else None
        payload = json_value({"strategy_id": cursor.strategy_id,
                              "strategy_version": VERSIONS[cursor.strategy_id], "mode": "PAPER",
                              "event_type": kind, "snapshot_id": snapshot_id,
                              "timestamp": raw.captured_at, "trade_id": trade.id if trade else None,
                              "state": trade.state if trade else None, "trade_document": doc,
                              "policy_hash": doc["policy_hash"] if doc else self.hashes[cursor.strategy_id],
                              "policy": doc["policy"] if doc else self.policies[cursor.strategy_id],
                              "exit_policy_version": doc.get("exit_policy_version") if doc else mirror_exit.VERSION,
                              **details})
        cursor.sequence += 1
        hashed = digest({"strategy_id": cursor.strategy_id, "sequence": cursor.sequence,
                         "event_key": key, "previous_hash": cursor.head_hash, "payload": payload})
        session.add(ParallelPaperEvent(
            strategy_id=cursor.strategy_id, sequence=cursor.sequence,
            event_key=key, event_type=kind, snapshot_id=snapshot_id,
            trade_id=trade.id if trade else None, previous_hash=cursor.head_hash,
            event_hash=hashed, payload=canonical(payload)))
        cursor.head_hash = hashed

    def _entry_valid(self, doc: dict, raw: ScalperMarketSnapshot, bars: list[Bar]) -> str | None:
        if ((raw.future_instrument_id, raw.future_expiry.isoformat() if raw.future_expiry else None)
                != (doc["future_instrument_id"], doc["future_expiry"]) or raw.nifty_future is None):
            return "FUTURES_REFERENCE_CHANGED"
        direction, features, strategy = doc["direction"], doc["strategy_features"], doc["strategy_id"]
        if strategy.startswith("ORB_"):
            aligned = (raw.nifty_future > features["opening_high"] if direction == "BULL"
                       else raw.nifty_future < features["opening_low"])
        else:
            usable = contiguous_bars(bars)
            if strategy == "EMA_PULLBACK":
                closes = [bar.close for bar in usable]
                fast, slow = ema(closes, self.experiment.ema_fast), ema(closes, self.experiment.ema_slow)
                aligned = bool(fast and slow and fast[-1] is not None and slow[-1] is not None
                               and (fast[-1] > slow[-1] if direction == "BULL" else fast[-1] < slow[-1]))
            else:
                aligned = supertrend(usable, self.experiment.atr_period,
                                     self.experiment.supertrend_multiplier)["supertrend_direction"] == direction
        return None if aligned else "PENDING_STRATEGY_THESIS_INVALID"

    @staticmethod
    def _risk(rows: list[ParallelPaperTrade], config: ScalperConfig, at: datetime, blocked: bool) -> list[str]:
        ledger = Ledger(trades=[_lab(row) for row in rows])
        reasons = _risk_blockers(ledger, config, at, blocked)
        # A recovered prior-session OPEN position may realize its loss today.
        # Include exit-day P&L/cooldowns without weakening the shared risk checks.
        closed = sorted((trade.exit_timestamp, float(trade.outcome["gross_rupees"]))
                        for trade in ledger.trades if trade.state == "CLOSED"
                        and trade.exit_timestamp is not None and trade.exit_timestamp <= at
                        and trade.outcome is not None)
        today = [pnl for timestamp, pnl in closed if timestamp.astimezone(IST).date()
                 == at.astimezone(IST).date()]
        if sum(today) <= -config.hard_daily_loss:
            reasons.append("HARD_DAILY_LOSS_REACHED")
        streak = 0
        for pnl in reversed(today):
            if pnl >= 0:
                break
            streak += 1
        if streak >= config.max_consecutive_losses:
            reasons.append("MAX_CONSECUTIVE_LOSSES_REACHED")
        if closed and (at - closed[-1][0]).total_seconds() < config.cooldown_after_exit_seconds:
            reasons.append("ANY_EXIT_COOLDOWN_ACTIVE")
        losses = [timestamp for timestamp, pnl in closed if pnl < 0]
        if losses and (at - losses[-1]).total_seconds() < config.cooldown_after_loss_seconds:
            reasons.append("LOSS_COOLDOWN_ACTIVE")
        return list(dict.fromkeys(reasons))

    def _transition(self, session: Session, cursor: ParallelPaperCursor, snapshot_id: int,
                    raw: ScalperMarketSnapshot, points: list[MarketPoint], bars: list[Bar], trade: ParallelPaperTrade,
                    rows: list[ParallelPaperTrade], blocked: bool) -> None:
        if self._terminalize_expired(session, cursor, snapshot_id, raw, trade):
            return
        if trade.state == "UNRESOLVED_EXPOSURE":
            return
        config = self.configs[trade.strategy_id]
        doc = json.loads(trade.document)
        if (doc["policy_hash"] != self.hashes[trade.strategy_id] or
                doc.get("exit_policy_version") != self.policies[trade.strategy_id]["exit_policy_version"]):
            trade.state = "UNRESOLVED_EXPOSURE"
            doc["unresolved_reason"] = "EXECUTION_POLICY_CHANGED"
            trade.document = canonical(doc)
            self._emit(session, cursor, snapshot_id, raw, "UNRESOLVED", trade,
                       reason=doc["unresolved_reason"])
            return
        candidate = ScalperCandidate.model_validate(doc["candidate"])
        # Exit quantity is the original trade's confirmed quantity, never the new expiry's lot size.
        book = raw.model_copy(update={"lot_size": doc["lot_size"]})
        pending = trade.state == "PENDING_PAPER_ENTRY"
        pair, failure = executable_pair(book, candidate, config, entry=pending, exit_book_policy=exit_book.VERSION)
        if pending:
            age = (raw.captured_at - datetime.fromisoformat(doc["decision_timestamp"])).total_seconds()
            if age <= 0:
                return
            risk = self._risk([row for row in rows if row.id != trade.id], config, raw.captured_at, blocked)
            reason = ("ENTRY_TTL_EXCEEDED" if age > config.entry_ttl_seconds else
                      "EXPIRED_CONTRACT_ENTRY_FORBIDDEN" if candidate.short_leg.expiry < raw.captured_at.date() else
                      "LOT_SIZE_CHANGED" if raw.lot_size != doc["lot_size"] else
                      risk[0] if risk else failure or self._entry_valid(doc, raw, bars))
            if reason is None:
                assert pair is not None
                if (pair.value < config.min_credit or pair.value / candidate.spread_width < config.min_credit_to_width
                        or (candidate.spread_width - pair.value) * doc["lot_size"] > config.max_loss_per_trade):
                    reason = "FILL_ECONOMICS_OUTSIDE_RISK"
            if reason:
                trade.state = "ENTRY_REJECTED"
                doc["rejection_reason"] = reason
                trade.document = canonical(doc)
                self._emit(session, cursor, snapshot_id, raw, "ENTRY_REJECTED", trade, reason=reason)
                return
            assert pair is not None
            trade.state = "OPEN"
            doc.update(entry_timestamp=raw.captured_at.isoformat(), entry_snapshot_id=snapshot_id,
                       entry_credit=pair.value, entry_quotes=pair.evidence,
                       underlying_entry_price=raw.nifty_future,
                       defined_max_risk=(candidate.spread_width - pair.value) * doc["lot_size"],
                       best_gross_points=0.0, mae=0.0, mfe=0.0)
            trade.document = canonical(doc)
            self._emit(session, cursor, snapshot_id, raw, "ENTRY", trade,
                       fill_method="NEXT_OBSERVATION_BID_ASK")
            return
        if trade.state != "OPEN":
            return
        features = doc["strategy_features"]
        if (trade.strategy_id.startswith("ORB_") and raw.nifty_future is not None
                and raw.future_instrument_id == doc["future_instrument_id"]
                and raw.future_expiry is not None and raw.future_expiry.isoformat() == doc["future_expiry"]):
            if features["opening_low"] <= raw.nifty_future <= features["opening_high"]:
                doc.setdefault("false_breakout_first_at", raw.captured_at.isoformat())
            opposite = (raw.nifty_future < features["opening_low"] if doc["direction"] == "BULL"
                        else raw.nifty_future > features["opening_high"])
            if opposite:
                doc.setdefault("breakout_reversal_first_at", raw.captured_at.isoformat())
        if failure:
            terminal = (failure == "MISSING_EXACT_CONTRACT" or
                        raw.captured_at.astimezone(IST).time().replace(tzinfo=None) >= config.forced_exit_time)
            if doc.get("exit_policy_version") == mirror_exit.VERSION and not terminal:
                mirror_exit.parallel_signal(doc, points, bars, raw.captured_at, self.experiment, available=False)
                trade.document = canonical(doc)
            if terminal:
                trade.state = "UNRESOLVED_EXPOSURE"
                doc["unresolved_reason"] = failure
                trade.document = canonical(doc)
                self._emit(session, cursor, snapshot_id, raw, "UNRESOLVED", trade, reason=failure)
            else:
                trade.document = canonical(doc)
                self._emit(session, cursor, snapshot_id, raw, "MARK", trade, available=False, reason=failure)
            return
        assert pair is not None
        lab = _lab(trade)
        assert lab.entry_credit is not None and lab.entry_timestamp is not None
        pnl = lab.entry_credit - pair.value
        lab.mae, lab.mfe = min(lab.mae, pnl), max(lab.mfe, pnl)
        context = {"timestamp": raw.captured_at.isoformat(), "spot": raw.nifty_spot, "lot_size": doc["lot_size"]}
        doc.update(current_debit=pair.value, gross_mark_points=pnl,
                   last_snapshot_id=snapshot_id,
                   gross_mark_rupees=pnl * doc["lot_size"], mae=lab.mae, mfe=lab.mfe,
                   mark_quality_warning=exit_book.warning(pair.evidence),
                   best_gross_points=max(lab.best_gross_points, pnl))
        if doc.get("exit_policy_version") == mirror_exit.VERSION:
            mirror_exit.legacy_diagnostics(doc, raw.captured_at, pair.value, config)
            reason = mirror_exit.safety_reason(doc, raw.captured_at, pair.value, config,
                                               event_blocked=blocked)
            if reason is None:
                reason = mirror_exit.parallel_signal(
                    doc, points, bars, raw.captured_at, self.experiment, available=True)
        else:
            reason = _exit_reason(lab, context, pair.value, config, event_blocked=blocked)
        trade.document = canonical(doc)
        self._emit(session, cursor, snapshot_id, raw, "MARK", trade, available=True, quotes=pair.evidence,
                   mark_quality_warning=doc["mark_quality_warning"])
        if reason is None:
            return
        outcome = self.costs.calculate(
            day=raw.captured_at.date(), lot_size=doc["lot_size"], lots=1,
            entry_short=doc["entry_quotes"]["short"]["fill_price"],
            entry_long=doc["entry_quotes"]["long"]["fill_price"],
            exit_short=pair.short_price, exit_long=pair.long_price)
        doc.update(exit_timestamp=raw.captured_at.isoformat(), exit_snapshot_id=snapshot_id,
                   exit_reason=reason, exit_debit=pair.value, exit_quotes=pair.evidence,
                   holding_seconds=(raw.captured_at - lab.entry_timestamp).total_seconds(),
                   final_outcome=outcome, gross_pnl=outcome["gross_rupees"],
                   net_pnl=outcome["net_rupees"],
                   net_estimated_pnl=outcome["gross_rupees"] - outcome["known_partial_cost_rupees"],
                   net_estimate_status=outcome["cost_completeness"],
                   mfe_rupees=lab.mfe * doc["lot_size"], mae_rupees=lab.mae * doc["lot_size"])
        trade.state, trade.document = "CLOSED", canonical(doc)
        self._emit(session, cursor, snapshot_id, raw, "EXIT", trade, reason=reason)

    def _process(self, strategy: str, snapshot_id: int, raw: ScalperMarketSnapshot,
                 points: list[MarketPoint], bars: list[Bar], source: dict) -> str:
        config = self.configs[strategy]
        with self.sessions.begin() as session:
            locked = session.execute(update(ParallelPaperCursor).where(
                ParallelPaperCursor.strategy_id == strategy).values(
                    lock_version=ParallelPaperCursor.lock_version + 1))
            if not isinstance(locked, CursorResult) or locked.rowcount != 1:
                raise RuntimeError("PARALLEL_PAPER_MIGRATION_REQUIRED")
            cursor = session.get(ParallelPaperCursor, strategy)
            assert cursor is not None
            if session.scalar(select(ParallelPaperEvent.sequence).where(
                    ParallelPaperEvent.strategy_id == strategy,
                    ParallelPaperEvent.event_key == f"{snapshot_id}:SESSION:OBSERVATION")) is not None:
                return "DUPLICATE"
            if cursor.observed_at and raw.captured_at <= datetime.fromisoformat(cursor.observed_at):
                raise ValueError("PARALLEL_PAPER_OUT_OF_ORDER")
            rows = self._trades(session, strategy)
            for trade in rows:
                if trade.state in ACTIVE:
                    self._terminalize_expired(session, cursor, snapshot_id, raw, trade)
            self._assert_entry_policy_upgrade_safe(session, raw.captured_at)
            blocked = any(event.block_entries for event in self.events[strategy].events_at(raw.captured_at))
            for trade in rows:
                if trade.state in ACTIVE:
                    self._transition(session, cursor, snapshot_id, raw, points, bars, trade, rows, blocked)
            state = json.loads(cursor.state_json)
            decision = decide(strategy, points, bars, raw.captured_at, state, self.experiment)
            decision.update(snapshot_id=snapshot_id, market_data=source, policy_hash=self.hashes[strategy])
            # Management above always runs. An ORB signal while exposure remains
            # active is a diagnostic, not another attempted/rejected trade.
            if (strategy.startswith("ORB_") and decision["status"] == "ENTER"
                    and any(row.state in ACTIVE for row in rows)):
                decision["status"] = "BLOCKED"
                decision["blockers"] = ["ACTIVE_POSITION_BLOCKS_ENTRY"]
            if decision["status"] == "ENTER":
                built = build_candidates(raw, _construction_signal(raw, decision["direction"]), config)
                candidate = built.candidates[0] if built.candidates else None
                reasons = self._risk(rows, config, raw.captured_at, blocked)
                if candidate is None:
                    reasons.append("NO_EXECUTABLE_PROTECTED_SPREAD")
                elif candidate.short_leg.expiry < raw.captured_at.date():
                    reasons.append("EXPIRED_CONTRACT_ENTRY_FORBIDDEN")
                if not raw.future_instrument_id or raw.future_expiry is None or raw.nifty_future is None:
                    reasons.append("FUTURES_REFERENCE_UNAVAILABLE")
                decision["candidate_rejections"] = built.rejection_counts
                decision["blockers"] = list(dict.fromkeys([*decision["blockers"], *reasons]))
                if reasons:
                    decision["status"] = "ENTRY_REJECTED"
                else:
                    assert candidate is not None
                    entry_committed(strategy, decision, state)
                    trade_id = digest([VERSIONS[strategy], snapshot_id, candidate.candidate_id])
                    doc = json_value({"strategy_id": strategy, "strategy_version": VERSIONS[strategy],
                                      "trade_id": trade_id, "policy_hash": self.hashes[strategy],
                                      "policy": self.policies[strategy],
                                      "exit_policy_version": mirror_exit.VERSION,
                                      "decision_timestamp": raw.captured_at, "direction": decision["direction"],
                                      "decision": {**decision, "thesis": {}}, "strategy_features": decision["features"],
                                      "entry_reason": decision["entry_reason"], "candidate": candidate,
                                      "short_strike": candidate.short_leg.strike,
                                      "hedge_strike": candidate.long_leg.strike,
                                      "lot_size": raw.lot_size, "lots": 1,
                                      "future_instrument_id": raw.future_instrument_id,
                                      "future_expiry": raw.future_expiry})
                    trade = ParallelPaperTrade(
                        id=trade_id, strategy_id=strategy, strategy_version=VERSIONS[strategy],
                        decision_snapshot_id=snapshot_id,
                        state="PENDING_PAPER_ENTRY", document=canonical(doc))
                    session.add(trade)
                    session.flush()
                    self._emit(session, cursor, snapshot_id, raw, "PENDING_PAPER_ENTRY", trade)
                    decision["status"] = "PENDING_PAPER_ENTRY"
            cursor.snapshot_id, cursor.observed_at = snapshot_id, raw.captured_at.isoformat()
            cursor.state_json, cursor.latest_json = canonical(state), canonical(decision)
            self._emit(session, cursor, snapshot_id, raw, "OBSERVATION", strategy_state=state, decision=decision)
            return decision["status"]

    def process_snapshot(self, snapshot_id: int, *, skip_strategies: set[str] | None = None) -> dict[str, str]:
        with self.sessions() as session:
            record = session.get(ScalperMarketSnapshotRecord, snapshot_id)
            if record is None or json.loads(record.context_json or "{}").get("mode") != CAPTURE_MODE:
                raise ValueError("PARALLEL_PAPER_CAPTURE_REQUIRED")
        raw = self.market.snapshots.get_snapshot(snapshot_id)
        assert raw is not None
        points, source = self.market.series(raw.captured_at, max(
            config.max_quote_age_seconds for config in self.configs.values()))
        bars = build_bars(points, raw.captured_at, self.experiment)
        statuses = {}
        for strategy in STRATEGIES:
            if strategy in (skip_strategies or set()):
                statuses[strategy] = "FAILED"
                continue
            try:
                statuses[strategy] = self._process(strategy, snapshot_id, raw, points, bars, source)
            except Exception as exc:
                logger.error("PARALLEL_PAPER_FAILED strategy=%s snapshot=%d error_type=%s",
                             strategy, snapshot_id, type(exc).__name__)
                statuses[strategy] = "FAILED"
        return statuses

    def pending_snapshot_ids(self, limit: int = 200) -> list[int]:
        record = ScalperMarketSnapshotRecord
        with self.sessions() as session:
            cursors = session.scalars(select(ParallelPaperCursor)).all()
            timestamps = [cursor.observed_at for cursor in cursors if cursor.observed_at is not None]
            query = select(record.id).where(record.context_json.like(f'%"mode":"{CAPTURE_MODE}"%'))
            if len(timestamps) == len(STRATEGIES):
                query = query.where(record.captured_at > datetime.fromisoformat(min(timestamps)))
            return list(session.scalars(query.order_by(record.captured_at, record.id).limit(limit)))

    def required_contracts(self, at: datetime | None = None) -> list[OptionContractSnapshot]:
        contracts = self.market.basket(at or datetime.now().astimezone())
        with self.sessions() as session:
            rows = session.scalars(select(ParallelPaperTrade).where(
                ParallelPaperTrade.state.in_(("PENDING_PAPER_ENTRY", "OPEN")))).all()
            for row in rows:
                doc = json.loads(row.document)
                if expiry.expired(doc, at or datetime.now(IST)):
                    continue
                candidate = doc["candidate"]
                contracts.extend(OptionContractSnapshot.model_validate(candidate[name])
                                 for name in ("short_leg", "long_leg"))
        return list({(leg.exchange, leg.instrument_token, leg.expiry, leg.strike,
                      leg.option_type, leg.trading_symbol): leg for leg in contracts}.values())
