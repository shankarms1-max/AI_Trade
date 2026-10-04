from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    MarketFeatureSnapshotRecord, MarketRegimeSnapshotRecord, OperationalEventRecord,
    RiskDecisionRecord, ShadowTradeRecord,
)
from app.features.repository import FeatureRepository
from app.notifications.dedupe import (
    candidate_key, daily_key, operational_key, regime_key, risk_key,
    shadow_entry_key, shadow_exit_key,
)
from app.notifications.formatter import (
    format_candidate, format_daily_summary, format_operational_alert, format_regime,
    format_risk, format_shadow_entry, format_shadow_exit,
)
from app.notifications.models import (
    NotificationEventCode, NotificationPriority, NotificationRequest,
)
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.observability.health import market_session_state
from app.observability.models import MarketSessionState
from app.observability.service import get_daily_operations_summary
from app.pipeline.repository import PipelineRepository
from app.regime.repository import RegimeRepository
from app.risk.repository import RiskRepository
from app.shadow.repository import ShadowRepository
from app.strategy.repository import StrategyRepository

IST = ZoneInfo("Asia/Kolkata")

IMPORTANT_RISK_REASONS = {
    "MAX_LOSS_EXCEEDED", "DAILY_LOSS_LIMIT_REACHED", "MAX_TRADES_REACHED",
    "BLOCKING_MARKET_EVENT", "INTRADAY_OI_UNUSABLE", "LOT_SIZE_UNAVAILABLE",
    "RISK_LIMIT_UNCONFIGURED",
    "SHADOW_RISK_LIMIT_UNCONFIGURED",
}


def is_important_risk_rejection(decision: dict[str, Any]) -> bool:
    codes = set(decision.get("failed_checks") or []) | set(decision.get("reason_codes") or [])
    return bool(IMPORTANT_RISK_REASONS.intersection(codes))

OPERATIONAL_MAP: dict[str, tuple[NotificationEventCode, NotificationPriority, str, bool]] = {
    "COLLECTOR_HEARTBEAT_MISSED": (NotificationEventCode.COLLECTOR_STALE, NotificationPriority.CRITICAL, "UNHEALTHY", False),
    "COLLECTOR_RUN_FAILED": (NotificationEventCode.COLLECTOR_FAILED, NotificationPriority.IMPORTANT, "DEGRADED", False),
    "COLLECTOR_RECOVERED": (NotificationEventCode.COLLECTOR_RECOVERED, NotificationPriority.IMPORTANT, "HEALTHY", True),
    "DATA_FRESHNESS_STALE": (NotificationEventCode.DATA_FRESHNESS_UNHEALTHY, NotificationPriority.CRITICAL, "UNHEALTHY", False),
    "DATA_FRESHNESS_RECOVERED": (NotificationEventCode.DATA_FRESHNESS_RECOVERED, NotificationPriority.IMPORTANT, "HEALTHY", True),
    "DATABASE_UNHEALTHY": (NotificationEventCode.DATABASE_UNHEALTHY, NotificationPriority.CRITICAL, "UNHEALTHY", False),
    "DATABASE_RECOVERED": (NotificationEventCode.DATABASE_RECOVERED, NotificationPriority.IMPORTANT, "HEALTHY", True),
    "PIPELINE_PARTIAL": (NotificationEventCode.PIPELINE_PARTIAL, NotificationPriority.IMPORTANT, "DEGRADED", False),
    "PIPELINE_FAILED": (NotificationEventCode.PIPELINE_FAILED, NotificationPriority.CRITICAL, "UNHEALTHY", False),
    "PIPELINE_RECOVERED": (NotificationEventCode.PIPELINE_RECOVERED, NotificationPriority.IMPORTANT, "HEALTHY", True),
    "MARKET_DATA_DEGRADED": (NotificationEventCode.MARKET_DATA_DEGRADED, NotificationPriority.IMPORTANT, "DEGRADED", False),
    "MARKET_DATA_RECOVERED": (NotificationEventCode.MARKET_DATA_RECOVERED, NotificationPriority.IMPORTANT, "HEALTHY", True),
}


def should_notify_directional(
    directions: list[str], current: str, required: int
) -> tuple[bool, bool]:
    """Return (notify, changed) for the first confirmed directional state."""
    if current not in {"BULLISH", "BEARISH"} or len(directions) < required:
        return False, False
    if any(item != current for item in directions[:required]):
        return False, False
    prior = directions[required] if len(directions) > required else None
    if prior == current:
        return False, False
    return True, prior in {"BULLISH", "BEARISH"} and prior != current


