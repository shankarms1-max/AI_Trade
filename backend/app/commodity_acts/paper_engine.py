"""Automated paper lifecycle over V1 evidence. No broker object is accepted."""

from datetime import datetime, time
from decimal import Decimal
import json

from sqlalchemy import select, update

from app.acts_paper import acts_v2
from app.commodity_acts.context import build_context
from app.commodity_acts.broker import assert_market_only
from app.commodity_acts.domain import CommoditySnapshot
from app.commodity_acts.models import CommodityActsDecision, CommodityMarketSnapshotRecord
from app.commodity_acts.paper_naked import (
    NakedPaperPolicy,
    VERSION,
    candidate_risk,
    candidates,
    decide,
    execute,
    load_policy,
    reservation,
    stress,
)
from app.commodity_acts.paper_journal import emit, verify
from app.commodity_acts.paper_models import (
    CommodityPaperControl,
    CommodityPaperCursor,
    CommodityPaperFill,
    CommodityPaperOrder,
    CommodityPaperPosition,
)
from app.commodity_acts.paper_policy import ACTIVE, margin_for
from app.research.manifest import canonical, digest, json_value
from app.scalper_lab.acts import alpha_evidence
from app.alpha.models import AlphaFeatureSnapshot


def pnl(credit, debit, multiplier):
    return float((Decimal(str(credit)) - Decimal(str(debit))) * Decimal(str(multiplier)))


