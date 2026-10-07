"""Dedicated Phase 15 worker; independent from the three-minute collector."""
from pathlib import Path
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from apscheduler.events import EVENT_JOB_MAX_INSTANCES  # noqa: E402
from apscheduler.schedulers.blocking import BlockingScheduler  # noqa: E402

from app.collector.factory import make_live_snapshot_builder  # noqa: E402
from app.collector.service import IST, TradingCalendar  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.scalper.service import ScalperService  # noqa: E402

logger = get_logger(__name__)


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir,
                      settings.log_max_bytes, settings.log_backup_count)
    if not settings.scalper_enabled:
        logger.info("SCALPER_DISABLED")
        return 0
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    sessions = build_session_factory(build_engine(
        settings.database_url.get_secret_value()))
    broker_settings = settings.model_copy(update={
        "kotak_strike_range": settings.scalper_strike_range})
    service = ScalperService(sessions, settings,
                             make_live_snapshot_builder(broker_settings))
    calendar = TradingCalendar(settings.scalper_start_time,
                               settings.scalper_forced_exit_time,
                               settings.configured_holidays)

    def run() -> None:
        if calendar.eligibility(datetime.now(IST)) != "OPEN":
            return
        try:
            service.capture_once()
        except Exception:
            # Safe details are emitted by the service; keep scheduling future captures.
            return

    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(run, "interval", seconds=settings.scalper_interval_seconds,
                      id="nifty_scalper_capture", max_instances=1, coalesce=True,
                      misfire_grace_time=settings.scalper_interval_seconds)
    scheduler.add_listener(
        lambda _: logger.warning("SCALPER_ERROR reason=overlap"),
        EVENT_JOB_MAX_INSTANCES)
    logger.info("SCALPER_WORKER_STARTED interval_seconds=%d mode=PAPER",
                settings.scalper_interval_seconds)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
