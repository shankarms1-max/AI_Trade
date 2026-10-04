from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    AIResearchSnapshotRecord, CollectorHeartbeatRecord, CollectorRunRecord,
    MarketFeatureSnapshotRecord, MarketSnapshotRecord, OperationalEventRecord,
    OptionContractSnapshotRecord, PipelineRunRecord, ShadowTradeMarkRecord,
    ShadowTradeRecord,
)
from app.observability.models import EventSeverity, safe_exception


class ObservabilityRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def upsert_heartbeat(
        self, worker_id: str, now: datetime, *, status: str = "RUNNING",
        started_at: datetime | None = None, snapshot_id: int | None = None,
        snapshot_at: datetime | None = None, error: BaseException | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.scalar(select(CollectorHeartbeatRecord).where(
                CollectorHeartbeatRecord.worker_id == worker_id
            ))
            if row is None:
                row = CollectorHeartbeatRecord(
                    worker_id=worker_id, started_at=started_at or now,
                    last_heartbeat_at=now, status=status,
                )
                session.add(row)
            row.last_heartbeat_at = now
            row.status = status
            if snapshot_id is not None:
                row.last_snapshot_id = snapshot_id
                row.last_snapshot_at = snapshot_at or now
            if error is None:
                row.safe_error_type = None
                row.safe_error_message = None
            else:
                row.safe_error_type, row.safe_error_message = safe_exception(error)

    def latest_heartbeat(self) -> dict[str, Any] | None:
        with self._sessions() as session:
            row = session.scalar(select(CollectorHeartbeatRecord).order_by(
                CollectorHeartbeatRecord.last_heartbeat_at.desc()
            ).limit(1))
            return None if row is None else self._row(row)

    def collector_runs(self, start: datetime, end: datetime, limit: int = 1000) -> list[dict[str, Any]]:
        with self._sessions() as session:
            rows = session.scalars(select(CollectorRunRecord).where(
                CollectorRunRecord.started_at >= start, CollectorRunRecord.started_at < end
            ).order_by(CollectorRunRecord.started_at.desc()).limit(limit)).all()
            return [self._row(row) for row in rows]

    def pipeline_runs(self, start: datetime, end: datetime, limit: int = 1000) -> list[dict[str, Any]]:
        with self._sessions() as session:
            rows = session.scalars(select(PipelineRunRecord).where(
                PipelineRunRecord.started_at >= start, PipelineRunRecord.started_at < end
            ).order_by(PipelineRunRecord.started_at.desc()).limit(limit)).all()
            return [self._row(row) for row in rows]

    def latest_pipeline(self) -> dict[str, Any] | None:
        with self._sessions() as session:
            row = session.scalar(select(PipelineRunRecord).order_by(
                PipelineRunRecord.started_at.desc(), PipelineRunRecord.id.desc()
            ).limit(1))
            return None if row is None else self._row(row)

    def latest_snapshot_quality(self) -> dict[str, Any] | None:
        with self._sessions() as session:
            snapshot = session.scalar(select(MarketSnapshotRecord).order_by(
                MarketSnapshotRecord.timestamp_ist.desc(), MarketSnapshotRecord.id.desc()
            ).limit(1))
            if snapshot is None:
                return None
            contracts = session.scalars(select(OptionContractSnapshotRecord).where(
                OptionContractSnapshotRecord.market_snapshot_id == snapshot.id
            )).all()
            feature = session.scalar(select(MarketFeatureSnapshotRecord).where(
                MarketFeatureSnapshotRecord.market_snapshot_id == snapshot.id
            ).order_by(MarketFeatureSnapshotRecord.id.desc()).limit(1))
            total = len(contracts)
            nonzero_volume = sum(bool(row.volume) for row in contracts)
            nonzero_change = sum(bool(row.change_in_open_interest) for row in contracts)
            oi_diff = sum(
                row.open_interest is not None and row.previous_open_interest is not None
                and row.open_interest != row.previous_open_interest for row in contracts
            )
            mismatch = sum(
                row.open_interest is not None and row.previous_open_interest is not None
                and row.change_in_open_interest is not None
                and row.open_interest - row.previous_open_interest != row.change_in_open_interest
                for row in contracts
            )
            return {
                "snapshot_id": snapshot.id, "timestamp": snapshot.timestamp_ist,
                "source": snapshot.source, "spot_available": snapshot.nifty_spot is not None,
                "future_available": snapshot.nifty_future is not None,
                "vix_available": snapshot.india_vix is not None,
                "lot_size_available": snapshot.lot_size is not None,
                "option_contract_count": total,
                "nonzero_volume_count": nonzero_volume,
                "nonzero_oi_change_count": nonzero_change,
                "oi_current_prev_difference_count": oi_diff,
                "oi_mismatch_count": mismatch,
                "intraday_oi_usable": bool(
                    feature and feature.feature_json.get("data_quality", {}).get("intraday_oi_usable")
                ),
                "bid_ask_coverage_pct": self._coverage(contracts, lambda row: row.bid is not None and row.ask is not None),
                "volume_coverage_pct": self._coverage(contracts, lambda row: row.volume is not None),
                "oi_change_coverage_pct": self._coverage(contracts, lambda row: row.change_in_open_interest is not None),
            }

    def snapshot_daily_quality(self, start: datetime, end: datetime) -> dict[str, Any]:
        with self._sessions() as session:
            rows = session.scalars(select(MarketSnapshotRecord).where(
                MarketSnapshotRecord.timestamp_ist >= start, MarketSnapshotRecord.timestamp_ist < end
            )).all()
            ids = [row.id for row in rows] or [-1]
            features = session.scalars(select(MarketFeatureSnapshotRecord).where(
                MarketFeatureSnapshotRecord.market_snapshot_id.in_(ids)
            )).all()
            usable_ids = {row.market_snapshot_id for row in features if
                          row.feature_json.get("data_quality", {}).get("intraday_oi_usable")}
            return {
                "count": len(rows),
                "vix_available": sum(row.india_vix is not None for row in rows),
                "lot_size_available": sum(row.lot_size is not None for row in rows),
                "usable_oi": len(usable_ids),
            }

    def shadow_metrics(self, start: datetime, end: datetime) -> dict[str, Any]:
        with self._sessions() as session:
            open_rows = session.scalars(select(ShadowTradeRecord).where(
                ShadowTradeRecord.status == "OPEN"
            )).all()
            entries = session.scalar(select(func.count(ShadowTradeRecord.id)).where(
                ShadowTradeRecord.entry_timestamp >= start, ShadowTradeRecord.entry_timestamp < end
            )) or 0
            exits = session.scalar(select(func.count(ShadowTradeRecord.id)).where(
                ShadowTradeRecord.exit_timestamp >= start, ShadowTradeRecord.exit_timestamp < end
            )) or 0
            invalid = session.scalar(select(func.count(ShadowTradeRecord.id)).where(
                ShadowTradeRecord.status == "INVALID",
                ShadowTradeRecord.updated_at >= start, ShadowTradeRecord.updated_at < end
            )) or 0
            mark = session.scalar(select(ShadowTradeMarkRecord).order_by(
                ShadowTradeMarkRecord.timestamp.desc()
            ).limit(1))
            updated = session.scalar(select(func.max(ShadowTradeRecord.updated_at)))
            event_counts = self._event_code_counts(session, start, end)
            return {
                "open_shadow_trades": len(open_rows), "shadow_entries_today": entries,
                "shadow_exits_today": exits,
                "latest_shadow_mark_at": None if mark is None else mark.timestamp,
                "latest_shadow_trade_update_at": updated,
                "unavailable_marks_today": event_counts.get("SHADOW_MARK_UNAVAILABLE", 0),
                "payout_anomalies_today": event_counts.get("SHADOW_PAYOUT_ANOMALY", 0),
                "invalid_shadow_trades_today": invalid,
            }

    def ai_metrics(self, start: datetime, end: datetime) -> dict[str, Any]:
        with self._sessions() as session:
            rows = session.scalars(select(AIResearchSnapshotRecord).where(
                AIResearchSnapshotRecord.created_at >= start,
                AIResearchSnapshotRecord.created_at < end,
            ).order_by(AIResearchSnapshotRecord.created_at.desc())).all()
            latest_success = next((row for row in rows if row.status == "SUCCESS"), None)
            latest_failure = next((row for row in rows if row.status == "FAILED"), None)
            return {
                "calls_today": len(rows),
                "cost_today_usd": round(sum(float(row.estimated_cost_usd or 0) for row in rows), 8),
                "latest_status": None if not rows else rows[0].status,
                "last_success_at": None if latest_success is None else latest_success.created_at,
                "last_failure_at": None if latest_failure is None else latest_failure.created_at,
                "last_latency_ms": None if not rows else rows[0].latency_ms,
            }

    def record_event(
        self, component: str, severity: EventSeverity | str, event_code: str,
        safe_message: str, *, error_type: str | None = None,
        snapshot_id: int | None = None, pipeline_run_id: int | None = None,
        context: str | None = None, metadata: dict[str, Any] | None = None,
    ) -> int:
        clean = safe_exception(RuntimeError(safe_message))[1]
        with self._sessions.begin() as session:
            row = OperationalEventRecord(
                component=component.upper()[:64], severity=getattr(severity, "value", severity),
                event_code=event_code.upper()[:120], error_type=error_type,
                safe_message=clean, snapshot_id=snapshot_id,
                pipeline_run_id=pipeline_run_id, context=context,
                metadata_json=self._safe_metadata(metadata or {}),
            )
            session.add(row)
            session.flush()
            return row.id

    def record_transition(self, component: str, old: str | None, new: str) -> int | None:
        if old == new or not (old == "UNHEALTHY" and new == "HEALTHY"):
            return None
        code = f"{component.upper()}_RECOVERED"
        with self._sessions() as session:
            latest = session.scalar(select(OperationalEventRecord).where(
                OperationalEventRecord.component == component.upper()
            ).order_by(OperationalEventRecord.created_at.desc(),
                       OperationalEventRecord.id.desc()).limit(1))
            if latest is not None and latest.event_code == code:
                return None
        return self.record_event(component, EventSeverity.INFO, code, f"{component} recovered")

    def record_health_transition(
        self, component: str, status: str, failure_code: str, recovery_code: str,
        failure_message: str,
    ) -> int | None:
        """Persist only a change into or out of a failed/degraded health state."""
        component = component.upper()
        with self._sessions() as session:
            latest = session.scalar(select(OperationalEventRecord).where(
                OperationalEventRecord.component == component,
                OperationalEventRecord.event_code.in_([failure_code, recovery_code]),
            ).order_by(OperationalEventRecord.created_at.desc(),
                       OperationalEventRecord.id.desc()).limit(1))
        if status in {"DEGRADED", "UNHEALTHY"}:
            if latest is not None and latest.event_code == failure_code:
                return None
            return self.record_event(component, EventSeverity.WARN, failure_code, failure_message,
                                     metadata={"health_status": status})
        if status == "HEALTHY" and latest is not None and latest.event_code == failure_code:
            return self.record_event(component, EventSeverity.INFO, recovery_code,
                                     f"{component.replace('_', ' ').title()} recovered",
                                     metadata={"health_status": status})
        return None

    def events(
        self, limit: int = 100, *, component: str | None = None,
        severity: str | None = None, event_code: str | None = None,
        start: datetime | None = None, end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        query = select(OperationalEventRecord)
        if component:
            query = query.where(OperationalEventRecord.component == component.upper())
        if severity:
            query = query.where(OperationalEventRecord.severity == severity.upper())
        if event_code:
            query = query.where(OperationalEventRecord.event_code == event_code.upper())
        if start:
            query = query.where(OperationalEventRecord.created_at >= start)
        if end:
            query = query.where(OperationalEventRecord.created_at < end)
        with self._sessions() as session:
            rows = session.scalars(query.order_by(
                OperationalEventRecord.created_at.desc(), OperationalEventRecord.id.desc()
            ).limit(limit)).all()
            return [self._row(row) for row in rows]

    @staticmethod
    def _coverage(rows: list[Any], predicate) -> float | None:
        return None if not rows else round(100 * sum(predicate(row) for row in rows) / len(rows), 2)

    @classmethod
    def _safe_metadata(cls, value: Any, key: str = "") -> Any:
        sensitive = ("key", "token", "secret", "password", "authorization", "mpin", "totp", "sid", "rid", "ucc", "mobile")
        if any(item in key.lower() for item in sensitive):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k)[:80]: cls._safe_metadata(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._safe_metadata(item) for item in value[:50]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value if not isinstance(value, str) else value[:500]
        return type(value).__name__

    @staticmethod
    def _event_code_counts(session: Session, start: datetime, end: datetime) -> dict[str, int]:
        rows = session.execute(select(
            OperationalEventRecord.event_code, func.count(OperationalEventRecord.id)
        ).where(OperationalEventRecord.created_at >= start,
                OperationalEventRecord.created_at < end).group_by(
                    OperationalEventRecord.event_code)).all()
        return {code: count for code, count in rows}

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}
