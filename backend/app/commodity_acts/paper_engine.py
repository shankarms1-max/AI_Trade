"""Automated paper lifecycle over V1 evidence. No broker object is accepted."""
from datetime import datetime, time
from decimal import Decimal
import json

from sqlalchemy import select, update

from app.acts_paper import acts_v2
from app.commodity_acts.context import build_context
from app.commodity_acts.domain import CommoditySnapshot
from app.commodity_acts.models import CommodityActsDecision, CommodityMarketSnapshotRecord
from app.commodity_acts.paper_execution import candidates, pair
from app.commodity_acts.paper_journal import emit, verify
from app.commodity_acts.paper_models import (CommodityPaperControl, CommodityPaperCursor, CommodityPaperFill,
                                           CommodityPaperOrder, CommodityPaperPosition)
from app.commodity_acts.paper_policy import ACTIVE, PAPER_VERSION, PaperPolicy, margin_for
from app.research.manifest import canonical, digest, json_value
from app.scalper_lab.acts import alpha_evidence
from app.alpha.models import AlphaFeatureSnapshot


def pnl(credit, debit, multiplier):
    return float((Decimal(str(credit)) - Decimal(str(debit))) * Decimal(str(multiplier)))


class CommodityPaperEngine:
    def __init__(self, research):
        self.research, self.sessions, self.config = research, research.sessions, research.config
        self.spec, self.instrument, self.clock = research.spec, research.spec.instrument_id, research.clock
        self.scorer = research.acts_config
        self.policy = self.config.trading_policy(self.instrument)
        self.enabled = self.config.trading_enabled and self.config.enabled(self.instrument)
        if self.enabled:
            start = datetime.combine(datetime.today(), self.spec.market_session.forced_exit)
            end = datetime.combine(datetime.today(), self.spec.market_session.end)
            if (end - start).total_seconds() < 30:
                raise ValueError("COMMODITY_EXIT_OBSERVATION_BUFFER_REQUIRED")
        verify(self.sessions, self.instrument)

    def _emit(self, session, cursor, kind, current, sid, known, changes=(), **details):
        details["global_sequence"] = session.get(CommodityPaperControl, 1).lock_version
        emit(session, cursor, kind, sid, current, known, changes=changes, **details)

    @staticmethod
    def _save(row, doc, state=None):
        if state is not None:
            row.state = state
        doc["state"] = row.state
        row.document = canonical(doc)

    def _risk(self, rows, current, known, proposed, policy, *, excluding=None):
        active = [row for row in rows if row.state in ACTIVE and row.id != excluding]
        own = [row for row in rows if row.instrument_id == self.instrument and row.id != excluding]
        reasons = []
        if self.config.global_max_defined_risk is None or self.config.global_daily_loss_limit is None:
            reasons.append("GLOBAL_RISK_UNCONFIGURED")
        if len(active) >= self.config.global_max_positions or sum(r.instrument_id == self.instrument for r in active) >= policy.max_positions:
            reasons.append("POSITION_LIMIT")
        docs = [(row, json.loads(row.document)) for row in rows if row.id != excluding]
        day = current.timestamp_ist.date().isoformat()
        closed = [(row, doc) for row, doc in docs if row.state == "CLOSED" and doc["exit_timestamp"][:10] == day]
        realized = sum(min(doc["gross_realized_pnl"], doc.get("net_realized_pnl") if doc.get("net_realized_pnl") is not None
                           else doc["gross_realized_pnl"]) for _, doc in closed)
        own_realized = sum(min(doc["gross_realized_pnl"], doc.get("net_realized_pnl") if doc.get("net_realized_pnl") is not None
                               else doc["gross_realized_pnl"]) for row, doc in closed if row.instrument_id == self.instrument)
        reserved = sum(json.loads(row.document)["defined_risk"] for row in active)
        def loss_reservation(doc):
            loss = doc["defined_risk"]
            net = PaperPolicy.model_validate(doc["policy"]).net(-loss, current.timestamp_ist.date(),
                datetime.fromisoformat(doc.get("entry_timestamp", doc["decision_timestamp"])).date())
            return max(loss, -net) if net is not None else loss
        daily_reserved = sum(loss_reservation(json.loads(row.document)) for row in active)
        own_reserved = sum(loss_reservation(json.loads(row.document)) for row in active if row.instrument_id == self.instrument)
        proposed_net = policy.net(-proposed, current.timestamp_ist.date())
        daily_proposed = max(proposed, -proposed_net) if proposed_net is not None else proposed
        if (self.config.global_max_defined_risk is not None and reserved + proposed > self.config.global_max_defined_risk):
            reasons.append("GLOBAL_DEFINED_RISK_LIMIT")
        if own_realized - own_reserved - daily_proposed < -policy.daily_loss_limit:
            reasons.append("INSTRUMENT_DAILY_LOSS_RESERVATION_LIMIT")
        if (self.config.global_daily_loss_limit is not None and realized - daily_reserved - daily_proposed < -self.config.global_daily_loss_limit):
            reasons.append("GLOBAL_DAILY_LOSS_RESERVATION_LIMIT")
        entered = sum(bool(json.loads(row.document).get("entry_timestamp"))
                      and json.loads(row.document)["session_id"] == current.session_id for row in own)
        if entered >= policy.max_trades_per_session:
            reasons.append("SESSION_TRADE_LIMIT")
        for row, doc in docs:
            if row.state in ("OPEN", "PENDING_EXIT", "UNRESOLVED_EXPOSURE"):
                mark_at = doc.get("mark_at")
                if mark_at is None or not 0 <= (known - datetime.fromisoformat(mark_at)).total_seconds() <= doc["policy"]["max_quote_age_seconds"]:
                    reasons.append("UNVALUED_OR_STALE_EXISTING_EXPOSURE")
        return sorted(set(reasons))

    def _daily_exit(self, rows, row, current_doc, at, known, policy, control):
        own, total = 0.0, 0.0
        for other in rows:
            doc = current_doc if other.id == row.id else json.loads(other.document)
            value = None
            if other.state == "CLOSED" and doc["exit_timestamp"][:10] == at.date().isoformat():
                value = min(doc["gross_realized_pnl"], doc["net_realized_pnl"] if doc.get("net_realized_pnl") is not None else doc["gross_realized_pnl"])
            elif other.state in ("OPEN", "PENDING_EXIT", "UNRESOLVED_EXPOSURE") and doc.get("mark_at") and (
                    0 <= (known-datetime.fromisoformat(doc["mark_at"])).total_seconds() <= doc["policy"]["max_quote_age_seconds"]):
                value = doc.get("unrealized_gross")
                if value is not None and doc.get("unrealized_net") is not None:
                    value = min(value, doc["unrealized_net"])
            if value is not None:
                total += value
                if other.instrument_id == self.instrument: own += value
        global_limit = json.loads(control.policy_json).get("daily_loss")
        return ("INSTRUMENT_DAILY_LOSS" if own <= -policy.daily_loss_limit else
                "GLOBAL_DAILY_LOSS" if global_limit is not None and total <= -global_limit else None)

    def _order(self, session, row, doc, kind, current, sid, known, reason):
        identity = digest({"position_id": row.id, "kind": kind})
        order = session.get(CommodityPaperOrder, identity)
        if order is None:
            order_doc = {"id": identity, "position_id": row.id, "instrument_id": self.instrument,
                         "kind": kind, "state": "PENDING", "signal_at": current.timestamp_ist.isoformat(),
                         "created_at": self.clock().isoformat(),
                         "signal_snapshot_id": sid, "reason": reason}
            order = CommodityPaperOrder(id=identity, instrument_id=self.instrument, position_id=row.id,
                                        state="PENDING", observed_at=current.timestamp_ist.isoformat(), document=canonical(order_doc))
            session.add(order)
        return order

    def _fill(self, session, order, current, sid, known, execution):
        fill_doc = {"id": digest({"order_id": order.id}), "order_id": order.id,
            "position_id": order.position_id, "instrument_id": self.instrument, "kind": json.loads(order.document)["kind"],
            "snapshot_id": sid, "observed_at": current.timestamp_ist.isoformat(), "processed_at": known.isoformat(),
            "execution": execution, "lots": 1, "atomic": True, "broker_orders_enabled": False}
        fill = CommodityPaperFill(id=fill_doc["id"], instrument_id=self.instrument, order_id=order.id,
                                  observed_at=current.timestamp_ist.isoformat(), document=canonical(fill_doc))
        session.add(fill)
        order_doc = json.loads(order.document)
        order_doc.update(fill_id=fill.id, filled_at=current.timestamp_ist.isoformat())
        self._save(order, order_doc, "FILLED")
        return fill

    def _transition(self, session, cursor, row, current, sid, known, ctx, all_rows):
        doc = json.loads(row.document)
        policy = PaperPolicy.model_validate(doc["policy"])
        if doc["policy_hash"] != digest(policy):
            raise RuntimeError("COMMODITY_PAPER_POSITION_POLICY_INVALID")
        candidate = doc["candidate"]
        scorer = acts_v2.ActsV2Config(**doc["acts_policy"])
        at = current.timestamp_ist
        forced = time.fromisoformat(doc["forced_exit_time"])
        forced_due = at.date().isoformat() != doc["session_id"][-10:] or at.time().replace(tzinfo=None) >= forced
        if row.state == "PENDING_PAPER_ENTRY":
            order = session.get(CommodityPaperOrder, doc["entry_order_id"])
            signal_at = datetime.fromisoformat(doc["decision_timestamp"])
            created_at = datetime.fromisoformat(json.loads(order.document)["created_at"])
            if at <= max(signal_at, created_at):
                return
            reason = ("TRADING_DISABLED" if not self.enabled else "KILL_SWITCH" if policy.kill_switch or self.policy and self.policy.kill_switch else
                      "GLOBAL_RISK_POLICY_CHANGED" if doc["global_policy"] != {
                          "positions": self.config.global_max_positions, "risk": self.config.global_max_defined_risk,
                          "daily_loss": self.config.global_daily_loss_limit} else
                      "ENTRY_TTL_EXCEEDED" if (known - signal_at).total_seconds() > policy.entry_ttl_seconds else
                      "SESSION_ENTRY_CLOSED" if forced_due else
                      "EXPIRY_ENTRY_FORBIDDEN" if (current.expiry - at.date()).days < policy.min_entry_dte else
                      policy.metadata_reason(current, known) or acts_v2.entry_valid(ctx, doc["direction"], scorer))
            execution, failure = pair(current, candidate, policy, known, entry=True, after=max(signal_at, created_at)) if reason is None else (None, None)
            if execution is not None:
                risk = (candidate["spread_width"] - execution["value"]) * policy.evidence.rupees_per_point_per_lot
                reason = ("FILL_ECONOMICS_OUTSIDE_RISK" if risk > policy.max_loss_per_trade or execution["value"] <= policy.min_credit else
                          next(iter(self._risk(all_rows, current, known, risk, policy, excluding=row.id)), None))
                if (execution["short"]["quote"]["ask"]-execution["long"]["quote"]["bid"] >=
                        execution["value"]*policy.stop_credit_multiple):
                    reason = "FILL_ALREADY_AT_STOP"
                margin = margin_for(policy, candidate["short_leg"]["instrument_token"], candidate["long_leg"]["instrument_token"], current.expiry, known)
                if policy.max_broker_margin is not None and (margin is None or margin["required_margin"] > policy.max_broker_margin):
                    reason = "FILL_MARGIN_UNVERIFIED_OR_LIMIT_EXCEEDED"
            if reason is not None:
                doc["rejection_reason"] = reason
                self._save(row, doc, "ENTRY_REJECTED")
                od = json.loads(order.document)
                od["rejection_reason"] = reason
                self._save(order, od, "REJECTED")
                self._emit(session, cursor, "ENTRY_REJECTED", current, sid, known,
                           (("position", row), ("order", order)), reason=reason)
                return
            if failure:
                od = json.loads(order.document)
                od["last_attempt"] = {"snapshot_id": sid, "reason": failure, "observed_at": at.isoformat()}
                self._save(order, od)
                self._emit(session, cursor, "ENTRY_WAIT", current, sid, known, (("order", order),), reason=failure)
                return
            fill = self._fill(session, order, current, sid, known, execution)
            margin = margin_for(policy, candidate["short_leg"]["instrument_token"], candidate["long_leg"]["instrument_token"], current.expiry, known)
            doc.update(entry_timestamp=at.isoformat(), entry_snapshot_id=sid, entry_credit=execution["value"],
                       entry_execution=execution, defined_risk=risk, margin_evidence=margin,
                       margin_status="VERIFIED_PORTFOLIO_ESTIMATE" if margin else "MARGIN_UNVERIFIED")
            self._save(row, doc, "OPEN")
            self._emit(session, cursor, "ENTRY_FILLED", current, sid, known,
                       (("position", row), ("order", order), ("fill", fill)))
        execution, failure = pair(current, candidate, policy, known, entry=False)
        reason = ("FORCED_SESSION_EXIT" if forced_due else "EXPIRY_SAFEGUARD" if at.date() >= policy.evidence.option_expiry else
                  "KILL_SWITCH" if policy.kill_switch or self.policy and self.policy.kill_switch else
                  "TIME_STOP" if (at - datetime.fromisoformat(doc["entry_timestamp"])).total_seconds() >= policy.max_holding_minutes * 60 else None)
        if execution is not None:
            debit = execution["value"]
            gross_points = doc["entry_credit"] - debit
            doc.update(current_debit=debit, unrealized_gross=pnl(doc["entry_credit"], debit, policy.evidence.rupees_per_point_per_lot),
                       mark_at=at.isoformat(), mark_execution=execution,
                       best_gross_points=max(doc.get("best_gross_points", gross_points), gross_points), unresolved_reason=None)
            doc["unrealized_net"] = policy.net(doc["unrealized_gross"], at.date(), datetime.fromisoformat(doc["entry_timestamp"]).date())
            credit, best = doc["entry_credit"], doc["best_gross_points"]
            if reason is None:
                reason = (self._daily_exit(all_rows, row, doc, at, known, policy, session.get(CommodityPaperControl, 1)) or
                          ("HARD_MAX_LOSS" if doc["unrealized_gross"] <= -policy.max_loss_per_trade else
                          "SPREAD_STOP" if debit >= credit * policy.stop_credit_multiple else
                          "TRAILING_EXIT" if best >= credit * policy.trailing_activation_pct / 100 and
                          gross_points <= best - credit * policy.trailing_giveback_pct / 100 else
                          "PROFIT_CAPTURE" if debit <= credit * (1-policy.profit_capture_pct/100) else None))
            if reason is None:
                mirror = doc.setdefault("mirror_watch", {})
                _, direction, _ = acts_v2.market_state(ctx, mirror, scorer)
                opposite, rejects = (candidates(current, direction, ctx, policy, known)
                                     if direction is not None and direction != doc["direction"] else (None, {}))
                signal = acts_v2.decide(ctx, mirror, opposite, rejects, [], scorer)
                doc["mirror_signal"] = signal
                if signal["status"] == "ENTER" and signal["direction"] != doc["direction"]:
                    reason = "OPPOSITE_ACTS_V2_ENTRY_SIGNAL"
        else:
            doc.update(current_debit=None, unrealized_gross=None, unrealized_net=None, mark_at=None,
                       unresolved_reason=failure)
        if reason and not doc.get("exit_order_id"):
            order = self._order(session, row, doc, "EXIT", current, sid, known, reason)
            doc.update(exit_order_id=order.id, exit_reason=reason)
            self._save(row, doc, "PENDING_EXIT" if execution else "UNRESOLVED_EXPOSURE")
            self._emit(session, cursor, "EXIT_REQUESTED", current, sid, known,
                       (("position", row), ("order", order)), reason=reason)
        if doc.get("exit_order_id"):
            order = session.get(CommodityPaperOrder, doc["exit_order_id"])
            od = json.loads(order.document)
            signal_at = max(datetime.fromisoformat(od["signal_at"]), datetime.fromisoformat(od["created_at"]))
            if at <= signal_at:
                return
            closing, failure = pair(current, candidate, policy, known, entry=False, after=signal_at)
            if closing is not None:
                fill = self._fill(session, order, current, sid, known, closing)
                gross = pnl(doc["entry_credit"], closing["value"], policy.evidence.rupees_per_point_per_lot)
                margin = doc.get("margin_evidence")
                doc.update(exit_timestamp=at.isoformat(), exit_snapshot_id=sid, exit_debit=closing["value"],
                           exit_execution=closing, gross_realized_pnl=gross,
                           net_realized_pnl=policy.net(gross, at.date(), datetime.fromisoformat(doc["entry_timestamp"]).date()), unrealized_gross=None, unrealized_net=None,
                           return_on_verified_margin=gross / margin["required_margin"] if margin and margin.get("measurement_kind") == "ACTUAL_BLOCKED" else None,
                           gross_pnl_over_verified_margin_estimate=gross / margin["required_margin"] if margin else None,
                           unresolved_reason=None)
                self._save(row, doc, "CLOSED")
                self._emit(session, cursor, "EXIT_FILLED", current, sid, known,
                           (("position", row), ("order", order), ("fill", fill)))
                return
            doc["unresolved_reason"] = failure
        self._save(row, doc, "UNRESOLVED_EXPOSURE" if execution is None or doc.get("exit_order_id") and failure else
                   "PENDING_EXIT" if doc.get("exit_order_id") else "OPEN")
        self._emit(session, cursor, "POSITION_OBSERVED", current, sid, known, (("position", row),), reason=doc.get("unresolved_reason"))

    def process_snapshot(self, sid):
        with self.sessions() as session:
            raw = session.get(CommodityMarketSnapshotRecord, sid)
            research = session.scalar(select(CommodityActsDecision).where(CommodityActsDecision.snapshot_id == sid,
                CommodityActsDecision.instrument_id == self.instrument))
            if raw is None or research is None or raw.instrument_id != self.instrument:
                raise ValueError("COMMODITY_PAPER_EVIDENCE_REQUIRED")
            current = CommoditySnapshot.model_validate_json(raw.snapshot_json)
            research_doc = json.loads(research.document)
            alpha = AlphaFeatureSnapshot.model_validate(research_doc["alpha"])
        known = self.clock()
        if current.timestamp_ist > known:
            raise ValueError("COMMODITY_PAPER_FUTURE_OBSERVATION")
        ctx = build_context(current, self.research._history(current), self.spec, known, self.config.max_quote_age_seconds)
        ctx["acts"] = {"alpha": alpha_evidence(alpha, known, current.future_instrument_id, current.future_expiry, current.expiry)}
        with self.sessions.begin() as session:
            # One commodity-only DB mutex serializes global risk reservations
            # across independent instrument processes (also effective on SQLite).
            result = session.execute(update(CommodityPaperControl).where(CommodityPaperControl.id == 1).values(
                lock_version=CommodityPaperControl.lock_version + 1))
            if result.rowcount != 1:
                raise RuntimeError("COMMODITY_PAPER_MIGRATION_REQUIRED")
            control = session.get(CommodityPaperControl, 1)
            cursor = session.get(CommodityPaperCursor, self.instrument)
            if cursor.snapshot_id == sid:
                return "DUPLICATE"
            if cursor.observed_at and current.timestamp_ist <= datetime.fromisoformat(cursor.observed_at):
                raise ValueError("COMMODITY_PAPER_OUT_OF_ORDER")
            # A different instrument may have held the mutex. Never validate a
            # book against the pre-wait clock or backdate its order creation.
            known = self.clock()
            ctx["decided_at"] = known.isoformat()
            ctx["acts"]["alpha"] = alpha_evidence(alpha, known, current.future_instrument_id,
                                                   current.future_expiry, current.expiry)
            state = json.loads(cursor.state_json)
            identity = [current.session_id, current.future_token, current.future_expiry.isoformat(), current.expiry.isoformat()]
            if state.get("identity") != identity:
                state.update(identity=identity, watch={})
            rows = list(session.scalars(select(CommodityPaperPosition).order_by(CommodityPaperPosition.id)))
            for row in rows:
                if row.instrument_id == self.instrument and row.state in ACTIVE:
                    self._transition(session, cursor, row, current, sid, known, ctx, rows)
            session.flush()
            watch = state.setdefault("watch", {})
            _, direction, _ = acts_v2.market_state(ctx, watch, self.scorer)
            candidate, rejected = (candidates(current, direction, ctx, self.policy, known)
                if self.enabled and self.policy is not None and direction is not None else (None, {}))
            blockers = []
            if not self.enabled: blockers.append("PAPER_ACTIVATION_DISABLED")
            if self.policy is None: blockers.append("PAPER_POLICY_UNCONFIGURED")
            if self.policy is not None:
                at, forced = current.timestamp_ist, self.spec.market_session.forced_exit
                seconds_to_forced = (datetime.combine(at.date(), forced, at.tzinfo)-at).total_seconds()
                if seconds_to_forced <= self.policy.entry_ttl_seconds or not self.spec.market_session.contains(at):
                    blockers.append("SESSION_ENTRY_CLOSED")
                if (current.expiry - at.date()).days < self.policy.min_entry_dte: blockers.append("EXPIRY_ENTRY_FORBIDDEN")
                if self.policy.kill_switch: blockers.append("KILL_SWITCH")
                if candidate is not None:
                    blockers.extend(self._risk(rows, current, known, candidate.defined_max_loss_per_lot, self.policy))
                    global_policy = {"positions": self.config.global_max_positions, "risk": self.config.global_max_defined_risk,
                                     "daily_loss": self.config.global_daily_loss_limit}
                    if control.policy_json not in ("{}", canonical(global_policy)):
                        blockers.append("GLOBAL_RISK_POLICY_CHANGED")
            decision = acts_v2.decide(ctx, watch, candidate, rejected, sorted(set(blockers)), self.scorer)
            if decision["status"] == "ENTER":
                control.policy_json = canonical(global_policy)
                pid = digest({"instrument_id": self.instrument, "decision_snapshot_id": sid, "version": PAPER_VERSION})
                doc = json_value({"id": pid, "instrument_id": self.instrument, "state": "PENDING_PAPER_ENTRY",
                    "session_id": current.session_id, "decision_timestamp": current.timestamp_ist, "decision_snapshot_id": sid,
                    "processed_at": known, "candidate": candidate, "direction": decision["direction"], "decision": decision,
                    "policy": self.policy, "policy_hash": digest(self.policy), "acts_policy": self.scorer.payload(), "future_token": current.future_token,
                    "global_policy": global_policy,
                    "forced_exit_time": self.spec.market_session.forced_exit, "defined_risk": candidate.defined_max_loss_per_lot,
                    "margin_status": "MARGIN_UNVERIFIED", "gross_realized_pnl": None, "net_realized_pnl": None,
                    "unrealized_gross": None, "unrealized_net": None, "mark_at": None})
                row = CommodityPaperPosition(id=pid, instrument_id=self.instrument, state="PENDING_PAPER_ENTRY", document=canonical(doc))
                row.observed_at = current.timestamp_ist.isoformat()
                session.add(row)
                session.flush()
                order = self._order(session, row, doc, "ENTRY", current, sid, known, "ACTS_V2_ENTER")
                doc["entry_order_id"] = order.id
                self._save(row, doc)
                self._emit(session, cursor, "PENDING_PAPER_ENTRY", current, sid, known, (("position", row), ("order", order)))
            latest = json_value({"version": PAPER_VERSION, "stage": "AUTOMATED_PAPER", "instrument_id": self.instrument,
                "observed_at": current.timestamp_ist, "processed_at": known, "snapshot_id": sid, "decision": decision,
                "candidate": candidate, "risk_blocks": sorted(set(blockers)), "broker_orders_enabled": False,
                "paper_enabled": self.enabled, "margin_status": candidate.construction_evidence["margin"]["status"] if candidate else "MARGIN_UNVERIFIED"})
            latest.update(future_price=current.future_price, future_symbol=current.future_instrument_id,
                future_expiry=current.future_expiry.isoformat(), option_expiry=current.expiry.isoformat(),
                source_market_timestamp=current.source_market_timestamp.isoformat() if current.source_market_timestamp else None,
                data_quality={key: research_doc["geometry"].get(key) for key in (
                    "contracts", "bid_ask_coverage", "depth_coverage", "source_timestamp_coverage")})
            self._emit(session, cursor, "PAPER_OBSERVATION", current, sid, known,
                       state=state, latest=latest, snapshot_hash=digest(current), scoring_policy=self.scorer.payload(),
                       risk_configuration={"positions": self.config.global_max_positions, "risk": self.config.global_max_defined_risk,
                                           "daily_loss": self.config.global_daily_loss_limit},
                       paper_policy=self.policy, paper_policy_hash=digest(self.policy) if self.policy else None)
        return "PAPER_OBSERVATION"
