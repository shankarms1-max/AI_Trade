"""Bounded one-exposure forward paper bridge, consuming only persisted SQL data.

Every transition and its evidence commit in one serialized transaction. The
singleton cursor serializes workers across processes; journal keys and decision
uniqueness also protect restart/retry paths. No external client is constructed.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from time import perf_counter
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select, update
from sqlalchemy.orm import selectinload

from app.db.models import (MarketSnapshotRecord, MarketRegimeSnapshotRecord,
                           MarketFeatureSnapshotRecord, AlphaFeatureSnapshotRecord,
                           RiskDecisionRecord, StrategyCandidateSetRecord, ShadowTradeRecord)
from app.features.repository import raw_record_to_model
from app.paper.models import PaperCursor, PaperEvent, PaperTrade
from app.research.config import ReplayIntegrityConfig
from app.research.costs import CostSchedule
from app.research.manifest import canonical, digest, implementation_hash, json_value
from app.research.quotes import (ContractIdentity, QuotePolicy, execution_pair,
                                 exact_contract, information_time, validate_book)
from app.risk.exposure import RiskState
from app.risk.candidate_checks import strategy_fingerprint
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.risk.models import RISK_VERSION
from app.shadow.exits import exit_reason
from app.shadow.service import config_from_settings as exit_config
from app.strategy.models import CreditSpreadCandidate

IST = ZoneInfo("Asia/Kolkata")
ACTIVE = ("PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE")


def execution_mode(settings):
    if settings.forward_paper_enabled:
        return "PAPER"
    if settings.pipeline_decision_only:
        return "DECISION_ONLY"
    if not settings.pipeline_after_snapshot:
        return "CAPTURE_ONLY"
    return "UNAVAILABLE"  # Legacy shadow is not the forward paper engine.


class PaperEngine:
    def __init__(self, sessions, settings, *, clock=None):
        if not settings.forward_paper_enabled:
            raise ValueError("FORWARD_PAPER_DISABLED")
        # Recheck when constructed programmatically (model_copy bypasses validators).
        settings.validate_forward_paper_mode()
        self.sessions, self.settings = sessions, settings
        self.clock = clock or (lambda: datetime.now(IST))
        self.integrity = ReplayIntegrityConfig(enabled=True, quote_policy=QuotePolicy(
            max_spread_percent=min(settings.risk_max_bid_ask_spread_pct,
                                   settings.strategy_max_bid_ask_spread_pct)))
        self.exits = exit_config(settings)
        # Existing scalar settings lack a dated schedule/version and SEBI input.
        # Keep the observed gross result; never promote them to complete net costs.
        self.costs = CostSchedule(brokerage_per_order=settings.brokerage_per_order,
            exchange_rate=settings.exchange_rate, stt_rate=settings.stt_rate,
            gst_rate=settings.gst_rate, stamp_rate=settings.stamp_rate,
            slippage_points_per_leg=settings.slippage_points_per_leg,
            declared_complete=settings.costs_complete)
        # Allowlisted config only: no credentials/environment values enter evidence.
        from app.pipeline.service import shadow_risk_config_from_settings
        from app.strategy.service import config_from_settings as strategy_config
        from app.regime.engine import config_from_settings as regime_config
        from app.features.engine import config_from_settings as feature_config
        from app.alpha.engine import config_from_settings as alpha_config
        self.risk = shadow_risk_config_from_settings(settings)
        self.events = ConfiguredMarketEventProvider.from_json(settings.risk_market_events_json)
        self.policy = json_value(dict(version="forward_paper_v1", integrity=self.integrity,
            exits=self.exits, risk=self.risk, strategy=strategy_config(settings),
            regime=regime_config(settings), features=feature_config(settings),
            alpha=alpha_config(settings), alpha_enabled=settings.alpha_engine_enabled,
            costs=self.costs, lots=1, event_configuration_hash=digest(settings.risk_market_events_json),
            source_tree_hash=implementation_hash()))
        self.policy_hash = digest(self.policy)
        verify_journal(sessions)

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as session:
            # Migration seeds this row. Tests using create_all seed it explicitly.
            changed = session.execute(update(PaperCursor).where(PaperCursor.id == 1)
                .values(lock_version=PaperCursor.lock_version + 1)).rowcount
            if changed != 1:
                raise RuntimeError("FORWARD_PAPER_MIGRATION_REQUIRED")
            cursor = session.get(PaperCursor, 1)
            # Refuse coexistence with ALL legacy open trades, across versions.
            if session.scalar(select(ShadowTradeRecord.id).where(ShadowTradeRecord.status == "OPEN").limit(1)):
                raise ValueError("FORWARD_PAPER_REQUIRES_NO_LEGACY_EXPOSURE")
            yield session, cursor

    def raw(self, session, snapshot_id):
        row = session.scalar(select(MarketSnapshotRecord).options(selectinload(MarketSnapshotRecord.options))
                             .where(MarketSnapshotRecord.id == snapshot_id))
        if row is None:
            raise LookupError("PAPER_SNAPSHOT_NOT_FOUND")
        return raw_record_to_model(row, normalize_research_timestamps=True)

    def cadence_reason(self, at):
        start = datetime.combine(at.date(), self.integrity.session_start, IST)
        end = datetime.combine(at.date(), self.integrity.session_end, IST)
        if not start <= at <= end + timedelta(seconds=self.integrity.interval_tolerance_seconds):
            return "OUTSIDE_CAPTURE_SESSION"
        elapsed = (at-start).total_seconds()
        nearest = round(elapsed/self.integrity.expected_interval_seconds)*self.integrity.expected_interval_seconds
        if abs(elapsed-nearest) > self.integrity.interval_tolerance_seconds:
            return "OFF_CADENCE_OBSERVATION"
        return None

    def emit(self, session, cursor, kind, snapshot_id, raw, trade=None, **details):
        key = f"{snapshot_id}:{trade.id if trade else 'SESSION'}:{kind}"
        if session.scalar(select(PaperEvent.sequence).where(PaperEvent.event_key == key)):
            raise ValueError("DUPLICATE_PAPER_TRANSITION")
        document = json.loads(trade.document) if trade else None
        payload = json_value(dict(event_type=kind, execution_mode="PAPER", snapshot_id=snapshot_id,
            market_snapshot_timestamp=raw.timestamp_ist, event_timestamp=self.clock(),
            information_timestamp=information_time(raw), policy_hash=self.policy_hash,
            trade_id=trade.id if trade else None, state=trade.state if trade else None,
            trade_document=document, **details))
        cursor.sequence += 1
        hashed = digest(dict(sequence=cursor.sequence, event_key=key,
                             previous_hash=cursor.head_hash, payload=payload))
        session.add(PaperEvent(sequence=cursor.sequence, event_key=key, event_type=kind,
            snapshot_id=snapshot_id, trade_id=trade.id if trade else None,
            previous_hash=cursor.head_hash, event_hash=hashed, payload=canonical(payload)))
        cursor.head_hash = hashed
        session.flush()

    def save(self, trade, document, state=None):
        if state:
            trade.state = state
        trade.document = canonical(document)

    def evidence(self, raw, candidate, details, *, entry):
        result = {}
        for name, leg, quote, side in zip(("short", "long"),
                (candidate.short_leg, candidate.long_leg), details,
                ("bid", "ask") if entry else ("ask", "bid")):
            contract, _ = exact_contract(raw, ContractIdentity.of(leg))
            result[name] = dict(validation=quote.payload(), side=side,
                observed_contract=None if contract is None else contract.model_dump(mode="json"))
        return json_value(result)

    def unresolved(self, session, cursor, row, doc, snapshot_id, raw, reason, **details):
        doc.update(unresolved_reason=reason, last_observation=raw.timestamp_ist.isoformat(),
                   last_snapshot_id=snapshot_id, profitability_claim=False)
        self.save(row, doc, "UNRESOLVED_EXPOSURE")
        self.emit(session, cursor, "UNRESOLVED_EXPOSURE", snapshot_id, raw, row,
                  reason=reason, **details)

    def observe(self, snapshot_id):
        started = perf_counter()
        with self.transaction() as (session, cursor):
            raw = self.raw(session, snapshot_id)
            at = raw.timestamp_ist.astimezone(IST)
            if session.scalar(select(PaperEvent.sequence).where(
                    PaperEvent.event_key == f"{snapshot_id}:SESSION:OBSERVATION")):
                return 0  # Durable pre-stage completion; never fill a newly queued t entry on retry.
            if cursor.observed_at and at <= datetime.fromisoformat(cursor.observed_at):
                raise ValueError("PAPER_OUT_OF_ORDER_OBSERVATION")
            for row in session.scalars(select(PaperTrade).where(PaperTrade.state.in_(ACTIVE))).all():
                doc = json.loads(row.document)
                if row.state == "UNRESOLVED_EXPOSURE":
                    continue  # Terminal research classification; exposure continues blocking entries.
                candidate = CreditSpreadCandidate.model_validate(doc["candidate"])
                previous = datetime.fromisoformat(doc["last_observation"])
                delta = (at-previous).total_seconds()
                reason = None
                if doc["policy_hash"] != self.policy_hash:
                    reason = "PAPER_POLICY_CHANGED"
                elif at.date() != previous.date():
                    reason = "CROSS_SESSION_OBSERVATION"
                elif abs(delta-self.integrity.expected_interval_seconds) > self.integrity.interval_tolerance_seconds:
                    reason = "MISSING_EXPECTED_PATH_OBSERVATION"
                reason = reason or self.cadence_reason(at)
                entry = row.state == "PENDING_PAPER_ENTRY"
                if entry and (information_time(raw)-datetime.fromisoformat(doc["decision_timestamp"])).total_seconds() > self.integrity.max_fill_delay_seconds:
                    reason = "DECISION_TO_FILL_TTL_EXCEEDED"
                if raw.lot_size != doc["lot_size"]:
                    reason = "LOT_SIZE_CHANGED"
                pair, quote_reason, quotes = execution_pair(raw, candidate, entry=entry, lots=1,
                    policy=self.integrity.quote_policy, evaluated_at=self.clock())
                reason = reason or (quote_reason if pair is None else None)
                evidence = self.evidence(raw, candidate, quotes, entry=entry)
                if entry:
                    if at <= datetime.fromisoformat(doc["decision_timestamp"]) or snapshot_id == row.decision_snapshot_id:
                        reason = "SAME_OBSERVATION_FILL_FORBIDDEN"
                    if at.date() >= candidate.expiry:
                        reason = "ZERO_DTE_EXCLUDED"
                    if not self.risk.entry_start_time <= at.time().replace(tzinfo=None) <= self.risk.entry_end_time:
                        reason = "FILL_OUTSIDE_ENTRY_WINDOW"
                    if reason is None:
                        credit = pair[0].fill_price-pair[1].fill_price
                        reason = self.entry_risk(raw, candidate, doc, credit)
                    if reason:
                        doc.update(rejection_reason=reason, attempted_execution_snapshot_id=snapshot_id)
                        self.save(row, doc, "ENTRY_REJECTED")
                        self.emit(session, cursor, "ENTRY_REJECTED", snapshot_id, raw, row,
                                  reason=reason, quotes=evidence)
                    else:
                        doc.update(entry_credit=credit, entry_timestamp=at.isoformat(),
                            execution_snapshot_id=snapshot_id, entry_quotes=evidence,
                            last_observation=at.isoformat(), last_snapshot_id=snapshot_id)
                        self.save(row, doc, "OPEN")
                        self.emit(session, cursor, "ENTRY_EXECUTION", snapshot_id, raw, row,
                                  quotes=evidence, fill_method="NEXT_OBSERVATION_BID_ASK")
                    continue
                if reason:
                    self.unresolved(session, cursor, row, doc, snapshot_id, raw, reason, quotes=evidence)
                    continue
                debit = pair[0].fill_price-pair[1].fill_price
                pnl = doc["entry_credit"]-debit
                if not 0 <= debit <= candidate.spread_width:
                    self.unresolved(session, cursor, row, doc, snapshot_id, raw,
                                    "INVALID_EXECUTABLE_PAYOFF", quotes=evidence)
                    continue
                # Required pre-feature ordering uses ONLY the last persisted prior regime.
                regime_row = session.scalar(select(MarketRegimeSnapshotRecord)
                    .join(MarketSnapshotRecord, MarketSnapshotRecord.id == MarketRegimeSnapshotRecord.market_snapshot_id)
                    .where(MarketSnapshotRecord.timestamp_ist < at,
                           MarketSnapshotRecord.timestamp_ist >= previous,
                           MarketRegimeSnapshotRecord.regime_version == self.settings.active_regime_version)
                    .order_by(MarketSnapshotRecord.timestamp_ist.desc()).limit(1))
                regime = None if regime_row is None else SimpleNamespace(regime=regime_row.regime)
                proxy = SimpleNamespace(entry_credit=doc["entry_credit"], strategy_type=candidate.strategy_type.value,
                                        structural_reference=candidate.support_or_resistance_reference)
                trigger = exit_reason(proxy, SimpleNamespace(pnl_per_unit=pnl, timestamp=at, spot=raw.nifty_spot), regime, self.exits)
                doc.update(last_observation=at.isoformat(), last_snapshot_id=snapshot_id,
                           current_debit=debit, gross_mark_per_unit=pnl)
                self.save(row, doc)
                self.emit(session, cursor, "MARK", snapshot_id, raw, row, quotes=evidence,
                          prior_regime=None if regime_row is None else regime_row.result_json,
                          trigger_regime_snapshot_id=None if regime_row is None else regime_row.market_snapshot_id)
                if trigger:
                    accounting = self.costs.calculate(day=at.date(), lot_size=doc["lot_size"], lots=1,
                        entry_short=doc["entry_quotes"]["short"]["validation"]["fill_price"],
                        entry_long=doc["entry_quotes"]["long"]["validation"]["fill_price"],
                        exit_short=pair[0].fill_price, exit_long=pair[1].fill_price)
                    doc.update(exit_snapshot_id=snapshot_id, exit_timestamp=at.isoformat(),
                        exit_reason=trigger, exit_quotes=evidence, final_outcome=accounting)
                    self.save(row, doc, "CLOSED")
                    self.emit(session, cursor, "EXIT_EXECUTION", snapshot_id, raw, row, quotes=evidence)
                    self.emit(session, cursor, "FINAL_OUTCOME", snapshot_id, raw, row, accounting=accounting,
                              profitability_claim=False)
            cursor.snapshot_id, cursor.observed_at = snapshot_id, at.isoformat()
            self.emit(session, cursor, "OBSERVATION", snapshot_id, raw,
                      raw_snapshot_hash=digest(raw), raw_snapshot=raw,
                      runtime_ms=round((perf_counter()-started)*1000, 3))
        return 0

    def entry_risk(self, raw, candidate, doc, credit):
        if any(event.block_entries for event in self.events.events_at(raw.timestamp_ist)):
            return "FILL_MARKET_EVENT_BLOCKED"
        if not 0 < credit < candidate.spread_width:
            return "INVALID_EXECUTABLE_PAYOFF"
        # Reject deteriorated economics beyond the exact approved monetary envelope.
        loss = (candidate.spread_width-credit)*raw.lot_size
        cap = doc["risk_decision"].get("max_loss_per_lot")
        if cap is None or loss > cap + 1e-8:
            return "FILL_EXCEEDS_APPROVED_MAX_LOSS"
        if credit < self.risk.min_net_credit or credit/candidate.spread_width < self.risk.min_credit_to_width:
            return "FILL_CREDIT_RISK_REJECTED"
        for limit in (self.risk.max_loss_per_trade, self.risk.max_capital_per_trade):
            if limit is None or loss > limit:
                return "FILL_MONETARY_LIMIT_REJECTED"
        for leg, oi_min, vol_min in ((candidate.short_leg, self.risk.min_short_oi, self.risk.min_short_volume),
                                     (candidate.long_leg, self.risk.min_long_oi, self.risk.min_long_volume)):
            contract, _ = exact_contract(raw, ContractIdentity.of(leg))
            if contract.open_interest is None or contract.open_interest < oi_min:
                return "FILL_OI_RISK_REJECTED"
            if self.risk.require_volume and (contract.volume is None or contract.volume < vol_min):
                return "FILL_VOLUME_RISK_REJECTED"
        return None

    def enqueue(self, snapshot_id):
        with self.transaction() as (session, cursor):
            raw = self.raw(session, snapshot_id)
            if session.scalar(select(PaperTrade.id).where(PaperTrade.decision_snapshot_id == snapshot_id)):
                return None
            if cursor.snapshot_id != snapshot_id:
                raise ValueError("PAPER_PRE_OBSERVATION_REQUIRED")
            if session.scalar(select(PaperTrade.id).where(PaperTrade.state.in_(ACTIVE)).limit(1)):
                return None
            result = session.scalar(select(StrategyCandidateSetRecord).where(
                StrategyCandidateSetRecord.market_snapshot_id == snapshot_id,
                StrategyCandidateSetRecord.strategy_version == self.settings.active_strategy_version))
            if result is None:
                return None
            approved = session.scalars(select(RiskDecisionRecord).where(
                RiskDecisionRecord.market_snapshot_id == snapshot_id,
                RiskDecisionRecord.strategy_version == self.settings.active_strategy_version,
                RiskDecisionRecord.risk_version == RISK_VERSION,
                RiskDecisionRecord.strategy_candidate_set_id == result.id,
                RiskDecisionRecord.decision == "APPROVED").order_by(RiskDecisionRecord.id)).all()
            by_id = {item["candidate_id"]: item for item in result.result_json["candidates"]}
            if len(by_id) != len(result.result_json["candidates"]):
                raise ValueError("AMBIGUOUS_PAPER_CANDIDATE_IDENTITY")
            approved = [item for item in approved if item.result_json["candidate_reference"] in by_id]
            if not approved:
                return None
            chosen = max(approved, key=lambda item: (item.result_json.get("selection_score") or 0, -item.id))
            candidate = CreditSpreadCandidate.model_validate(by_id[chosen.result_json["candidate_reference"]])
            day = raw.timestamp_ist.astimezone(IST).date()
            state = self.state(session, day)
            reason = None
            if day >= candidate.expiry:
                reason = "ZERO_DTE_EXCLUDED"
            elif self.cadence_reason(raw.timestamp_ist.astimezone(IST)):
                reason = self.cadence_reason(raw.timestamp_ist.astimezone(IST))
            elif not state.monetary_pnl_complete:
                reason = "UNRESOLVED_OR_GROSS_ONLY_MONETARY_STATE"
            elif state.trades_today >= min(self.risk.max_trades_per_day, self.exits.max_new_trades_per_day):
                reason = "DAILY_PAPER_LIMIT_REACHED"
            elif self.risk.max_daily_loss is None or state.realized_pnl_today <= -self.risk.max_daily_loss:
                reason = "DAILY_MONETARY_RISK_UNAVAILABLE_OR_REACHED"
            if raw.lot_size is None or raw.lot_size <= 0 or raw.lot_size != candidate.lot_size:
                reason = "LOT_SIZE_UNAVAILABLE_OR_CHANGED"
            if candidate.market_snapshot_id != snapshot_id or candidate.regime_snapshot_id != chosen.regime_snapshot_id:
                reason = "DECISION_PROVENANCE_MISMATCH"
            # Also require exact, valid decision-time books. No LTP candidate fallback.
            for leg in (candidate.short_leg, candidate.long_leg):
                contract, failure = exact_contract(raw, ContractIdentity.of(leg))
                if contract is None:
                    reason = reason or failure
                else:
                    book = validate_book(contract, raw.timestamp_ist, self.integrity.quote_policy, evaluated_at=self.clock())
                    if not book.valid:
                        reason = reason or book.reason
            decision_at = max(information_time(raw), self.clock())
            feature = session.scalars(select(MarketFeatureSnapshotRecord).where(
                MarketFeatureSnapshotRecord.market_snapshot_id == snapshot_id)
                .order_by(MarketFeatureSnapshotRecord.id)).all()
            alpha = session.scalars(select(AlphaFeatureSnapshotRecord).where(
                AlphaFeatureSnapshotRecord.market_snapshot_id == snapshot_id)
                .order_by(AlphaFeatureSnapshotRecord.id)).all()
            regime = session.get(MarketRegimeSnapshotRecord, candidate.regime_snapshot_id)
            doc = json_value(dict(candidate=candidate, risk_decision=chosen.result_json,
                feature_evidence=[dict(id=row.id, feature=row.feature_json) for row in feature],
                alpha_evidence=[dict(id=row.id, alpha=row.result_json) for row in alpha],
                regime_evidence=None if regime is None else dict(id=regime.id, regime=regime.result_json),
                risk_decision_id=chosen.id, risk_decision_hash=digest(chosen.result_json),
                strategy_version=self.settings.active_strategy_version,
                regime_version=self.settings.active_regime_version, policy_hash=self.policy_hash,
                policy=self.policy, decision_snapshot_id=snapshot_id,
                decision_timestamp=decision_at, decision_market_timestamp=raw.timestamp_ist,
                decision_raw_hash=digest(raw), last_observation=raw.timestamp_ist,
                last_snapshot_id=snapshot_id, lot_size=raw.lot_size, lots=1, profitability_claim=False))
            row = PaperTrade(id=digest(["forward_paper_v1", snapshot_id, candidate.candidate_id]),
                decision_snapshot_id=snapshot_id, state="PENDING_PAPER_ENTRY", document=canonical(doc))
            session.add(row)
            session.flush()
            self.emit(session, cursor, "DECISION_CREATED", snapshot_id, raw, row)
            if reason:
                doc["rejection_reason"] = reason
                self.save(row, doc, "ENTRY_REJECTED")
                self.emit(session, cursor, "ENTRY_REJECTED", snapshot_id, raw, row, reason=reason)
            else:
                self.emit(session, cursor, "ENTRY_PENDING", snapshot_id, raw, row)
        return None

    @staticmethod
    def state(session, day):
        rows = session.scalars(select(PaperTrade)).all()
        documents = [(row.state, json.loads(row.document)) for row in rows]
        entries = [doc for _, doc in documents if doc.get("entry_timestamp", "")[:10] == str(day)]
        outcomes = [doc.get("final_outcome", {}) for state, doc in documents
                    if state == "CLOSED" and doc.get("exit_timestamp", "")[:10] == str(day)]
        incomplete = any(state == "UNRESOLVED_EXPOSURE" for state, _ in documents) or any(
            item.get("net_rupees") is None for item in outcomes)
        keys = frozenset(strategy_fingerprint(CreditSpreadCandidate.model_validate(doc["candidate"]), day)
                         for state, doc in documents if state in ACTIVE)
        return RiskState(len(entries), sum(item.get("net_rupees") or 0 for item in outcomes), keys,
                         False, True, "SHADOW", not incomplete)

    def get_state(self, trading_date):
        with self.sessions() as session:
            return self.state(session, trading_date)


def assert_no_paper_exposure(sessions):
    with sessions() as session:
        # Keep capture/decision-only deployments compatible with schema 0014.
        if inspect(session.connection()).has_table("paper_trades") and session.scalar(
                select(PaperTrade.id).where(PaperTrade.state.in_(ACTIVE)).limit(1)):
            raise ValueError("MODE_CHANGE_REQUIRES_NO_FORWARD_PAPER_EXPOSURE")


def paper_summary(sessions):
    with sessions() as session:
        if not inspect(session.connection()).has_table("paper_trades"):
            return None  # Not migrated is not an authoritative zero count.
        from sqlalchemy import func
        counts = dict(session.execute(select(PaperTrade.state, func.count()).group_by(PaperTrade.state)).all())
        return {"pending": counts.get("PENDING_PAPER_ENTRY", 0), "open": counts.get("OPEN", 0),
                "closed": counts.get("CLOSED", 0), "unresolved": counts.get("UNRESOLVED_EXPOSURE", 0),
                "rejected": counts.get("ENTRY_REJECTED", 0), "profitability_claim": False}


def verify_journal(sessions):
    with sessions.begin() as session:
        # Hold the same PostgreSQL cursor lock as transitions during the audit.
        cursor = session.scalar(select(PaperCursor).where(PaperCursor.id == 1).with_for_update())
        head, sequence = "GENESIS", 0
        projections = {}
        for item in session.scalars(select(PaperEvent).order_by(PaperEvent.sequence)):
            sequence += 1
            expected = digest(dict(sequence=sequence, event_key=item.event_key,
                                   previous_hash=head, payload=json.loads(item.payload)))
            if item.sequence != sequence or item.previous_hash != head or item.event_hash != expected:
                raise ValueError("PAPER_JOURNAL_INTEGRITY_FAILURE")
            head = expected
            payload = json.loads(item.payload)
            if (payload["event_type"] != item.event_type or payload["snapshot_id"] != item.snapshot_id
                    or payload["trade_id"] != item.trade_id
                    or item.event_key != f"{item.snapshot_id}:{item.trade_id or 'SESSION'}:{item.event_type}"):
                raise ValueError("PAPER_JOURNAL_COLUMN_MISMATCH")
            if item.trade_id:
                projections[item.trade_id] = (payload["state"], canonical(payload["trade_document"]))
        if cursor is None or cursor.sequence != sequence or cursor.head_hash != head:
            raise ValueError("PAPER_JOURNAL_CURSOR_MISMATCH")
        actual = {row.id: (row.state, row.document) for row in session.scalars(select(PaperTrade))}
        if actual != projections:
            raise ValueError("PAPER_PROJECTION_INTEGRITY_FAILURE")
        return sequence