class CommodityPaperEngine:
    def __init__(self, research, *, margin_reader=None):
        self.research, self.sessions, self.config = research, research.sessions, research.config
        self.spec, self.instrument, self.clock = research.spec, research.spec.instrument_id, research.clock
        self.scorer = research.acts_config
        self.policy = self.config.trading_policy(self.instrument)
        if margin_reader is not None:
            assert_market_only(margin_reader)
        self.margin_reader = margin_reader
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
        limit = self._risk_limit(policy)
        if limit is None or self.config.global_daily_loss_limit is None:
            reasons.append("GLOBAL_RISK_UNCONFIGURED")
        if (
            len(active) >= self.config.global_max_positions
            or sum(r.instrument_id == self.instrument for r in active) >= policy.max_positions
        ):
            reasons.append("POSITION_LIMIT")
        docs = [(row, json.loads(row.document)) for row in rows if row.id != excluding]
        day = current.timestamp_ist.date().isoformat()
        closed = [(row, doc) for row, doc in docs if row.state == "CLOSED" and doc["exit_timestamp"][:10] == day]
        realized = sum(
            min(
                doc["gross_realized_pnl"],
                doc.get("net_realized_pnl") if doc.get("net_realized_pnl") is not None else doc["gross_realized_pnl"],
            )
            for _, doc in closed
        )
        own_realized = sum(
            min(
                doc["gross_realized_pnl"],
                doc.get("net_realized_pnl") if doc.get("net_realized_pnl") is not None else doc["gross_realized_pnl"],
            )
            for row, doc in closed
            if row.instrument_id == self.instrument
        )
        reserved = sum(
            max(reservation(d := json.loads(row.document)), -(d.get("unrealized_gross") or 0)) for row in active
        )

        def loss_reservation(doc):
            loss = max(reservation(doc), -(doc.get("unrealized_gross") or 0))
            net = load_policy(doc["policy"]).net(
                -loss,
                current.timestamp_ist.date(),
                datetime.fromisoformat(doc.get("entry_timestamp", doc["decision_timestamp"])).date(),
            )
            return max(loss, -net) if net is not None else loss

        daily_reserved = sum(loss_reservation(json.loads(row.document)) for row in active)
        own_reserved = sum(
            loss_reservation(json.loads(row.document)) for row in active if row.instrument_id == self.instrument
        )
        proposed_net = policy.net(-proposed, current.timestamp_ist.date())
        daily_proposed = max(proposed, -proposed_net) if proposed_net is not None else proposed
        if limit is not None and reserved + proposed > limit:
            reasons.append(
                "GLOBAL_STRESS_RISK_LIMIT" if isinstance(policy, NakedPaperPolicy) else "GLOBAL_DEFINED_RISK_LIMIT"
            )
        if own_realized - own_reserved - daily_proposed < -policy.daily_loss_limit:
            reasons.append("INSTRUMENT_DAILY_LOSS_RESERVATION_LIMIT")
        if (
            self.config.global_daily_loss_limit is not None
            and realized - daily_reserved - daily_proposed < -self.config.global_daily_loss_limit
        ):
            reasons.append("GLOBAL_DAILY_LOSS_RESERVATION_LIMIT")
        entered = sum(
            bool(json.loads(row.document).get("entry_timestamp"))
            and json.loads(row.document)["session_id"] == current.session_id
            for row in own
        )
        if entered >= policy.max_trades_per_session:
            reasons.append("SESSION_TRADE_LIMIT")
        for row, doc in docs:
            if row.state in ("OPEN", "PENDING_EXIT", "UNRESOLVED_EXPOSURE"):
                mark_at = doc.get("mark_at")
                # The next-observation mark is deliberately unavailable just
                # after entry. Its independently validated two-sided entry book
                # can support a risk reservation while fresh, but never P&L.
                if (
                    mark_at is None
                    and row.state == "OPEN"
                    and doc.get("strategy") == "NAKED_SHORT_OPTION"
                    and not doc.get("unresolved_reason")
                    and not doc.get("mark_execution")
                ):
                    mark_at = (
                        doc.get("entry_execution", {}).get("short", {}).get("quote", {}).get("source_market_timestamp")
                    )
                if (
                    mark_at is None
                    or not 0
                    <= (known - datetime.fromisoformat(mark_at)).total_seconds()
                    <= doc["policy"]["max_quote_age_seconds"]
                ):
                    reasons.append("UNVALUED_OR_STALE_EXISTING_EXPOSURE")
        return sorted(set(reasons))

    def _risk_limit(self, policy):
        return (
            self.config.global_max_stress_risk
            if isinstance(policy, NakedPaperPolicy)
            else self.config.global_max_defined_risk
        )

    def _global_policy(self, policy):
        return {
            "positions": self.config.global_max_positions,
            "risk": self._risk_limit(policy),
            "daily_loss": self.config.global_daily_loss_limit,
        }

    def _margin(self, current, candidate, policy, known):
        record = margin_for(
            policy,
            candidate["short_leg"]["instrument_token"],
            candidate.get("long_leg", {}).get("instrument_token"),
            policy.evidence.option_expiry,
            known,
        )
        if record is None and self.margin_reader is not None and isinstance(policy, NakedPaperPolicy):
            estimate = self.margin_reader.estimate(current, candidate, policy)
            if estimate is not None:
                # Reuse exact identity, positive-value and timestamp checks; a
                # custom read capability cannot attach another contract's margin.
                temporary = policy.model_copy(update={"margin_evidence": (estimate,)})
                record = margin_for(
                    temporary,
                    candidate["short_leg"]["instrument_token"],
                    None,
                    policy.evidence.option_expiry,
                    self.clock(),
                )
        return record

    @staticmethod
    def _global_stress_due(rows, current_row, current_doc):
        cap = current_doc["global_policy"].get("risk")
        total = 0
        for row in rows:
            if row.state in ACTIVE:
                doc = current_doc if row.id == current_row.id else json.loads(row.document)
                total += max(reservation(doc), -(doc.get("unrealized_gross") or 0))
        return cap is not None and total > cap

    def _daily_exit(self, rows, row, current_doc, at, known, policy, control):
        own, total = 0.0, 0.0
        for other in rows:
            doc = current_doc if other.id == row.id else json.loads(other.document)
            value = None
            if other.state == "CLOSED" and doc["exit_timestamp"][:10] == at.date().isoformat():
                value = min(
                    doc["gross_realized_pnl"],
                    doc["net_realized_pnl"] if doc.get("net_realized_pnl") is not None else doc["gross_realized_pnl"],
                )
            elif (
                other.state in ("OPEN", "PENDING_EXIT", "UNRESOLVED_EXPOSURE")
                and doc.get("mark_at")
                and (
                    0
                    <= (known - datetime.fromisoformat(doc["mark_at"])).total_seconds()
                    <= doc["policy"]["max_quote_age_seconds"]
                )
            ):
                value = doc.get("unrealized_gross")
                if value is not None and doc.get("unrealized_net") is not None:
                    value = min(value, doc["unrealized_net"])
            if value is not None:
                total += value
                if other.instrument_id == self.instrument:
                    own += value
        global_limit = json.loads(control.policy_json).get("daily_loss")
        return (
            "INSTRUMENT_DAILY_LOSS"
            if own <= -policy.daily_loss_limit
            else "GLOBAL_DAILY_LOSS" if global_limit is not None and total <= -global_limit else None
        )

    def _order(self, session, row, doc, kind, current, sid, known, reason):
        identity = digest({"position_id": row.id, "kind": kind})
        order = session.get(CommodityPaperOrder, identity)
        if order is None:
            order_doc = {
                "id": identity,
                "position_id": row.id,
                "instrument_id": self.instrument,
                "kind": kind,
                "state": "PENDING",
                "signal_at": current.timestamp_ist.isoformat(),
                "created_at": self.clock().isoformat(),
                "signal_snapshot_id": sid,
                "reason": reason,
            }
            if doc.get("strategy") == "NAKED_SHORT_OPTION":
                order_doc.update(
                    strategy="NAKED_SHORT_OPTION",
                    action="SELL" if kind == "ENTRY" else "BUY",
                    contract_quantity=doc["candidate"]["contract_quantity"],
                    broker_orders_enabled=False,
                )
            order = CommodityPaperOrder(
                id=identity,
                instrument_id=self.instrument,
                position_id=row.id,
                state="PENDING",
                observed_at=current.timestamp_ist.isoformat(),
                document=canonical(order_doc),
            )
            session.add(order)
        return order

    def _fill(self, session, order, current, sid, known, execution):
        fill_doc = {
            "id": digest({"order_id": order.id}),
            "order_id": order.id,
            "position_id": order.position_id,
            "instrument_id": self.instrument,
            "kind": json.loads(order.document)["kind"],
            "snapshot_id": sid,
            "observed_at": current.timestamp_ist.isoformat(),
            "processed_at": known.isoformat(),
            "execution": execution,
            "lots": 1,
            "atomic": True,
            "broker_orders_enabled": False,
        }
        fill = CommodityPaperFill(
            id=fill_doc["id"],
            instrument_id=self.instrument,
            order_id=order.id,
            observed_at=current.timestamp_ist.isoformat(),
            document=canonical(fill_doc),
        )
        session.add(fill)
        order_doc = json.loads(order.document)
        order_doc.update(fill_id=fill.id, filled_at=current.timestamp_ist.isoformat())
        self._save(order, order_doc, "FILLED")
        return fill

    def _transition(self, session, cursor, row, current, sid, known, ctx, all_rows):
        doc = json.loads(row.document)
        policy = load_policy(doc["policy"])
        naked = isinstance(policy, NakedPaperPolicy)
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
            reason = (
                "TRADING_DISABLED"
                if not self.enabled
                else (
                    "KILL_SWITCH"
                    if policy.kill_switch or self.policy and self.policy.kill_switch
                    else (
                        "GLOBAL_RISK_POLICY_CHANGED"
                        if doc["global_policy"] != self._global_policy(policy)
                        else (
                            "ENTRY_TTL_EXCEEDED"
                            if (known - signal_at).total_seconds() > policy.entry_ttl_seconds
                            else (
                                "SESSION_ENTRY_CLOSED"
                                if forced_due
                                else (
                                    "PRE_EXPIRY_ENTRY_CUTOFF"
                                    if naked and max(at, known) >= policy.expiry_cutoff()
                                    else (
                                        "EXPIRY_ENTRY_FORBIDDEN"
                                        if (current.expiry - at.date()).days < policy.min_entry_dte
                                        else policy.metadata_reason(current, known)
                                        or acts_v2.entry_valid(ctx, doc["direction"], scorer)
                                    )
                                )
                            )
                        )
                    )
                )
            )
            if not naked and reason is None:
                reason = "LEGACY_SPREAD_ENTRIES_DISABLED"
            execution, failure = (
                execute(current, candidate, policy, known, entry=True, after=max(signal_at, created_at))
                if reason is None
                else (None, None)
            )
            if execution is not None:
                scenario = (
                    stress(
                        current,
                        next(
                            q
                            for q in current.options
                            if q.instrument_token == candidate["short_leg"]["instrument_token"]
                        ),
                        execution["value"],
                        policy,
                    )
                    if naked
                    else None
                )
                risk = (
                    max(scenario["stress_loss_estimate"] if scenario else 0, policy.max_loss_per_trade)
                    if naked
                    else (candidate["spread_width"] - execution["value"]) * policy.evidence.rupees_per_point_per_lot
                )
                over_risk = (
                    scenario is None or scenario["stress_loss_estimate"] > policy.max_stress_loss_per_trade
                    if naked
                    else risk > policy.max_loss_per_trade
                )
                reason = (
                    "FILL_ECONOMICS_OUTSIDE_RISK"
                    if over_risk or execution["value"] <= policy.min_credit
                    else next(iter(self._risk(all_rows, current, known, risk, policy, excluding=row.id)), None)
                )
                unwind = execution["short"]["quote"]["ask"] - (execution["long"]["quote"]["bid"] if not naked else 0)
                if unwind >= execution["value"] * policy.stop_credit_multiple:
                    reason = "FILL_ALREADY_AT_STOP"
                if naked and (unwind - execution["value"]) * policy.evidence.lot_size >= policy.max_loss_per_trade:
                    reason = "FILL_ALREADY_AT_STOP"
                margin_candidate = {**candidate, "executable_credit": execution["value"]}
                margin = self._margin(current, margin_candidate, policy, known)
                if self.margin_reader is not None:
                    known = self.clock()
                    _, margin_age_failure = execute(
                        current, candidate, policy, known, entry=True, after=max(signal_at, created_at)
                    )
                    if margin_age_failure or policy.metadata_reason(current, known):
                        reason = "QUOTE_EXPIRED_DURING_MARGIN_CHECK"
                    if naked and known >= policy.expiry_cutoff():
                        reason = "PRE_EXPIRY_ENTRY_CUTOFF"
                    if known >= datetime.combine(
                        at.date(), forced, at.tzinfo
                    ) or not self.spec.market_session.contains(known):
                        reason = "SESSION_ENTRY_CLOSED"
                    if (known - signal_at).total_seconds() > policy.entry_ttl_seconds:
                        reason = "ENTRY_TTL_EXCEEDED"
                    if reason is None:
                        reason = next(iter(self._risk(all_rows, current, known, risk, policy, excluding=row.id)), None)
                if policy.max_broker_margin is not None and (
                    margin is None or margin["required_margin"] > policy.max_broker_margin
                ):
                    reason = "FILL_MARGIN_UNVERIFIED_OR_LIMIT_EXCEEDED"
            if reason is not None:
                doc["rejection_reason"] = reason
                self._save(row, doc, "ENTRY_REJECTED")
                od = json.loads(order.document)
                od["rejection_reason"] = reason
                self._save(order, od, "REJECTED")
                self._emit(
                    session,
                    cursor,
                    "ENTRY_REJECTED",
                    current,
                    sid,
                    known,
                    (("position", row), ("order", order)),
                    reason=reason,
                )
                return
            if failure:
                od = json.loads(order.document)
                od["last_attempt"] = {"snapshot_id": sid, "reason": failure, "observed_at": at.isoformat()}
                self._save(order, od)
                self._emit(session, cursor, "ENTRY_WAIT", current, sid, known, (("order", order),), reason=failure)
                return
            fill = self._fill(session, order, current, sid, known, execution)
            doc.update(
                entry_timestamp=at.isoformat(),
                entry_snapshot_id=sid,
                entry_credit=execution["value"],
                entry_execution=execution,
                margin_evidence=margin,
                margin_status=(
                    ("VERIFIED_SINGLE_INSTRUMENT_ESTIMATE" if naked else "VERIFIED_PORTFOLIO_ESTIMATE")
                    if margin
                    else "MARGIN_UNVERIFIED"
                ),
            )
            if naked:
                doc.update(
                    risk_reservation=risk,
                    stress_loss_estimate=scenario["stress_loss_estimate"],
                    entry_stress=scenario,
                    entry_processed_at=known.isoformat(),
                    contract_quantity=policy.evidence.lot_size,
                )
            else:
                doc["defined_risk"] = risk
            self._save(row, doc, "OPEN")
            self._emit(
                session,
                cursor,
                "ENTRY_FILLED",
                current,
                sid,
                known,
                (("position", row), ("order", order), ("fill", fill)),
            )
            if naked:
                return  # Do not invent a same-observation post-entry mark.
        after = (
            max(datetime.fromisoformat(doc["entry_timestamp"]), datetime.fromisoformat(doc["entry_processed_at"]))
            if naked
            else None
        )
        execution, failure = execute(current, candidate, policy, known, entry=False, after=after)
        reason = (
            "FORCED_SESSION_EXIT"
            if forced_due
            else (
                "PRE_EXPIRY_SAFEGUARD"
                if naked and max(at, known) >= policy.expiry_cutoff()
                else (
                    "EXPIRY_SAFEGUARD"
                    if at.date() >= policy.evidence.option_expiry
                    else (
                        "KILL_SWITCH"
                        if policy.kill_switch or self.policy and self.policy.kill_switch
                        else (
                            "TIME_STOP"
                            if (at - datetime.fromisoformat(doc["entry_timestamp"])).total_seconds()
                            >= policy.max_holding_minutes * 60
                            else None
                        )
                    )
                )
            )
        )
        if execution is not None:
            debit = execution["value"]
            gross_points = doc["entry_credit"] - debit
            doc.update(
                current_debit=debit,
                unrealized_gross=pnl(doc["entry_credit"], debit, policy.evidence.rupees_per_point_per_lot),
                mark_at=at.isoformat(),
                mark_execution=execution,
                best_gross_points=max(doc.get("best_gross_points", gross_points), gross_points),
                unresolved_reason=None,
            )
            doc["unrealized_net"] = policy.net(
                doc["unrealized_gross"], at.date(), datetime.fromisoformat(doc["entry_timestamp"]).date()
            )
            credit, best = doc["entry_credit"], doc["best_gross_points"]
            if naked:
                quote = next(
                    q for q in current.options if q.instrument_token == candidate["short_leg"]["instrument_token"]
                )
                scenario = (
                    stress(current, quote, credit, policy) if policy.metadata_reason(current, known) is None else None
                )
                doc.update(
                    current_stress=scenario,
                    risk_reservation=max(
                        doc["risk_reservation"],
                        scenario["stress_loss_estimate"] if scenario else -doc["unrealized_gross"],
                    ),
                )
            if reason is None:
                reason = self._daily_exit(
                    all_rows, row, doc, at, known, policy, session.get(CommodityPaperControl, 1)
                ) or (
                    ("PREMIUM_LOSS_STOP" if naked else "HARD_MAX_LOSS")
                    if doc["unrealized_gross"] <= -policy.max_loss_per_trade
                    else (
                        ("PREMIUM_MULTIPLE_STOP" if naked else "SPREAD_STOP")
                        if debit >= credit * policy.stop_credit_multiple
                        else (
                            "STRESS_RISK_EXIT"
                            if naked
                            and (
                                scenario is None or scenario["stress_loss_estimate"] > policy.max_stress_loss_per_trade
                            )
                            else (
                                "GLOBAL_STRESS_RISK_EXIT"
                                if naked and self._global_stress_due(all_rows, row, doc)
                                else (
                                    "TRAILING_EXIT"
                                    if best >= credit * policy.trailing_activation_pct / 100
                                    and gross_points <= best - credit * policy.trailing_giveback_pct / 100
                                    else (
                                        "PROFIT_CAPTURE"
                                        if debit <= credit * (1 - policy.profit_capture_pct / 100)
                                        else None
                                    )
                                )
                            )
                        )
                    )
                )
            if reason is None:
                mirror = doc.setdefault("mirror_watch", {})
                _, direction, _ = acts_v2.market_state(ctx, mirror, scorer)
                # Margin is an entry/capital constraint, not a prerequisite to
                # a risk-reducing BUY. This only evaluates an opposite signal;
                # any actual subsequent SELL must pass normal entry gates again.
                opposite, rejects = (
                    candidates(current, direction, ctx, policy, known, deferred_margin=naked)
                    if direction is not None and direction != doc["direction"]
                    else (None, {})
                )
                signal = decide(ctx, mirror, opposite, rejects, [], scorer)
                doc["mirror_signal"] = signal
                if signal["status"] == "ENTER" and signal["direction"] != doc["direction"]:
                    reason = "OPPOSITE_ACTS_V2_ENTRY_SIGNAL"
        else:
            doc.update(
                current_debit=None, unrealized_gross=None, unrealized_net=None, mark_at=None, unresolved_reason=failure
            )
        if reason and not doc.get("exit_order_id"):
            order = self._order(session, row, doc, "EXIT", current, sid, known, reason)
            doc.update(exit_order_id=order.id, exit_reason=reason)
            self._save(row, doc, "PENDING_EXIT" if execution else "UNRESOLVED_EXPOSURE")
            self._emit(
                session,
                cursor,
                "EXIT_REQUESTED",
                current,
                sid,
                known,
                (("position", row), ("order", order)),
                reason=reason,
            )
        if doc.get("exit_order_id"):
            order = session.get(CommodityPaperOrder, doc["exit_order_id"])
            od = json.loads(order.document)
            signal_at = max(datetime.fromisoformat(od["signal_at"]), datetime.fromisoformat(od["created_at"]))
            if at <= signal_at:
                return
            closing, failure = execute(
                current, candidate, policy, known, entry=False, after=max(signal_at, after) if after else signal_at
            )
            if closing is not None:
                fill = self._fill(session, order, current, sid, known, closing)
                gross = pnl(doc["entry_credit"], closing["value"], policy.evidence.rupees_per_point_per_lot)
                margin = doc.get("margin_evidence")
                doc.update(
                    exit_timestamp=at.isoformat(),
                    exit_snapshot_id=sid,
                    exit_debit=closing["value"],
                    exit_execution=closing,
                    gross_realized_pnl=gross,
                    net_realized_pnl=policy.net(
                        gross, at.date(), datetime.fromisoformat(doc["entry_timestamp"]).date()
                    ),
                    unrealized_gross=None,
                    unrealized_net=None,
                    return_on_verified_margin=(
                        gross / margin["required_margin"]
                        if margin and margin.get("measurement_kind") == "ACTUAL_BLOCKED"
                        else None
                    ),
                    gross_pnl_over_verified_margin_estimate=gross / margin["required_margin"] if margin else None,
                    unresolved_reason=None,
                )
                self._save(row, doc, "CLOSED")
                self._emit(
                    session,
                    cursor,
                    "EXIT_FILLED",
                    current,
                    sid,
                    known,
                    (("position", row), ("order", order), ("fill", fill)),
                )
                return
            doc["unresolved_reason"] = failure
        self._save(
            row,
            doc,
            (
                "UNRESOLVED_EXPOSURE"
                if execution is None or doc.get("exit_order_id") and failure
                else "PENDING_EXIT" if doc.get("exit_order_id") else "OPEN"
            ),
        )
        self._emit(
            session,
            cursor,
            "POSITION_OBSERVED",
            current,
            sid,
            known,
            (("position", row),),
            reason=doc.get("unresolved_reason"),
        )

    def process_snapshot(self, sid):
        with self.sessions() as session:
            raw = session.get(CommodityMarketSnapshotRecord, sid)
            research = session.scalar(
                select(CommodityActsDecision).where(
                    CommodityActsDecision.snapshot_id == sid, CommodityActsDecision.instrument_id == self.instrument
                )
            )
            if raw is None or research is None or raw.instrument_id != self.instrument:
                raise ValueError("COMMODITY_PAPER_EVIDENCE_REQUIRED")
            current = CommoditySnapshot.model_validate_json(raw.snapshot_json)
            research_doc = json.loads(research.document)
            alpha = AlphaFeatureSnapshot.model_validate(research_doc["alpha"])
        known = self.clock()
        if current.timestamp_ist > known:
            raise ValueError("COMMODITY_PAPER_FUTURE_OBSERVATION")
        ctx = build_context(
            current, self.research._history(current), self.spec, known, self.config.max_quote_age_seconds
        )
        ctx["acts"] = {
            "alpha": alpha_evidence(alpha, known, current.future_instrument_id, current.future_expiry, current.expiry)
        }
        with self.sessions.begin() as session:
            # One commodity-only DB mutex serializes global risk reservations
            # across independent instrument processes (also effective on SQLite).
            result = session.execute(
                update(CommodityPaperControl)
                .where(CommodityPaperControl.id == 1)
                .values(lock_version=CommodityPaperControl.lock_version + 1)
            )
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
            ctx["acts"]["alpha"] = alpha_evidence(
                alpha, known, current.future_instrument_id, current.future_expiry, current.expiry
            )
            state = json.loads(cursor.state_json)
            identity = [
                current.session_id,
                current.future_token,
                current.future_expiry.isoformat(),
                current.expiry.isoformat(),
            ]
            if state.get("identity") != identity:
                state.update(identity=identity, watch={})
            rows = list(session.scalars(select(CommodityPaperPosition).order_by(CommodityPaperPosition.id)))
            for row in rows:
                if row.instrument_id == self.instrument and row.state in ACTIVE:
                    self._transition(session, cursor, row, current, sid, known, ctx, rows)
            session.flush()
            watch = state.setdefault("watch", {})
            _, direction, _ = acts_v2.market_state(ctx, watch, self.scorer)
            candidate, rejected = (
                candidates(current, direction, ctx, self.policy, known, deferred_margin=self.margin_reader is not None)
                if self.enabled and self.policy is not None and direction is not None
                else (None, {})
            )
            blockers = []
            if not self.enabled:
                blockers.append("PAPER_ACTIVATION_DISABLED")
            if self.policy is None:
                blockers.append("PAPER_POLICY_UNCONFIGURED")
            if self.policy is not None:
                at, forced = current.timestamp_ist, self.spec.market_session.forced_exit
                seconds_to_forced = (datetime.combine(at.date(), forced, at.tzinfo) - max(at, known)).total_seconds()
                if (
                    seconds_to_forced <= self.policy.entry_ttl_seconds
                    or not self.spec.market_session.contains(at)
                    or not self.spec.market_session.contains(known)
                ):
                    blockers.append("SESSION_ENTRY_CLOSED")
                if not isinstance(self.policy, NakedPaperPolicy):
                    blockers.append("LEGACY_SPREAD_ENTRIES_DISABLED")
                if isinstance(self.policy, NakedPaperPolicy) and max(at, known) >= self.policy.expiry_cutoff():
                    blockers.append("PRE_EXPIRY_ENTRY_CUTOFF")
                if (current.expiry - at.date()).days < self.policy.min_entry_dte:
                    blockers.append("EXPIRY_ENTRY_FORBIDDEN")
                if self.policy.kill_switch:
                    blockers.append("KILL_SWITCH")
                if candidate is not None:
                    blockers.extend(self._risk(rows, current, known, candidate_risk(candidate), self.policy))
                    global_policy = self._global_policy(self.policy)
                    if control.policy_json not in ("{}", canonical(global_policy)):
                        blockers.append("GLOBAL_RISK_POLICY_CHANGED")
                    if not blockers and self.margin_reader is not None and isinstance(self.policy, NakedPaperPolicy):
                        margin = self._margin(current, candidate.model_dump(mode="json"), self.policy, known)
                        known = self.clock()
                        ctx["decided_at"] = known.isoformat()
                        ctx["acts"]["alpha"] = alpha_evidence(
                            alpha, known, current.future_instrument_id, current.future_expiry, current.expiry
                        )
                        _, failure = execute(
                            current, candidate.model_dump(mode="json"), self.policy, known, entry=True
                        )
                        if failure or self.policy.metadata_reason(current, known):
                            blockers.append("QUOTE_EXPIRED_DURING_MARGIN_CHECK")
                        if known >= self.policy.expiry_cutoff():
                            blockers.append("PRE_EXPIRY_ENTRY_CUTOFF")
                        if (
                            datetime.combine(at.date(), forced, at.tzinfo) - known
                        ).total_seconds() <= self.policy.entry_ttl_seconds or not self.spec.market_session.contains(
                            known
                        ):
                            blockers.append("SESSION_ENTRY_CLOSED")
                        if self.policy.max_broker_margin is not None and (
                            margin is None or margin["required_margin"] > self.policy.max_broker_margin
                        ):
                            blockers.append("MARGIN_UNVERIFIED_OR_LIMIT_EXCEEDED")
                        candidate.construction_evidence["margin"] = {
                            "status": "VERIFIED_SINGLE_INSTRUMENT_ESTIMATE" if margin else "MARGIN_UNVERIFIED",
                            "required_margin": margin["required_margin"] if margin else None,
                            "evidence": margin,
                            "premium_to_verified_margin_estimate": (
                                candidate.executable_credit * candidate.contract_quantity / margin["required_margin"]
                                if margin
                                else None
                            ),
                        }
            decision = decide(ctx, watch, candidate, rejected, sorted(set(blockers)), self.scorer)
            if decision["status"] == "ENTER":
                control.policy_json = canonical(global_policy)
                pid = digest({"instrument_id": self.instrument, "decision_snapshot_id": sid, "version": VERSION})
                doc = json_value(
                    {
                        "id": pid,
                        "instrument_id": self.instrument,
                        "state": "PENDING_PAPER_ENTRY",
                        "session_id": current.session_id,
                        "decision_timestamp": current.timestamp_ist,
                        "decision_snapshot_id": sid,
                        "processed_at": known,
                        "candidate": candidate,
                        "direction": decision["direction"],
                        "decision": decision,
                        "policy": self.policy,
                        "policy_hash": digest(self.policy),
                        "acts_policy": self.scorer.payload(),
                        "future_token": current.future_token,
                        "global_policy": global_policy,
                        "forced_exit_time": self.spec.market_session.forced_exit,
                        "strategy": "NAKED_SHORT_OPTION",
                        "version": VERSION,
                        "risk_reservation": candidate.risk_reservation,
                        "stress_loss_estimate": candidate.stress_loss_estimate,
                        "maximum_loss": None,
                        "loss_is_bounded": False,
                        "margin_status": candidate.construction_evidence["margin"]["status"],
                        "decision_margin_evidence": candidate.construction_evidence["margin"].get("evidence"),
                        "gross_realized_pnl": None,
                        "net_realized_pnl": None,
                        "unrealized_gross": None,
                        "unrealized_net": None,
                        "mark_at": None,
                    }
                )
                row = CommodityPaperPosition(
                    id=pid, instrument_id=self.instrument, state="PENDING_PAPER_ENTRY", document=canonical(doc)
                )
                row.observed_at = current.timestamp_ist.isoformat()
                session.add(row)
                session.flush()
                order = self._order(session, row, doc, "ENTRY", current, sid, known, "ACTS_V2_ENTER")
                doc["entry_order_id"] = order.id
                self._save(row, doc)
                self._emit(
                    session, cursor, "PENDING_PAPER_ENTRY", current, sid, known, (("position", row), ("order", order))
                )
            latest = json_value(
                {
                    "version": VERSION,
                    "strategy": "NAKED_SHORT_OPTION",
                    "stage": "AUTOMATED_PAPER",
                    "instrument_id": self.instrument,
                    "observed_at": current.timestamp_ist,
                    "processed_at": known,
                    "snapshot_id": sid,
                    "decision": decision,
                    "candidate": candidate,
                    "risk_blocks": sorted(set(blockers)),
                    "broker_orders_enabled": False,
                    "paper_enabled": self.enabled,
                    "margin_status": (
                        candidate.construction_evidence["margin"]["status"] if candidate else "MARGIN_UNVERIFIED"
                    ),
                }
            )
            latest.update(
                future_price=current.future_price,
                future_symbol=current.future_instrument_id,
                future_expiry=current.future_expiry.isoformat(),
                option_expiry=current.expiry.isoformat(),
                source_market_timestamp=(
                    current.source_market_timestamp.isoformat() if current.source_market_timestamp else None
                ),
                data_quality={
                    key: research_doc["geometry"].get(key)
                    for key in ("contracts", "bid_ask_coverage", "depth_coverage", "source_timestamp_coverage")
                },
            )
            self._emit(
                session,
                cursor,
                "PAPER_OBSERVATION",
                current,
                sid,
                known,
                state=state,
                latest=latest,
                snapshot_hash=digest(current),
                scoring_policy=self.scorer.payload(),
                risk_configuration={
                    "positions": self.config.global_max_positions,
                    "stress_risk": self.config.global_max_stress_risk,
                    "daily_loss": self.config.global_daily_loss_limit,
                },
                paper_policy=self.policy,
                paper_policy_hash=digest(self.policy) if self.policy else None,
            )
        return "PAPER_OBSERVATION"
