"""Versioned ACTS paper execution over the isolated Phase 15 capture stream."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta
import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.acts_paper import acts_v2
from app.acts_paper.alpha import ACTS_ALPHA_SOURCE, ActsAlphaRepository
from app.acts_paper.diagnostics import alpha_readiness, regime_readiness
from app.alpha.engine import config_from_settings
from app.core.logging import get_logger
from app.data.models import OptionContractSnapshot
from app.paper import exit_book, expiry, mirror_exit
from app.research.manifest import canonical, digest, json_value
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.scalper.config import ScalperConfig
from app.scalper.models import ScalperCandidate, ScalperMarketSnapshot
from app.scalper.paper import cost_schedule, executable_pair
from app.scalper.research_capture import ACTS_PAPER_MODE
from app.scalper.repository import IST, ScalperRepository
from app.scalper_lab import acts
from app.scalper_lab.alpha_bridge import load_local_alpha
from app.scalper_lab.context import ResearchObservation, context_at
from app.scalper_lab.engine import LabTrade, Ledger, _candidate, _exit_reason, _risk_blockers

logger = get_logger(__name__)
ACTIVE = ("PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE")
INTERVAL_SECONDS = 15
V2_ENTRY_POLICY_VERSION = "ACTS_V2_EXPIRY_DAY_ENTRY_V1"


def _at(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _lab_trade(row: ActsPaperTrade) -> LabTrade | None:
    doc = json.loads(row.document)
    if doc.get("candidate") is None:
        return None
    trade = LabTrade(doc.get("strategy_version", acts.STRATEGY_ID),
                     ScalperCandidate.model_validate(doc["candidate"]),
                     doc["decision"], row.decision_snapshot_id,
                     state={"PENDING_PAPER_ENTRY": "PENDING_ENTRY",
                            "UNRESOLVED_EXPOSURE": "UNRESOLVED"}.get(row.state, row.state),
                     entry_credit=doc.get("entry_credit"),
                     entry_timestamp=_at(doc.get("entry_timestamp")),
                     entry_quotes=doc.get("entry_quotes"),
                     entry_spot=doc.get("entry_spot"),
                     exit_timestamp=_at(doc.get("exit_timestamp")),
                     exit_reason=doc.get("exit_reason"),
                     outcome=doc.get("final_outcome"),
                     expiry_terminalization=expiry.is_unpriced(doc),
                     best_gross_points=doc.get("best_gross_points", 0.0),
                     mae=doc.get("mae", 0.0), mfe=doc.get("mfe", 0.0))
    trade.acts_adverse_since = {key: datetime.fromisoformat(value) for key, value
                                in doc.get("acts_adverse_since", {}).items()}
    trade.acts_last_adverse_at = {key: datetime.fromisoformat(value) for key, value
                                  in doc.get("acts_last_adverse_at", {}).items()}
    return trade


def verify_acts_paper_journal(sessions: sessionmaker[Session]) -> None:
    """Fail startup on a broken chain or a projection detached from its last event."""
    with sessions() as session:
        cursor = session.get(ActsPaperCursor, 1)
        if cursor is None:
            raise RuntimeError("ACTS_PAPER_MIGRATION_REQUIRED")
        previous = "GENESIS"
        latest: dict[str, tuple[str, dict[str, Any]]] = {}
        observation: tuple[int, str, dict[str, Any], dict[str, Any]] | None = None
        events = session.scalars(select(ActsPaperEvent).order_by(
            ActsPaperEvent.sequence)).all()
        for sequence, event in enumerate(events, 1):
            payload = json.loads(event.payload)
            expected = digest({"sequence": sequence, "event_key": event.event_key,
                               "previous_hash": previous, "payload": payload})
            if (event.sequence != sequence or event.previous_hash != previous
                    or event.event_hash != expected
                    or event.event_type != payload.get("event_type")
                    or event.snapshot_id != payload.get("snapshot_id")
                    or event.trade_id != payload.get("trade_id")):
                raise RuntimeError("ACTS_PAPER_JOURNAL_INVALID")
            if event.trade_id is not None:
                latest[event.trade_id] = (payload["state"], payload["trade_document"])
            if event.event_type == "ACTS_OBSERVATION":
                observation = (event.snapshot_id, payload["market_timestamp"],
                               payload["watch"], payload["latest"])
            previous = event.event_hash
        if cursor.sequence != len(events) or cursor.head_hash != previous:
            raise RuntimeError("ACTS_PAPER_JOURNAL_INVALID")
        if observation is not None and (
                cursor.snapshot_id != observation[0]
                or cursor.observed_at != observation[1]
                or json.loads(cursor.watch_json) != observation[2]
                or json.loads(cursor.latest_json) != observation[3]):
            raise RuntimeError("ACTS_PAPER_CURSOR_PROJECTION_INVALID")
        rows = session.scalars(select(ActsPaperTrade)).all()
        if len(rows) != len(latest):
            raise RuntimeError("ACTS_PAPER_PROJECTION_INVALID")
        for row in rows:
            if latest.get(row.id) != (row.state, json.loads(row.document)):
                raise RuntimeError("ACTS_PAPER_PROJECTION_INVALID")
            doc = json.loads(row.document)
            if "policy" in doc and doc.get("policy_hash") != digest(doc["policy"]):
                raise RuntimeError("ACTS_PAPER_TRADE_POLICY_INVALID")


class ActsPaperService:
    """Only database projections and observed bid/ask paper fills; no broker client."""

    def __init__(self, sessions: sessionmaker[Session], settings: Any) -> None:
        self.sessions = sessions
        self.settings = settings
        base = ScalperConfig.from_settings(settings)
        self.config = replace(base, lots=1, allowed_widths=(100, 200, 300, 400),
                              max_trades_per_day=min(base.max_trades_per_day, 3))
        self.costs = cost_schedule(settings)
        self.events = ConfiguredMarketEventProvider.from_json(
            self.config.event_configuration_json)
        self.repository = ScalperRepository(sessions)
        self.alpha_config = config_from_settings(settings)
        self.strategy_id = settings.acts_paper_strategy_version
        if self.strategy_id not in {acts.STRATEGY_ID, acts_v2.STRATEGY_ID}:
            raise ValueError("ACTS_PAPER_STRATEGY_VERSION_INVALID")
        self.v2_config = (acts_v2.ActsV2Config.from_settings(settings)
                          if self.strategy_id == acts_v2.STRATEGY_ID else None)
        self.v1_policy_hash = digest(json_value({
            "strategy_version": acts.STRATEGY_ID, "parameters": acts.PARAMETERS,
            "config": self.config.payload(), "costs": self.costs.payload(),
            "execution_mode": "PAPER", "lots": 1}))
        # Phase 15 V2 construction only; preserve the ACTS V1 compatibility hash.
        if self.v2_config is not None:
            self.config = replace(self.config, allowed_widths=(400,))
        self.policy = (None if self.v2_config is None else json_value({
                                "strategy_version": acts_v2.STRATEGY_ID,
                                "parameters": self.v2_config.payload(),
                                "config": self.config.payload(),
                                "costs": self.costs.payload(),
                                "execution_mode": "PAPER", "lots": 1,
                                "entry_policy_version": V2_ENTRY_POLICY_VERSION,
                                "entry_policy": {"same_day_expiry": "ALLOWED",
                                                 "expired_contracts": "FORBIDDEN"},
                                "exit_policy_version": mirror_exit.VERSION,
                                "exit_book_policy": exit_book.policy(),
                                "exit_policy": mirror_exit.acts_policy(self.config)}))
        self.policy_hash = self.v1_policy_hash if self.policy is None else digest(self.policy)
        verify_acts_paper_journal(sessions)
        if self.v2_config is not None:
            with sessions() as session:
                self._assert_entry_policy_upgrade_safe(session.scalars(select(ActsPaperTrade).where(
                    ActsPaperTrade.state.in_(ACTIVE))).all())

    def _terminalize_expired(self, session: Session, cursor: ActsPaperCursor,
                             snapshot_id: int, raw: ScalperMarketSnapshot, trade: ActsPaperTrade) -> bool:
        doc = json.loads(trade.document)
        if doc.get("strategy_version") != acts_v2.STRATEGY_ID:
            return False
        state = expiry.terminalize(trade.state, doc, raw.captured_at, snapshot_id)
        if state is None:
            return False
        self._save(trade, doc, state)
        self._emit(session, cursor, "ACTS_ENTRY_REJECTED" if state == "ENTRY_REJECTED" else
                   "ACTS_CONTRACT_EXPIRED", snapshot_id, raw, trade,
                   reason=doc.get("rejection_reason") if state == "ENTRY_REJECTED" else "CONTRACT_EXPIRED_UNPRICED",
                   lifecycle_version=expiry.VERSION)
        return True

    def _assert_entry_policy_upgrade_safe(self, trades: Iterable[ActsPaperTrade],
                                          at: datetime | None = None) -> None:
        if self.v2_config is None:
            return
        for trade in trades:
            doc = json.loads(trade.document)
            if (doc.get("strategy_version") == acts_v2.STRATEGY_ID
                    and expiry.expired(doc, at or datetime.now(IST))):
                continue
            if (doc.get("strategy_version") == acts_v2.STRATEGY_ID
                    and doc.get("policy", {}).get("entry_policy_version") != V2_ENTRY_POLICY_VERSION):
                raise RuntimeError("ACTS_V2_ENTRY_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES")
            if (doc.get("strategy_version") == acts_v2.STRATEGY_ID
                    and doc.get("policy", {}).get("exit_book_policy") != exit_book.policy()):
                raise RuntimeError("ACTS_V2_EXIT_BOOK_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES")

    @staticmethod
    def _entry_expiry_reason(raw: ScalperMarketSnapshot, candidate: ScalperCandidate,
                             strategy_id: str) -> str | None:
        if strategy_id == acts_v2.STRATEGY_ID:
            return ("EXPIRED_CONTRACT_ENTRY_FORBIDDEN"
                    if candidate.short_leg.expiry < raw.captured_at.date() else None)
        return ("EXPIRY_DAY_ENTRY_FORBIDDEN"
                if raw.captured_at.date() >= candidate.short_leg.expiry else None)

    def _emit(self, session: Session, cursor: ActsPaperCursor, kind: str,
              snapshot_id: int, raw: ScalperMarketSnapshot,
              trade: ActsPaperTrade | None = None, **details: Any) -> None:
        key = f"{snapshot_id}:{trade.id if trade else 'SESSION'}:{kind}"
        document = json.loads(trade.document) if trade else None
        payload = json_value({
            "event_type": kind, "execution_mode": "PAPER",
            "strategy_version": (document.get("strategy_version", acts.STRATEGY_ID)
                                 if document else self.strategy_id),
            "snapshot_id": snapshot_id,
            "market_timestamp": raw.captured_at,
            "source_market_timestamp": raw.source_market_timestamp,
            "ingestion_timestamp": raw.response_received_at,
            "policy_hash": document["policy_hash"] if document else self.policy_hash,
            "exit_policy_version": (document.get("exit_policy_version") if document else
                                    mirror_exit.VERSION if self.v2_config is not None else None),
            "trade_id": trade.id if trade else None,
            "state": trade.state if trade else None,
            "trade_document": document, **details})
        cursor.sequence += 1
        event_hash = digest({"sequence": cursor.sequence, "event_key": key,
                             "previous_hash": cursor.head_hash, "payload": payload})
        session.add(ActsPaperEvent(
            sequence=cursor.sequence, event_key=key, event_type=kind,
            snapshot_id=snapshot_id, trade_id=trade.id if trade else None,
            previous_hash=cursor.head_hash, event_hash=event_hash,
            payload=canonical(payload)))
        cursor.head_hash = event_hash
        logger.info("%s snapshot_id=%d trade_id=%s reason=%s", kind, snapshot_id,
                    trade.id if trade else "-", details.get("reason", "-"))

    def _observation(self, snapshot_id: int, raw: ScalperMarketSnapshot
                     ) -> ResearchObservation:
        return ResearchObservation(snapshot_id, raw)

    def _alpha(self, observation: ResearchObservation) -> ResearchObservation:
        try:
            matched, _ = load_local_alpha([observation], self.sessions,
                                          raw_source=ACTS_ALPHA_SOURCE)
            return matched[0]
        except (SQLAlchemyError, ValueError) as exc:
            logger.warning("ACTS_ALPHA_UNAVAILABLE error_type=%s", type(exc).__name__)
            return observation

    def _context(self, observation: ResearchObservation) -> dict[str, Any]:
        raw = observation.snapshot
        start = raw.captured_at.astimezone(IST).replace(
            hour=0, minute=0, second=0, microsecond=0)
        # Only the 15-minute option horizon loads full snapshots. The whole
        # session uses five scalar price columns for opening/session structure.
        history = self.repository.history(
            before=raw.captured_at, inclusive=False,
            since=raw.captured_at - timedelta(minutes=16), limit=70,
            capture_mode=ACTS_PAPER_MODE)
        price_history = self.repository.price_history(
            since=start, before=raw.captured_at,
            capture_mode=ACTS_PAPER_MODE)
        rows = [ResearchObservation(-1, item) for item in history]
        ctx = context_at(observation, rows, self.config.interval_seconds,
                         self.config.max_quote_age_seconds,
                         session_prices=price_history)
        ctx.update(snapshot_id=observation.snapshot_id,
                   future_instrument_id=raw.future_instrument_id,
                   future_expiry=raw.future_expiry, expiry_date=raw.expiry)
        ctx["acts"] = acts.evidence(ctx, observation.alpha)
        return ctx

    @staticmethod
    def _save(trade: ActsPaperTrade, doc: dict[str, Any], state: str | None = None) -> None:
        if state is not None:
            trade.state = state
        trade.document = canonical(doc)

    @staticmethod
    def _trades(session: Session) -> list[ActsPaperTrade]:
        return list(session.scalars(select(ActsPaperTrade).order_by(
            ActsPaperTrade.decision_snapshot_id)).all())

    def _mirror_signal(self, doc: dict, lab: LabTrade, raw: ScalperMarketSnapshot,
                       ctx: dict[str, Any], *, available: bool) -> str | None:
        assert self.v2_config is not None
        if datetime.fromisoformat(ctx["timestamp"]) != raw.captured_at:
            raise ValueError("ACTS_MIRROR_OBSERVATION_MISMATCH")
        watch = doc.setdefault("mirror_exit_state", {}).setdefault("watch", {})
        if watch.get("strategy_version") != acts_v2.STRATEGY_ID:
            watch.clear()
        _, direction, _ = acts_v2.market_state(ctx, watch, self.v2_config)
        candidate, rejected = None, {}
        if direction is not None and direction != doc["direction"]:
            candidate, rejected = _candidate(
                raw, {"strategy_id": acts_v2.STRATEGY_ID, "direction": direction}, ctx, self.config)
        return mirror_exit.acts_signal(doc, lab, ctx, self.v2_config, candidate=candidate,
                                       candidate_rejections=rejected, available=available)

    def _transition(self, session: Session, cursor: ActsPaperCursor,
                    snapshot_id: int, raw: ScalperMarketSnapshot,
                    ctx: dict[str, Any], trade: ActsPaperTrade,
                    blocked: bool) -> None:
        if self._terminalize_expired(session, cursor, snapshot_id, raw, trade):
            return
        doc = json.loads(trade.document)
        if trade.state == "UNRESOLVED_EXPOSURE":
            return
        expected_hash = (self.v1_policy_hash if doc.get("strategy_version") == acts.STRATEGY_ID
                         else self.policy_hash if doc.get("strategy_version") ==
                         self.strategy_id else None)
        if (doc["policy_hash"] != expected_hash or
                (doc.get("strategy_version") == acts_v2.STRATEGY_ID
                 and doc.get("exit_policy_version") != mirror_exit.VERSION)):
            doc["unresolved_reason"] = "ACTS_PAPER_POLICY_CHANGED"
            self._save(trade, doc, "UNRESOLVED_EXPOSURE")
            self._emit(session, cursor, "ACTS_UNRESOLVED", snapshot_id, raw,
                       trade, reason=doc["unresolved_reason"])
            return
        candidate = ScalperCandidate.model_validate(doc["candidate"])
        if trade.state == "PENDING_PAPER_ENTRY":
            age = (raw.captured_at - datetime.fromisoformat(
                doc["decision_timestamp"])).total_seconds()
            if age <= 0:
                return
            pair, failure = executable_pair(raw, candidate, self.config, entry=True)
            expiry_reason = self._entry_expiry_reason(raw, candidate, doc["strategy_version"])
            reason = ("ENTRY_TTL_EXCEEDED" if age > self.config.entry_ttl_seconds else
                      expiry_reason if expiry_reason is not None else
                      "KILL_SWITCH_ACTIVE" if self.config.kill_switch else
                      "MARKET_EVENT_BLOCKED" if blocked else
                      failure or (acts_v2.entry_valid(
                          ctx, doc["decision"]["direction"], self.v2_config)
                          if doc.get("strategy_version") == acts_v2.STRATEGY_ID
                          and self.v2_config is not None else
                          acts.entry_valid(ctx, doc["decision"]["direction"])))
            if reason is None:
                assert pair is not None
                loss = (candidate.spread_width - pair.value) * raw.lot_size
                if pair.value < self.config.min_credit or loss > self.config.max_loss_per_trade:
                    reason = "FILL_ECONOMICS_OUTSIDE_RISK"
            if reason is not None:
                doc["rejection_reason"] = reason
                self._save(trade, doc, "ENTRY_REJECTED")
                self._emit(session, cursor, "ACTS_ENTRY_REJECTED", snapshot_id, raw,
                           trade, reason=reason)
                return
            assert pair is not None
            doc.update(entry_timestamp=raw.captured_at.isoformat(),
                       entry_snapshot_id=snapshot_id, entry_spot=raw.nifty_spot,
                       entry_credit=pair.value, entry_quotes=pair.evidence,
                       defined_max_risk=(candidate.spread_width - pair.value) * raw.lot_size,
                       current_debit=pair.value, last_snapshot_id=snapshot_id,
                       best_gross_points=0.0, mae=0.0, mfe=0.0)
            self._save(trade, doc, "OPEN")
            self._emit(session, cursor, "ACTS_ENTRY", snapshot_id, raw, trade,
                       fill_method="NEXT_OBSERVATION_BID_ASK", quotes=pair.evidence)
            return
        if trade.state != "OPEN":
            return
        pair, failure = executable_pair(
            raw, candidate, self.config, entry=False,
            exit_book_policy=exit_book.VERSION if doc.get("strategy_version") == acts_v2.STRATEGY_ID else None)
        if failure is not None:
            terminal = (failure == "MISSING_EXACT_CONTRACT" or
                        raw.captured_at.astimezone(IST).time().replace(tzinfo=None) >= self.config.forced_exit_time)
            if doc.get("exit_policy_version") == mirror_exit.VERSION and not terminal:
                lab = _lab_trade(trade)
                assert lab is not None and self.v2_config is not None
                self._mirror_signal(doc, lab, raw, ctx, available=False)
                doc.update(
                    acts_adverse_since={key: value.isoformat() for key, value in lab.acts_adverse_since.items()},
                    acts_last_adverse_at={key: value.isoformat() for key, value in lab.acts_last_adverse_at.items()})
                self._save(trade, doc)
            if terminal:
                doc.update(unresolved_reason=failure, last_snapshot_id=snapshot_id)
                self._save(trade, doc, "UNRESOLVED_EXPOSURE")
                self._emit(session, cursor, "ACTS_UNRESOLVED", snapshot_id, raw,
                           trade, reason=failure)
            else:
                self._emit(session, cursor, "ACTS_MARK", snapshot_id, raw, trade,
                           available=False, reason=failure)
            return
        assert pair is not None
        lab = _lab_trade(trade)
        assert lab is not None and lab.entry_credit is not None and lab.entry_timestamp is not None
        pnl = lab.entry_credit - pair.value
        lab.mae, lab.mfe = min(lab.mae, pnl), max(lab.mfe, pnl)
        lab.best_gross_points = max(lab.best_gross_points, pnl)
        doc.update(current_debit=pair.value, gross_mark_points=pnl,
                   gross_mark_rupees=pnl * raw.lot_size,
                   mark_quality_warning=exit_book.warning(pair.evidence),
                   best_gross_points=lab.best_gross_points,
                   mae=lab.mae, mfe=lab.mfe,
                   last_snapshot_id=snapshot_id)
        if doc.get("exit_policy_version") == mirror_exit.VERSION:
            assert self.v2_config is not None
            mirror_exit.legacy_diagnostics(doc, raw.captured_at, pair.value, self.config, acts_strategy=True)
            reason = mirror_exit.safety_reason(doc, raw.captured_at, pair.value, self.config,
                                               event_blocked=blocked)
            if reason is None:
                reason = self._mirror_signal(doc, lab, raw, ctx, available=True)
        else:
            reason = _exit_reason(lab, ctx, pair.value, self.config, event_blocked=blocked)
        doc.update(
                   acts_adverse_since={key: value.isoformat() for key, value
                                       in lab.acts_adverse_since.items()},
                   acts_last_adverse_at={key: value.isoformat() for key, value
                                         in lab.acts_last_adverse_at.items()})
        self._save(trade, doc)
        self._emit(session, cursor, "ACTS_MARK", snapshot_id, raw, trade,
                   available=True, quotes=pair.evidence, gross_points=pnl,
                   mark_quality_warning=doc["mark_quality_warning"])
        if reason is None:
            return
        outcome = self.costs.calculate(
            day=raw.captured_at.astimezone(IST).date(), lot_size=raw.lot_size,
            lots=1, entry_short=doc["entry_quotes"]["short"]["fill_price"],
            entry_long=doc["entry_quotes"]["long"]["fill_price"],
            exit_short=pair.short_price, exit_long=pair.long_price)
        doc.update(exit_timestamp=raw.captured_at.isoformat(),
                   exit_snapshot_id=snapshot_id, exit_debit=pair.value,
                   exit_quotes=pair.evidence, exit_reason=reason,
                   holding_seconds=(raw.captured_at - lab.entry_timestamp).total_seconds(),
                   exit_thesis={"reason": reason, "regime": ctx["acts"]["regime"],
                                "alpha": ctx["acts"]["alpha"],
                                "positioning": ctx["acts"]["positioning"],
                                "observation_timestamp": ctx["timestamp"]},
                   final_outcome=outcome, gross_pnl=outcome["gross_rupees"])
        self._save(trade, doc, "CLOSED")
        self._emit(session, cursor, "ACTS_EXIT", snapshot_id, raw, trade,
                   reason=reason, quotes=pair.evidence,
                   accounting=outcome)

    def process_snapshot(self, snapshot_id: int) -> str:
        raw = self.repository.get_snapshot(snapshot_id)
        if raw is None:
            raise LookupError("ACTS_SNAPSHOT_NOT_FOUND")
        observation = self._alpha(self._observation(snapshot_id, raw))
        ctx = self._context(observation)
        alpha_info = alpha_readiness(observation, self.sessions, self.alpha_config)
        regime_info = regime_readiness(ctx)
        with self.sessions.begin() as session:
            changed = session.execute(update(ActsPaperCursor).where(
                ActsPaperCursor.id == 1).values(
                    lock_version=ActsPaperCursor.lock_version + 1))
            if not isinstance(changed, CursorResult) or changed.rowcount != 1:
                raise RuntimeError("ACTS_PAPER_MIGRATION_REQUIRED")
            cursor = session.get(ActsPaperCursor, 1)
            assert cursor is not None
            if (cursor.snapshot_id == snapshot_id or
                    session.scalar(select(ActsPaperEvent.sequence).where(
                        ActsPaperEvent.event_key ==
                        f"{snapshot_id}:SESSION:ACTS_OBSERVATION")) is not None):
                return "DUPLICATE"
            if cursor.observed_at is not None and raw.captured_at <= datetime.fromisoformat(
                    cursor.observed_at):
                raise ValueError("ACTS_OUT_OF_ORDER_OBSERVATION")
            blocked = any(event.block_entries for event in
                          self.events.events_at(raw.captured_at))
            active = session.scalars(select(ActsPaperTrade).where(
                ActsPaperTrade.state.in_(ACTIVE)).order_by(
                    ActsPaperTrade.decision_snapshot_id)).all()
            for trade in active:
                self._terminalize_expired(session, cursor, snapshot_id, raw, trade)
            self._assert_entry_policy_upgrade_safe(
                (trade for trade in active if trade.state in ACTIVE), raw.captured_at)
            for trade in active:
                if trade.state not in ACTIVE:
                    continue
                self._transition(session, cursor, snapshot_id, raw, ctx, trade, blocked)
            mirror_closed = any(
                trade.state == "CLOSED" and json.loads(trade.document).get("exit_reason") == mirror_exit.ACTS_REASON
                and json.loads(trade.document).get("exit_snapshot_id") == snapshot_id for trade in active)
            watch = json.loads(cursor.watch_json)
            candidate = None
            rejected: dict[str, int] = {}
            if self.v2_config is not None:
                if watch.get("strategy_version") != acts_v2.STRATEGY_ID:
                    watch.clear()
                _, direction, _ = acts_v2.market_state(ctx, watch, self.v2_config)
                if direction is not None:
                    candidate, rejected = _candidate(
                        raw, {"strategy_id": self.strategy_id, "direction": direction},
                        ctx, self.config)
                ledger = Ledger(trades=[lab for item in self._trades(session)
                                        if (lab := _lab_trade(item)) is not None])
                reasons = _risk_blockers(ledger, self.config, raw.captured_at, blocked)
                if mirror_closed:
                    reasons.append("MIRROR_EXIT_OBSERVATION_NO_REENTRY")
                if candidate is not None:
                    expiry_reason = self._entry_expiry_reason(raw, candidate, self.strategy_id)
                    if expiry_reason is not None:
                        reasons.append(expiry_reason)
                if candidate is not None and candidate.defined_max_loss_per_lot > (
                        self.config.max_loss_per_trade):
                    reasons.append("MAX_LOSS_PER_TRADE_EXCEEDED")
                decision = acts_v2.decide(ctx, watch, candidate, rejected,
                                          reasons, self.v2_config)
            else:
                decision = acts.decide(ctx, watch)
            decision["snapshot_id"] = snapshot_id
            status = decision["status"]
            if status == "ENTER":
                if self.v2_config is None:
                    self._emit(session, cursor, "ACTS_READY", snapshot_id, raw,
                               decision=decision)
                    candidate, rejected = _candidate(raw, decision, ctx, self.config)
                    decision["candidate_rejections"] = rejected
                    ledger = Ledger(trades=[lab for item in self._trades(session)
                                            if (lab := _lab_trade(item)) is not None])
                    reasons = _risk_blockers(ledger, self.config, raw.captured_at, blocked)
                    if candidate is None:
                        reasons.extend(["NO_EXECUTABLE_CANDIDATE", *sorted(rejected)])
                    if (candidate is not None
                            and raw.captured_at.date() >= candidate.short_leg.expiry):
                        reasons.append("EXPIRY_DAY_ENTRY_FORBIDDEN")
                    if candidate is not None and candidate.defined_max_loss_per_lot > (
                            self.config.max_loss_per_trade):
                        reasons.append("MAX_LOSS_PER_TRADE_EXCEEDED")
                    decision["blockers"] = list(dict.fromkeys([*decision["blockers"], *reasons]))
                else:
                    reasons = []
                    self._emit(session, cursor, "ACTS_READY", snapshot_id, raw,
                               decision=decision)
                assert candidate is not None or reasons
                attribution = (dict(
                    market_state=decision["market_state"],
                    opportunity_score=decision["opportunity_score"],
                    score_band=decision["score_band"],
                    score_components=decision["score_components"],
                    hard_blockers=decision["hard_blockers"],
                    entry_reason=decision["entry_reason"],
                    candidate_rejections=rejected)
                    if self.v2_config is not None else {})
                doc = json_value({
                    "strategy_version": self.strategy_id,
                    "policy_hash": self.policy_hash,
                    **({"policy": self.policy, "exit_policy_version": mirror_exit.VERSION,
                        "future_instrument_id": raw.future_instrument_id,
                        "future_expiry": raw.future_expiry} if self.v2_config is not None else {}),
                    "decision_timestamp": raw.captured_at,
                    "decision_snapshot_id": snapshot_id,
                    "direction": decision["direction"],
                    "alpha_classification": decision["alpha"]["classification"],
                    "regime_evidence": ctx["acts"]["regime"],
                    "positioning_evidence": decision["positioning"],
                    "pullback_evidence": decision["thesis"],
                    "resumption_evidence": {"trigger": decision["trigger"],
                                            "timestamp": raw.captured_at},
                    "decision": decision,
                    **attribution,
                    "candidate": candidate,
                    "short_contract": candidate.short_leg if candidate else None,
                    "hedge_contract": candidate.long_leg if candidate else None,
                    "option_expiry": raw.expiry,
                    "lot_size": raw.lot_size, "lots": 1,
                    "decision_entry_quotes": candidate.quote_evidence if candidate else None,
                    "decision_executable_credit": (
                        candidate.executable_credit if candidate else None),
                    "decision_defined_max_risk": (
                        candidate.defined_max_loss_per_lot if candidate else None),
                    "structural_reference": (decision["thesis"].get("wall") or {}).get(
                        "strike") if decision["thesis"] else None})
                trade = ActsPaperTrade(
                    id=digest([self.strategy_id, snapshot_id,
                               candidate.candidate_id if candidate else "REJECTED"]),
                    decision_snapshot_id=snapshot_id,
                    state="ENTRY_REJECTED" if reasons else "PENDING_PAPER_ENTRY",
                    document=canonical(doc))
                session.add(trade)
                session.flush()
                if reasons:
                    self._emit(session, cursor, "ACTS_ENTRY_REJECTED", snapshot_id,
                               raw, trade, reason=reasons[0])
                    status = "ENTRY_REJECTED"
                else:
                    assert candidate is not None
                    self._emit(session, cursor, "ACTS_PENDING_PAPER_ENTRY", snapshot_id,
                               raw, trade, candidate_id=candidate.candidate_id)
                    status = "PENDING_PAPER_ENTRY"
            else:
                details: dict[str, Any] = (
                    {"decision": decision} if self.v2_config is not None else {})
                self._emit(session, cursor, "ACTS_WATCH" if status == "WATCH" else
                           "ACTS_NO_SETUP", snapshot_id, raw,
                           reason=decision["blockers"][0] if decision["blockers"] else None,
                           **details)
            latest = {"snapshot_id": snapshot_id, "timestamp": raw.captured_at,
                      "status": status, "decision_regime": decision["regime"],
                      "alpha_state": decision["alpha"]["classification"],
                      "alpha_diagnostics": alpha_info,
                      "regime_diagnostics": regime_info,
                      "direction": decision["direction"],
                      "primary_blocker": decision["blockers"][0]
                      if decision["blockers"] else None}
            if self.v2_config is not None:
                latest.update(
                    strategy_version=self.strategy_id,
                    market_state=decision["market_state"],
                    opportunity_score=decision["opportunity_score"],
                    score_band=decision["score_band"],
                    score_components=decision["score_components"],
                    hard_blockers=decision["hard_blockers"],
                    candidate_rejections=decision["candidate_rejections"],
                    entry_reason=decision["entry_reason"])
            cursor.watch_json = canonical(watch)
            cursor.latest_json = canonical(latest)
            cursor.snapshot_id = snapshot_id
            cursor.observed_at = raw.captured_at.isoformat()
            details = {"decision": decision} if self.v2_config is not None else {}
            self._emit(session, cursor, "ACTS_OBSERVATION", snapshot_id, raw,
                       watch=watch, latest=latest, **details)
            return status

    def required_contracts(self, at: datetime | None = None) -> list[OptionContractSnapshot]:
        """Ask market-data capture for held exact legs, including pending hedges."""
        with self.sessions() as session:
            rows = session.scalars(select(ActsPaperTrade).where(
                ActsPaperTrade.state.in_(("PENDING_PAPER_ENTRY", "OPEN")))).all()
            result: dict[tuple[str, str], OptionContractSnapshot] = {}
            for row in rows:
                doc = json.loads(row.document)
                if doc.get("strategy_version") == acts_v2.STRATEGY_ID and expiry.expired(doc, at or datetime.now(IST)):
                    continue
                candidate = doc["candidate"]
                for name in ("short_leg", "long_leg"):
                    leg = candidate[name]
                    result[(leg["exchange"], leg["instrument_token"])] = (
                        OptionContractSnapshot.model_validate(leg))
            return list(result.values())

    def status(self, at: datetime) -> dict[str, Any]:
        day = at.astimezone(IST).date()
        with self.sessions() as session:
            cursor = session.get(ActsPaperCursor, 1)
            if cursor is None:
                raise RuntimeError("ACTS_PAPER_MIGRATION_REQUIRED")
            rows = self._trades(session)
            docs = [(row.state, json.loads(row.document)) for row in rows]
            today = [(row.id, state, doc) for row, (state, doc) in zip(rows, docs)
                     if doc["decision_timestamp"][:10] == str(day)]
            closed = [doc for _, state, doc in today if state == "CLOSED" and not expiry.is_unpriced(doc)]
            active = next(((state, doc) for state, doc in reversed(docs)
                           if state in ACTIVE), None)
            realized = sum(doc["final_outcome"]["gross_rupees"] for doc in closed)
            unrealized = (active[1].get("gross_mark_rupees") if active is not None
                          and active[0] == "OPEN" else None)
            entries = sum(doc.get("entry_timestamp") is not None
                          for _, _, doc in today)
            latest = json.loads(cursor.latest_json)
            history_at = ((latest.get("alpha_diagnostics") or {}).get("alpha_timestamp")
                          or latest.get("timestamp"))
            history_report: dict[str, Any] = {}
            if history_at:
                history_repo = ActsAlphaRepository(self.sessions, self.alpha_config)
                history_repo.load_history_before(datetime.fromisoformat(history_at))
                history_report = history_repo.last_history_report
            return json_value({
                "enabled": self.settings.acts_paper_enabled,
                "mode": "PAPER", "strategy_version": self.strategy_id,
                "latest_observation": latest,
                "alpha_history": history_report,
                "watch_state": json.loads(cursor.watch_json),
                "current_state": active[0] if active else
                json.loads(cursor.latest_json).get("status", "NO_SETUP"),
                "open_spread": active[1].get("candidate") if active else None,
                "today_acts_trades": [{
                    "trade_id": trade_id, "state": state,
                    "strategy_version": doc.get("strategy_version", acts.STRATEGY_ID),
                    "direction": doc["direction"],
                    "market_state": doc.get("market_state"),
                    "opportunity_score": doc.get("opportunity_score"),
                    "score_band": doc.get("score_band"),
                    "entry_reason": doc.get("entry_reason"),
                    "exit_reason": doc.get("exit_reason"),
                    "decision_timestamp": doc["decision_timestamp"],
                    "entry_timestamp": doc.get("entry_timestamp"),
                    "exit_timestamp": doc.get("exit_timestamp"),
                    "gross_pnl": doc.get("gross_pnl")}
                    for trade_id, state, doc in today],
                "open_position_count": sum(state == "OPEN" for state, _ in docs),
                "unresolved_exposure_count": sum(state == "UNRESOLVED_EXPOSURE" for state, _ in docs),
                "expired_unpriced_count": sum(state == "CLOSED" and expiry.is_unpriced(doc)
                                              for state, doc in docs),
                "realized_gross_pnl": realized,
                "unrealized_gross_pnl": unrealized,
                "total_gross_pnl": realized + unrealized if unrealized is not None
                else realized if active is None else None,
                "entries_remaining_today": max(0, self.config.max_trades_per_day - entries),
                "journal_head": cursor.head_hash})
