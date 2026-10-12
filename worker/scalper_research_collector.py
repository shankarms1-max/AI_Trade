"""Explicit Phase 15 research-only market-data collector (no strategy worker)."""
from datetime import datetime, time
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from apscheduler.events import EVENT_JOB_MAX_INSTANCES  # type: ignore[import-untyped]  # noqa: E402
from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore[import-untyped]  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]  # noqa: E402

from app.collector.factory import make_live_snapshot_builder  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.session import build_engine  # noqa: E402
from app.scalper.research_capture import (INTERVAL_SECONDS, IST, MODE,  # noqa: E402
                                          ResearchSnapshotCollector,
                                          ResearchSnapshotRepository)

logger = get_logger(__name__)
RESEARCH_START_TIME = time(9, 15)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=[MODE],
                        help="explicitly acknowledge research-only capture")
    args = parser.parse_args(argv)
    assert args.mode == MODE

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir,
                      settings.log_max_bytes, settings.log_backup_count)
    if settings.database_url is None:
        logger.error("SCALPER_RESEARCH_REFUSED reason=DATABASE_URL_REQUIRED mode=%s", MODE)
        return 2

    engine = build_engine(settings.database_url.get_secret_value())
    market_settings = settings.model_copy(update={
        "kotak_strike_range": settings.scalper_strike_range})
    collector = ResearchSnapshotCollector(
        ResearchSnapshotRepository(engine),
        make_live_snapshot_builder(market_settings),
        start=RESEARCH_START_TIME,
        end=settings.scalper_forced_exit_time,
        holidays=settings.configured_holidays)

    def run() -> None:
        try:
            collector.capture_once(datetime.now(IST))
        except Exception:
            # The collector logs a safe error type and retries at the next bucket.
            return

    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(
        run, CronTrigger(day_of_week="mon-fri", hour="9-15",
                         second="0,15,30,45", timezone=IST),
        id="scalper_research_capture", max_instances=1, coalesce=True,
        misfire_grace_time=INTERVAL_SECONDS - 1)
    scheduler.add_listener(
        lambda _: logger.warning("SCALPER_RESEARCH_CAPTURE_SKIPPED "
                                 "reason=overlap mode=%s", MODE),
        EVENT_JOB_MAX_INSTANCES)
    logger.info("SCALPER_RESEARCH_STARTED mode=%s interval_seconds=%d "
                "start=%s final_bucket=%s", MODE, INTERVAL_SECONDS,
                RESEARCH_START_TIME, settings.scalper_forced_exit_time)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        return 0
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