class NotificationRouter:
    def __init__(
        self, session_factory: sessionmaker[Session], settings: Any,
        service: NotificationService | None = None,
    ) -> None:
        self._sessions = session_factory
        self.settings = settings
        self.service = service or NotificationService(
            NotificationRepository(session_factory), settings
        )

    def route_operational_events(self, *, limit: int = 200) -> int:
        if not self.settings.telegram_enabled or not self.settings.telegram_notify_operational:
            return 0
        now = datetime.now(IST)
        start = datetime.combine(now.date(), time.min, IST)
        with self._sessions() as session:
            events = session.scalars(select(OperationalEventRecord).where(
                OperationalEventRecord.created_at >= start,
                OperationalEventRecord.event_code.in_(list(OPERATIONAL_MAP)),
            ).order_by(OperationalEventRecord.created_at.asc(),
                       OperationalEventRecord.id.asc()).limit(limit)).all()
        sent = 0
        for event in events:
            mapped = OPERATIONAL_MAP.get(event.event_code)
            if mapped is None:
                continue
            code, priority, status, recovered = mapped
            if event.event_code == "DATA_FRESHNESS_STALE" and (
                event.metadata_json or {}
            ).get("health_status") == "DEGRADED":
                code = NotificationEventCode.DATA_FRESHNESS_DEGRADED
                priority = NotificationPriority.IMPORTANT
                status = "DEGRADED"
            observed_at = event.created_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=IST)
            if code in {NotificationEventCode.COLLECTOR_STALE,
                        NotificationEventCode.DATA_FRESHNESS_DEGRADED,
                        NotificationEventCode.DATA_FRESHNESS_UNHEALTHY}:
                state = market_session_state(
                    observed_at, self.settings.collector_start_time,
                    self.settings.collector_end_time, self.settings.configured_holidays,
                )
                if state != MarketSessionState.OPEN:
                    continue
            sent += int(self.service.notify(NotificationRequest(
                event_code=code, dedupe_key=operational_key(event.id, code.value),
                priority=priority,
                message=format_operational_alert(
                    event.component, status, event.safe_message, observed_at, priority,
                    recovered=recovered, snapshot_id=event.snapshot_id,
                ),
                subject_ref_type="operational_event", subject_ref_id=str(event.id),
            )))
        return sent

    def notify_research(self, snapshot_id: int) -> int:
        if not self.settings.telegram_enabled:
            return 0
        sent = 0
        feature = FeatureRepository(self._sessions).get(snapshot_id)
        regime = RegimeRepository(self._sessions).get(snapshot_id)
        candidates = StrategyRepository(self._sessions).get(snapshot_id)
        risk = RiskRepository(self._sessions).get(snapshot_id)
        sent += self._notify_regime(snapshot_id, feature, regime)
        if candidates and candidates.get("eligible") and candidates.get("candidates"):
            top = candidates["candidates"][0]
            sent += int(self.service.notify(NotificationRequest(
                event_code=NotificationEventCode.STRATEGY_CANDIDATE_CREATED,
                dedupe_key=candidate_key(snapshot_id, top["candidate_id"]),
                priority=NotificationPriority.INFO, message=format_candidate(top),
                subject_ref_type="strategy_candidate", subject_ref_id=top["candidate_id"],
            )))
        sent += self._notify_risk(snapshot_id, risk, regime, candidates)
        sent += self._notify_shadow(snapshot_id)
        return sent

    def _notify_regime(
        self, snapshot_id: int, feature: dict[str, Any] | None,
        regime: dict[str, Any] | None,
    ) -> int:
        if not feature or not regime or regime.get("regime") == "NO_TRADE":
            return 0
        required = self.settings.risk_required_consecutive_directional_snapshots
        timestamp = datetime.fromisoformat(regime["timestamp"])
        row_limit = 2 if regime.get("regime") == "RANGE" else required + 1
        with self._sessions() as session:
            rows = session.scalars(select(MarketRegimeSnapshotRecord).join(
                MarketFeatureSnapshotRecord,
                MarketFeatureSnapshotRecord.id == MarketRegimeSnapshotRecord.feature_snapshot_id,
            ).where(MarketFeatureSnapshotRecord.timestamp <= timestamp).order_by(
                MarketFeatureSnapshotRecord.timestamp.desc(),
                MarketRegimeSnapshotRecord.id.desc(),
            ).limit(row_limit)).all()
        directions = [row.regime for row in rows]
        if regime["regime"] == "RANGE":
            if not self.settings.telegram_notify_range or (
                len(directions) > 1 and directions[1] == "RANGE"
            ):
                return 0
            return int(self.service.notify(NotificationRequest(
                event_code=NotificationEventCode.REGIME_CHANGED,
                dedupe_key=regime_key(snapshot_id, "RANGE"),
                priority=NotificationPriority.INFO,
                message=format_regime(regime, feature, changed=True),
                subject_ref_type="market_snapshot", subject_ref_id=str(snapshot_id),
            )))
        notify, changed = should_notify_directional(directions, regime["regime"], required)
        if not notify:
            return 0
        code = NotificationEventCode.REGIME_CHANGED if changed else NotificationEventCode.DIRECTIONAL_REGIME_CONFIRMED
        return int(self.service.notify(NotificationRequest(
            event_code=code, dedupe_key=regime_key(snapshot_id, regime["regime"]),
            priority=NotificationPriority.INFO,
            message=format_regime(regime, feature, changed=changed),
            subject_ref_type="market_snapshot", subject_ref_id=str(snapshot_id),
        )))

    def _notify_risk(
        self, snapshot_id: int, risk: dict[str, Any] | None,
        regime: dict[str, Any] | None, candidates: dict[str, Any] | None,
    ) -> int:
        if not risk:
            return 0
        by_id = {item["candidate_id"]: item for item in (candidates or {}).get("candidates", [])}
        with self._sessions() as session:
            records = session.scalars(select(RiskDecisionRecord).where(
                RiskDecisionRecord.market_snapshot_id == snapshot_id
            ).order_by(RiskDecisionRecord.id)).all()
        sent = 0
        for record in records:
            decision = dict(record.result_json)
            source = by_id.get(decision.get("candidate_reference"))
            if source:
                decision["source_candidate"] = source
            approved = decision.get("decision") == "APPROVED"
            important = is_important_risk_rejection(decision)
            if not approved and not important:
                continue
            sent += int(self.service.notify(NotificationRequest(
                event_code=(NotificationEventCode.RISK_APPROVED if approved
                            else NotificationEventCode.RISK_REJECTED_IMPORTANT),
                dedupe_key=risk_key(record.id),
                priority=NotificationPriority.IMPORTANT,
                message=format_risk(decision, regime, approved=approved),
                subject_ref_type="risk_decision", subject_ref_id=str(record.id),
            )))
        return sent

    def _notify_shadow(self, snapshot_id: int) -> int:
        with self._sessions() as session:
            entries = session.scalars(select(ShadowTradeRecord).where(
                ShadowTradeRecord.market_snapshot_id_entry == snapshot_id
            )).all()
            exits = session.scalars(select(ShadowTradeRecord).where(
                ShadowTradeRecord.exit_market_snapshot_id == snapshot_id,
                ShadowTradeRecord.status == "CLOSED",
            )).all()
        sent = 0
        for row in entries:
            sent += int(self.service.notify(NotificationRequest(
                event_code=NotificationEventCode.SHADOW_ENTRY_CREATED,
                dedupe_key=shadow_entry_key(row.id), priority=NotificationPriority.IMPORTANT,
                message=format_shadow_entry(row.result_json),
                subject_ref_type="shadow_trade", subject_ref_id=str(row.id),
            )))
        for row in exits:
            sent += int(self.service.notify(NotificationRequest(
                event_code=NotificationEventCode.SHADOW_EXITED,
                dedupe_key=shadow_exit_key(row.id), priority=NotificationPriority.IMPORTANT,
                message=format_shadow_exit(row.result_json),
                subject_ref_type="shadow_trade", subject_ref_id=str(row.id),
            )))
        return sent

    def send_daily_summary(self, day: date | None = None) -> bool:
        now = datetime.now(IST)
        target = day or now.date()
        if target.weekday() >= 5 or target in self.settings.configured_holidays:
            return False
        research = PipelineRepository(self._sessions).daily_summary(target)
        operations = get_daily_operations_summary(self._sessions, self.settings, target)
        return self.service.notify(NotificationRequest(
            event_code=NotificationEventCode.DAILY_RESEARCH_SUMMARY,
            dedupe_key=daily_key(target), priority=NotificationPriority.INFO,
            message=format_daily_summary(research, operations),
            subject_ref_type="trading_date", subject_ref_id=target.isoformat(),
        ))
