"""APScheduler process for the Phase 2 market-data recorder."""

from pathlib import Path
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from apscheduler.events import EVENT_JOB_MAX_INSTANCES  # noqa: E402
from apscheduler.schedulers.blocking import BlockingScheduler  # noqa: E402

from app.collector.factory import make_live_snapshot_builder  # noqa: E402
from app.collector.service import CollectorService, IST, TradingCalendar, collection_trigger  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.repositories import SnapshotRepository  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.pipeline.service import make_after_snapshot_callback  # noqa: E402
from app.observability.models import EventSeverity  # noqa: E402
from app.observability.repository import ObservabilityRepository  # noqa: E402
from app.notifications.models import (  # noqa: E402
    NotificationEventCode, NotificationPriority, NotificationRequest,
)
from app.notifications.router import NotificationRouter  # noqa: E402

logger = get_logger(__name__)


def main() -> int:
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, settings.log_dir, settings.log_max_bytes,
                      settings.log_backup_count)
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    repository = SnapshotRepository(sessions)
    operations = ObservabilityRepository(sessions)
    notifications = NotificationRouter(sessions, settings) if settings.telegram_enabled else None
    worker_id = settings.obs_collector_worker_id
    started_at = datetime.now(IST)
    recovery_pending = {"value": False}

    def heartbeat(status: str = "RUNNING") -> None:
        operations.upsert_heartbeat(worker_id, datetime.now(IST), status=status,
                                    started_at=started_at)

    def snapshot_observer(result) -> None:
        operations.upsert_heartbeat(
            worker_id, datetime.now(IST), started_at=started_at,
            snapshot_id=result.snapshot_id, snapshot_at=datetime.now(IST),
        )
        if recovery_pending["value"]:
            operations.record_event("COLLECTOR", EventSeverity.INFO, "COLLECTOR_RECOVERED",
                                    "Collector completed a successful run")
            recovery_pending["value"] = False

    def failure_observer(error: BaseException) -> None:
        recovery_pending["value"] = True
        operations.upsert_heartbeat(worker_id, datetime.now(IST), status="DEGRADED",
                                    started_at=started_at, error=error)
        operations.record_event(
            "COLLECTOR", EventSeverity.ERROR, "COLLECTOR_RUN_FAILED",
            "Market-data collection or persistence failed", error_type=type(error).__name__,
        )
    service = CollectorService(
        make_live_snapshot_builder(settings),
        repository,
        TradingCalendar(
            settings.collector_start_time,
            settings.collector_end_time,
            settings.configured_holidays,
        ),
        settings.collector_interval_minutes,
        make_after_snapshot_callback(sessions, settings, notifications)
        if settings.pipeline_after_snapshot else None,
        snapshot_observer,
        failure_observer,
    )
    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(
        service.run_scheduled,
        collection_trigger(settings.collector_interval_minutes),
        id="nifty_snapshot_collector",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    if notifications is not None:
        scheduler.add_job(
            notifications.route_operational_events, "interval",
            seconds=settings.obs_collector_heartbeat_seconds,
            id="telegram_operational_dispatch", max_instances=1, coalesce=True,
        )
        scheduler.add_job(
            notifications.send_daily_summary, "cron",
            hour=settings.telegram_daily_summary_time.hour,
            minute=settings.telegram_daily_summary_time.minute,
            id="telegram_daily_summary", max_instances=1, coalesce=True,
        )
    scheduler.add_listener(
        lambda event: logger.warning("COLLECTOR_RUN_SKIPPED_OVERLAP"),
        EVENT_JOB_MAX_INSTANCES,
    )
    scheduler.add_job(
        heartbeat, "interval", seconds=settings.obs_collector_heartbeat_seconds,
        id="collector_heartbeat", max_instances=1, coalesce=True,
    )
    heartbeat()
    operations.record_event("COLLECTOR", EventSeverity.INFO, "COLLECTOR_STARTED",
                            "Collector worker started", context=worker_id)
    if notifications is not None and settings.telegram_send_startup_message:
        try:
            notifications.service.notify(NotificationRequest(
                event_code=NotificationEventCode.SERVICE_STARTED,
                dedupe_key=f"SERVICE_STARTED:{started_at.isoformat()}",
                priority=NotificationPriority.INFO,
                message=("Nifty AI Research service started\n"
                         "Mode: RESEARCH / SHADOW\nLive execution: DISABLED"),
                subject_ref_type="worker", subject_ref_id=worker_id,
            ))
        except Exception as exc:
            logger.error("TELEGRAM_STARTUP_NOTIFICATION_FAILED error_type=%s",
                         type(exc).__name__)
    logger.info("COLLECTOR_STARTED interval_minutes=%d", settings.collector_interval_minutes)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        return 0
    finally:
        operations.upsert_heartbeat(worker_id, datetime.now(IST), status="STOPPED",
                                    started_at=started_at)
        operations.record_event("COLLECTOR", EventSeverity.INFO, "COLLECTOR_STOPPED",
                                "Collector worker stopped cleanly", context=worker_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
