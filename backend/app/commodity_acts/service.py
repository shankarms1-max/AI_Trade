"""Captured-data ACTS/Alpha research only; there is deliberately no trade type."""

from dataclasses import asdict, replace
from datetime import datetime
import json
import re

from sqlalchemy import select

from app.acts_paper import acts_v2
from app.alpha.clock import IST, using_session
from app.alpha.engine import build_alpha_features, config_from_settings
from app.alpha.models import AlphaFeatureSnapshot, AlphaValidity, CalculationMode
from app.commodity_acts.broker import assert_market_only
from app.commodity_acts.config import RESEARCH_SCOPE, STAGE, VERSION, CommoditySettings, InstrumentSpec
from app.commodity_acts.context import build_context
from app.commodity_acts.domain import CommoditySnapshot
from app.commodity_acts.geometry import geometry
from app.commodity_acts.entry import direction, directional_parameters
from app.commodity_acts.models import (
    CommodityActsDecision,
    CommodityActsEvent,
    CommodityMarketSnapshotRecord,
)
from app.commodity_acts.repository import append_event, bucket, locked_cursor, save_snapshot, verify_journal
from app.research.manifest import canonical, digest, json_value
from app.scalper_lab.acts import alpha_evidence


def _alpha_bucket(at):
    local = at.astimezone(IST)
    return local.replace(minute=local.minute // 3 * 3, second=0, microsecond=0)


class CommodityResearch:
    def __init__(
        self, sessions, settings, config: CommoditySettings, spec: InstrumentSpec, clock=lambda: datetime.now(IST)
    ):
        self.sessions, self.config, self.spec, self.clock = sessions, config, spec, clock
        self.acts_config = acts_v2.ActsV2Config.from_settings(settings)
        self.alpha_config = replace(
            config_from_settings(settings), price_source="FUTURE", holiday_dates=spec.market_session.holidays
        )
        if (
            self.acts_config.watch_score,
            self.acts_config.tradeable_score,
            self.acts_config.strong_score,
            self.acts_config.very_strong_score,
        ) != (40, 55, 70, 85):
            raise ValueError("COMMODITY_REQUIRES_ORIGINAL_ACTS_SCORE_BANDS")
        self.policy = json_value(
            {
                "version": VERSION,
                "stage": STAGE,
                "instrument": spec,
                "acts": self.acts_config.payload(),
                "directional_context": directional_parameters(self.acts_config),
                "alpha": asdict(self.alpha_config),
                "execution": "DISABLED_GEOMETRY_NOT_VALIDATED",
                "strategy_clock": "MARKET_OBSERVATION_TIME",
                "alpha_known_at": "ACTUAL_PROCESSING_TIME",
                "alpha_sampling": "FIRST_CAPTURE_IN_EACH_COMPLETED_OR_CURRENT_3_MINUTE_BUCKET",
            }
        )
        self.policy_hash = digest(self.policy)
        # Exact known predecessor only: unchanged Alpha/capture data can continue.
        # Historical documents/events/hashes are never rewritten.
        predecessor = {key: value for key, value in self.policy.items() if key != "directional_context"}
        self.previous_policy_hash = digest({**predecessor, "stage": "SCORE_TRANSFERABILITY"})
        verify_journal(sessions, spec.instrument_id)

    def _history(self, current):
        with self.sessions() as session:
            rows = session.scalars(
                select(CommodityMarketSnapshotRecord)
                .where(
                    CommodityMarketSnapshotRecord.instrument_id == self.spec.instrument_id,
                    CommodityMarketSnapshotRecord.captured_at < current.timestamp_ist,
                )
                .order_by(CommodityMarketSnapshotRecord.captured_at.desc())
                .limit(self.config.history_limit)
            ).all()
            history = [CommoditySnapshot.model_validate_json(row.snapshot_json) for row in reversed(rows)]
        return [
            row
            for row in history
            if row.instrument_id == current.instrument_id
            and row.snapshot_persisted_at is not None
            and row.snapshot_persisted_at <= current.timestamp_ist
            and (row.future_token, row.future_instrument_id, row.future_expiry, row.expiry)
            == (current.future_token, current.future_instrument_id, current.future_expiry, current.expiry)
        ]

    def _alpha(self, snapshot_id, current, history, state):
        previous = state.get("alpha")
        if previous is not None:
            previous = AlphaFeatureSnapshot.model_validate(previous)
            if (
                _alpha_bucket(previous.timestamp) == _alpha_bucket(current.timestamp_ist)
                and previous.reference_instrument_id == current.future_instrument_id
                and previous.reference_expiry == current.future_expiry
                and previous.expiry == current.expiry
            ):
                return previous
        choices = {}
        for row in history:
            if _alpha_bucket(row.timestamp_ist) < _alpha_bucket(current.timestamp_ist):
                choices.setdefault(_alpha_bucket(row.timestamp_ist), row)
        alpha_history = list(choices.values())
        prior = [previous] if previous is not None and previous.timestamp < current.timestamp_ist else []
        with using_session(self.spec.market_session.clock(self.spec.instrument_id)):
            # One original Alpha implementation, neutral observation fields and
            # explicit per-instrument clock/history. No MarketSnapshot conversion.
            return build_alpha_features(
                snapshot_id, current, alpha_history, prior, self.alpha_config, CalculationMode.LIVE_ORIGINAL
            )

    def process_snapshot(self, snapshot_id: int) -> str:
        with self.sessions() as session:
            record = session.get(CommodityMarketSnapshotRecord, snapshot_id)
            if record is None or record.instrument_id != self.spec.instrument_id:
                raise ValueError("COMMODITY_SNAPSHOT_OWNER_MISMATCH")
            current = CommoditySnapshot.model_validate_json(record.snapshot_json)
        if current.timestamp_ist > self.clock():
            raise ValueError("COMMODITY_FUTURE_OBSERVATION")
        history = self._history(current)
        with self.sessions.begin() as session:
            cursor = locked_cursor(session, self.spec.instrument_id)
            if (
                session.scalar(
                    select(CommodityActsDecision.id).where(
                        CommodityActsDecision.instrument_id == self.spec.instrument_id,
                        CommodityActsDecision.snapshot_id == snapshot_id,
                    )
                )
                is not None
            ):
                return "DUPLICATE"
            if cursor.observed_at and current.timestamp_ist <= datetime.fromisoformat(cursor.observed_at):
                raise ValueError("COMMODITY_OUT_OF_ORDER_OBSERVATION")
            state = json.loads(cursor.state_json)
            if state.get("policy_hash") not in (None, self.policy_hash, self.previous_policy_hash):
                raise ValueError("COMMODITY_RESEARCH_POLICY_CHANGED")
            identity = json_value(
                {
                    "future_token": current.future_token,
                    "future_symbol": current.future_instrument_id,
                    "future_expiry": current.future_expiry,
                    "option_expiry": current.expiry,
                }
            )
            if state.get("reference_identity") != identity:
                state.pop("alpha", None)
                state["watch"] = {}
            if state.get("session_id") != current.session_id:
                state["watch"] = {}
            alpha = self._alpha(snapshot_id, current, history, state)
            decided_at = self.clock()
            if alpha.feature_calculated_at is not None and alpha.feature_calculated_at > decided_at:
                raise ValueError("COMMODITY_ALPHA_NOT_YET_KNOWN")
            ctx = build_context(current, history, self.spec, decided_at, self.config.max_quote_age_seconds)
            # Alpha is checked as actually known at processing time. Its real
            # calculation time must not be backdated to ctx's observation clock.
            ctx["acts"] = {
                "alpha": alpha_evidence(
                    alpha, decided_at, current.future_instrument_id, current.future_expiry, current.expiry
                )
            }
            evidence = geometry(current, self.config.max_quote_age_seconds)
            decision = direction(ctx, state.setdefault("watch", {}), self.acts_config)
            unavailable = ["EXECUTION_SPREAD", "EXECUTION_DEPTH", "EXECUTION_CREDIT", "CASH_SPOT", "COMMODITY_VIX"]
            if ctx["acts"]["alpha"]["status"] != "AVAILABLE":
                unavailable.extend(["ALPHA_1", "ALPHA_2"])
            if ctx["opening_high"] is None:
                unavailable.append("OPENING_STRUCTURE")
            if any(not ctx["options"]["local_pcr"][key]["complete"] for key in ("3", "5")):
                unavailable.append("COMPLETE_LOCAL_POSITIONING_BASKETS")
            if current.source_market_timestamp is None:
                unavailable.append("REFERENCE_SOURCE_TIMESTAMP")
            if not evidence["atm_pair_complete"]:
                unavailable.append("ATM_QUOTES_UNAVAILABLE")
            if evidence["modal_strike_step"] is None:
                unavailable.append("STRIKE_GRID_UNDETERMINED")
            readiness = (
                "INSUFFICIENT_OPTION_PAIRS"
                if not evidence["atm_pair_complete"] or evidence["complete_pairs"] < self.config.minimum_option_pairs
                else (
                    "QUOTE_DEPTH_INSUFFICIENT"
                    if evidence["depth_count"] < 2 * self.config.minimum_option_pairs
                    else (
                        "ALPHA_WARMING_UP"
                        if alpha.validity_state != AlphaValidity.VALID
                        else "READY_FOR_ACTS_RESEARCH"
                    )
                )
            )
            document = json_value(
                {
                    "version": VERSION,
                    "stage": STAGE,
                    "research_scope": RESEARCH_SCOPE,
                    "instrument_id": current.instrument_id,
                    "session_id": current.session_id,
                    "snapshot_id": snapshot_id,
                    "observed_at": current.timestamp_ist,
                    "decided_at": decided_at,
                    "decision_kind": "SHADOW_DECISION",
                    "readiness": readiness,
                    "mode": "PAPER",
                    "broker_orders_enabled": False,
                    "spread_execution_enabled": False,
                    "pnl": None,
                    "policy": self.policy,
                    "policy_hash": self.policy_hash,
                    "reference_price": current.reference_price,
                    "future_price": current.future_price,
                    "reference_basis": current.reference_basis,
                    "future_instrument_id": current.future_instrument_id,
                    "future_token": current.future_token,
                    "future_exchange": current.future_exchange,
                    "future_expiry": current.future_expiry,
                    "option_expiry": current.expiry,
                    "lot_size": current.lot_size,
                    "atm": current.atm_strike,
                    "geometry": evidence,
                    "alpha": alpha,
                    "acts": decision,
                    "unavailable_components": unavailable,
                    "score_basis": "CONTEXT_ONLY_NORMALIZED_BY_AVAILABLE_WEIGHT",
                    "previous_policy_hash": (
                        state.get("policy_hash") if state.get("policy_hash") != self.policy_hash else None
                    ),
                    "execution_points_available": False,
                    "maximum_score_without_execution": 100
                    - sum(
                        self.acts_config.weights[name]
                        for name in ("execution_spread", "execution_depth", "execution_credit")
                    ),
                    "timestamp_provenance": {
                        "request_started_at": current.request_started_at,
                        "response_received_at": current.response_received_at,
                        "future_source_timestamp": current.source_market_timestamp,
                        "future_source_timestamp_raw": current.raw_future_source_timestamp,
                        "snapshot_persisted_at": current.snapshot_persisted_at,
                        "feature_calculated_at": alpha.feature_calculated_at,
                    },
                    "data_provenance": current.provenance,
                }
            )
            state.update(
                policy_hash=self.policy_hash,
                session_id=current.session_id,
                reference_identity=identity,
                alpha=json_value(alpha),
            )
            session.add(
                CommodityActsDecision(
                    instrument_id=current.instrument_id,
                    snapshot_id=snapshot_id,
                    decided_at=decided_at,
                    document=canonical(document),
                )
            )
            append_event(
                session,
                cursor,
                digest({"instrument_id": current.instrument_id, "snapshot_id": snapshot_id}),
                "SHADOW_DECISION",
                document,
                state,
                snapshot_id=snapshot_id,
                snapshot_hash=digest(current),
            )
            cursor.observed_at = current.timestamp_ist.isoformat()
        return "SHADOW_DECISION"

    def observe(self, snapshot: CommoditySnapshot) -> tuple[int, str]:
        if snapshot.instrument_id != self.spec.instrument_id or not self.spec.market_session.contains(
            snapshot.timestamp_ist
        ):
            raise ValueError("COMMODITY_CAPTURE_INSTRUMENT_OR_SESSION_MISMATCH")
        snapshot_id, _ = save_snapshot(self.sessions, snapshot, self.clock())
        return snapshot_id, self.process_snapshot(snapshot_id)

    def capture_once(self, market, at: datetime) -> tuple[int, str] | None:
        assert_market_only(market)
        if not self.spec.market_session.contains(at):
            return None
        # Resume captured-but-not-yet-evaluated evidence before newer decisions.
        with self.sessions() as session:
            pending = list(
                session.scalars(
                    select(CommodityMarketSnapshotRecord.id)
                    .where(
                        CommodityMarketSnapshotRecord.instrument_id == self.spec.instrument_id,
                        ~select(CommodityActsDecision.id)
                        .where(
                            CommodityActsDecision.instrument_id == self.spec.instrument_id,
                            CommodityActsDecision.snapshot_id == CommodityMarketSnapshotRecord.id,
                        )
                        .exists(),
                    )
                    .order_by(CommodityMarketSnapshotRecord.captured_at)
                    .limit(20)
                )
            )
        for snapshot_id in pending:
            self.process_snapshot(snapshot_id)
        if len(pending) == 20:
            return None  # Drain a larger backlog without creating an out-of-order decision.
        return self.observe(market.capture(self.spec))

    def record_failure(self, reason: str, at: datetime) -> None:
        if not re.fullmatch(r"[A-Z_]{1,80}", reason):
            reason = "COMMODITY_MARKET_REQUEST_FAILED"
        with self.sessions.begin() as session:
            cursor = locked_cursor(session, self.spec.instrument_id)
            key = digest({"instrument_id": self.spec.instrument_id, "failure_bucket": bucket(at)})
            if (
                session.scalar(
                    select(CommodityActsEvent.sequence).where(
                        CommodityActsEvent.instrument_id == self.spec.instrument_id,
                        CommodityActsEvent.event_key == key,
                    )
                )
                is not None
            ):
                return
            readiness = (
                reason
                if reason in ("FUTURE_UNRESOLVED", "OPTION_EXPIRY_UNRESOLVED", "OPTION_CHAIN_UNAVAILABLE")
                else "NOT_CAPTURED"
            )
            latest = {
                "version": VERSION,
                "stage": STAGE,
                "research_scope": RESEARCH_SCOPE,
                "instrument_id": self.spec.instrument_id,
                "readiness": readiness,
                "blockers": [reason],
                "observed_at": at.isoformat(),
                "spread_execution_enabled": False,
                "broker_orders_enabled": False,
                "pnl": None,
            }
            append_event(session, cursor, key, "CAPTURE_FAILED", latest, json.loads(cursor.state_json))
