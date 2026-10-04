from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from app.observability.ai_health import ai_health
from app.observability.collector_health import collector_health
from app.observability.database_health import check_database
from app.observability.health import aggregate_status, expected_snapshots, market_session_state
from app.observability.market_data_health import market_data_health
from app.observability.metrics import percentage
from app.observability.models import HealthStatus
from app.observability.pipeline_health import pipeline_health
from app.observability.repository import ObservabilityRepository
from app.observability.shadow_health import shadow_health
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService

IST = ZoneInfo("Asia/Kolkata")


class ObservabilityService:
    def __init__(self, session_factory: sessionmaker[Session], settings: Any) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._repo = ObservabilityRepository(session_factory)

    @staticmethod
    def _bounds(day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, time.min, IST)
        return start, start + timedelta(days=1)

    def detailed_health(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(IST)).astimezone(IST)
        session_state = market_session_state(
            current, self._settings.collector_start_time, self._settings.collector_end_time,
            self._settings.configured_holidays,
        )
        database = check_database(self._sessions)
        if database["status"] != HealthStatus.HEALTHY.value:
            telegram_status = "DISABLED" if not self._settings.telegram_enabled else "DEGRADED"
            components = {
                "database": database["status"], "collector_heartbeat": HealthStatus.UNKNOWN.value,
                "data_freshness": HealthStatus.UNKNOWN.value, "market_data": HealthStatus.UNKNOWN.value,
                "pipeline": HealthStatus.UNKNOWN.value, "shadow": HealthStatus.UNKNOWN.value,
                "ai": "DISABLED" if not self._settings.openai_api_key else "CONFIGURED_NOT_TESTED",
                "notifications": telegram_status,
                "alpha": "DISABLED" if not self._settings.alpha_engine_enabled else "UNKNOWN",
            }
            return {
                "overall_status": HealthStatus.UNHEALTHY.value,
                "current_time_ist": current, "market_session_state": session_state.value,
                "components": components, "database": database,
                "collector": None, "market_data": None, "pipeline": None,
                "shadow": None, "ai": {"status": components["ai"]},
                "alpha": {"status": components["alpha"]},
                "notifications": {"telegram": {
                    "status": telegram_status,
                    "enabled": self._settings.telegram_enabled,
                    "configured": bool(self._settings.telegram_bot_token and self._settings.telegram_chat_id),
                    "last_sent_at": None, "last_failed_at": None, "failed_today": 0,
                }},
            }

        start, end = self._bounds(current.date())
        quality = self._repo.latest_snapshot_quality()
        collector = collector_health(
            current, session_state, self._repo.latest_heartbeat(),
            self._repo.collector_runs(start, end), quality,
            self._settings.obs_collector_heartbeat_seconds,
            self._settings.obs_data_fresh_healthy_seconds,
            self._settings.obs_data_fresh_degraded_seconds,
        )
        expected = expected_snapshots(
            current, self._settings.collector_start_time, self._settings.collector_end_time,
            self._settings.obs_expected_snapshot_interval_seconds,
            self._settings.configured_holidays,
        )
        daily_quality = self._repo.snapshot_daily_quality(start, end)
        collector["expected_snapshots_so_far_today"] = expected
        collector["actual_snapshots_today"] = daily_quality["count"]
        collector["missing_snapshot_estimate"] = max(0, expected - daily_quality["count"])
        market = market_data_health(quality, session_state)
        pipeline_rows = self._repo.pipeline_runs(start, end)
        pipeline = pipeline_health(
            self._repo.latest_pipeline(), pipeline_rows, session_state,
            self._settings.obs_expected_snapshot_interval_seconds * 1000,
            self._settings.obs_pipeline_degraded_fraction,
        )
        shadow = shadow_health(
            current, session_state, self._repo.shadow_metrics(start, end),
            self._settings.obs_expected_snapshot_interval_seconds * 2,
        )
        ai = ai_health(
            bool(self._settings.openai_api_key and self._settings.ai_research_model),
            self._settings.ai_research_model, self._repo.ai_metrics(start, end),
        )
        telegram = NotificationService(
            NotificationRepository(self._sessions), self._settings
        ).health(current)
        latest_alpha = self._repo.latest_alpha()
        alpha_status = (
            "DISABLED" if not self._settings.alpha_engine_enabled
            else "WARMING_UP" if latest_alpha is None
            else latest_alpha.get("status", "UNKNOWN")
        )
        components = {
            "database": database["status"],
            "collector_heartbeat": collector["heartbeat"]["status"],
            "data_freshness": collector["data_freshness"]["status"],
            "market_data": market["status"], "pipeline": pipeline["status"],
            "shadow": shadow["status"], "ai": ai["status"],
            "notifications": telegram["status"],
            "alpha": alpha_status,
        }
        self._repo.record_health_transition(
            "COLLECTOR", collector["heartbeat"]["status"],
            "COLLECTOR_HEARTBEAT_MISSED", "COLLECTOR_RECOVERED",
            "Collector heartbeat is stale",
        )
        self._repo.record_health_transition(
            "DATA_FRESHNESS", collector["data_freshness"]["status"],
            "DATA_FRESHNESS_STALE", "DATA_FRESHNESS_RECOVERED",
            "Latest persisted market snapshot is stale",
        )
        self._repo.record_health_transition(
            "MARKET_DATA", market["status"],
            "MARKET_DATA_DEGRADED", "MARKET_DATA_RECOVERED",
            "Latest market-data snapshot has degraded required fields",
        )
        return {
            "overall_status": aggregate_status(components, session_state).value,
            "current_time_ist": current, "market_session_state": session_state.value,
            "components": components, "database": database, "collector": collector,
            "market_data": market, "pipeline": pipeline, "shadow": shadow, "ai": ai,
            "alpha": {"status": alpha_status, **(latest_alpha or {})},
            "notifications": {"telegram": telegram},
        }

    def compact_health(self, now: datetime | None = None) -> dict[str, Any]:
        detail = self.detailed_health(now)
        return {
            "status": detail["overall_status"],
            "current_time_ist": detail["current_time_ist"],
            "market_session_state": detail["market_session_state"],
            "components": detail["components"],
        }

    def metrics(self, now: datetime | None = None) -> dict[str, Any]:
        detail = self.detailed_health(now)
        collector = detail.get("collector") or {}
        pipeline = detail.get("pipeline") or {}
        shadow = detail.get("shadow") or {}
        ai = detail.get("ai") or {}
        expected = collector.get("expected_snapshots_so_far_today", 0)
        actual = collector.get("actual_snapshots_today", 0)
        return {
            "date": detail["current_time_ist"].date(),
            "snapshots_expected": expected, "snapshots_actual": actual,
            "snapshot_success_rate": percentage(actual, expected),
            "collector_failures": collector.get("failed_today", 0),
            "pipeline_runs": pipeline.get("runs_today", 0),
            "pipeline_success_rate": pipeline.get("pipeline_success_rate_today"),
            "avg_pipeline_ms": pipeline.get("average_total_pipeline_ms"),
            "p95_pipeline_ms": pipeline.get("p95_total_pipeline_ms"),
            "db_latency_ms": detail["database"].get("latency_ms"),
            "shadow_entries": shadow.get("shadow_entries_today", 0),
            "shadow_exits": shadow.get("shadow_exits_today", 0),
            "ai_calls": ai.get("calls_today", 0), "ai_cost": ai.get("cost_today_usd", 0),
        }

    def daily_operations_summary(self, day: date) -> dict[str, Any]:
        start, end = self._bounds(day)
        collector_rows = self._repo.collector_runs(start, end)
        pipeline_rows = self._repo.pipeline_runs(start, end)
        quality = self._repo.snapshot_daily_quality(start, end)
        shadow = self._repo.shadow_metrics(start, end)
        ai = self._repo.ai_metrics(start, end)
        events = self._repo.events(5000, start=start, end=end)
        expected = expected_snapshots(
            end - timedelta(microseconds=1), self._settings.collector_start_time,
            self._settings.collector_end_time,
            self._settings.obs_expected_snapshot_interval_seconds,
            self._settings.configured_holidays, day,
        )
        durations = [row.get("stage_timings_ms", {}).get("total_pipeline") for row in pipeline_rows]
        durations = [float(value) for value in durations if value is not None]
        from app.observability.metrics import average, percentile_95
        return {
            "date": day, "market_session_duration_minutes": round(
                (datetime.combine(day, self._settings.collector_end_time) -
                 datetime.combine(day, self._settings.collector_start_time)).total_seconds() / 60, 2
            ) if expected else 0,
            "expected_snapshots": expected, "actual_snapshots": quality["count"],
            "collector": {
                "success": sum(row["status"] == "SUCCESS" for row in collector_rows),
                "failure": sum(row["status"] == "FAILED" for row in collector_rows),
            },
            "data_quality": {
                "usable_oi_pct": percentage(quality["usable_oi"], quality["count"]),
                "vix_availability_pct": percentage(quality["vix_available"], quality["count"]),
                "lot_size_availability_pct": percentage(quality["lot_size_available"], quality["count"]),
            },
            "pipeline": {
                "success": sum(row["status"] == "SUCCESS" for row in pipeline_rows),
                "partial": sum(row["status"] == "PARTIAL" for row in pipeline_rows),
                "failure": sum(row["status"] == "FAILED" for row in pipeline_rows),
                "average_duration_ms": average(durations), "p95_duration_ms": percentile_95(durations),
            },
            "shadow": shadow,
            "ai": {"calls": ai["calls_today"], "cost_usd": ai["cost_today_usd"]},
            "operational_events": {
                severity: sum(row["severity"] == severity for row in events)
                for severity in ("WARN", "ERROR", "CRITICAL")
            },
        }


def get_daily_operations_summary(
    session_factory: sessionmaker[Session], settings: Any, day: date
) -> dict[str, Any]:
    return ObservabilityService(session_factory, settings).daily_operations_summary(day)
