"""Five independent paper experiments with one read-only Kotak capture; ACTS runs separately."""
from __future__ import annotations

import argparse
from datetime import datetime, time
import json
from pathlib import Path
import sys
from types import FunctionType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore[import-untyped]  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]  # noqa: E402

from app.parallel_paper.capture import make_snapshot_builder  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.parallel_paper.report import report  # noqa: E402
from app.parallel_paper.service import ParallelPaperService  # noqa: E402
from app.scalper.research_capture import (  # noqa: E402
    IST, PARALLEL_PAPER_MODE, ResearchSnapshotCollector, ResearchSnapshotRepository,
)

logger = get_logger(__name__)


def paper_only_builder(builder: object) -> None:
    if not isinstance(builder, FunctionType) or vars(builder):
        raise RuntimeError("PARALLEL_PAPER_ORDER_CAPABILITY_FORBIDDEN")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=["PAPER"])
    parser.add_argument("--status", action="store_true", help="read-only per-strategy P&L and evidence")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_max_bytes, settings.log_backup_count)
    if not args.status and not settings.parallel_paper_enabled:
        logger.info("PARALLEL_PAPER_DISABLED")
        return 0
    if settings.database_url is None:
        logger.error("PARALLEL_PAPER_DATABASE_URL_REQUIRED")
        return 2
    engine = build_engine(settings.database_url.get_secret_value())
    try:
        sessions = build_session_factory(engine)
        if args.status:
            print(json.dumps(report(sessions, datetime.now(IST)), sort_keys=True, indent=2))
            return 0
        service = ParallelPaperService(sessions, settings)
        builder = make_snapshot_builder(settings, service.required_contracts, service.market.basket)
        paper_only_builder(builder)
        collector = ResearchSnapshotCollector(
            ResearchSnapshotRepository(engine, mode=PARALLEL_PAPER_MODE, capture_evidence={
                "volume_capture": "KOTAK_REST_ALL_LAST_VOLUME"}), builder,
            start=time(9, 15), end=settings.scalper_forced_exit_time,
            holidays=settings.configured_holidays)

        def run() -> None:
            # Catch up committed evidence independently of a subsequent broker outage.
            failed: set[str] = set()
            for snapshot_id in service.pending_snapshot_ids():
                statuses = service.process_snapshot(snapshot_id, skip_strategies=failed)
                failed.update(strategy for strategy, status in statuses.items() if status == "FAILED")
                if failed:
                    logger.error("PARALLEL_PAPER_RETRY_REQUIRED snapshot=%d", snapshot_id)
            try:
                captured = collector.capture_once(datetime.now(IST))
                if captured is not None:
                    statuses = service.process_snapshot(captured.snapshot_id, skip_strategies=failed)
                    logger.info("PARALLEL_PAPER_OBSERVATION snapshot=%d statuses=%s",
                                captured.snapshot_id, json.dumps(statuses, sort_keys=True))
            except Exception as exc:
                logger.error("PARALLEL_PAPER_CAPTURE_FAILED error_type=%s", type(exc).__name__)

        seconds = ",".join(str(second) for second in range(settings.parallel_paper_capture_offset_seconds, 60, 15))
        scheduler = BlockingScheduler(timezone=IST)
        scheduler.add_job(run, CronTrigger(day_of_week="mon-fri", hour="9-15", second=seconds, timezone=IST),
                          id="parallel_paper_capture", max_instances=1, coalesce=True, misfire_grace_time=14)
        logger.info("PARALLEL_PAPER_STARTED mode=PAPER interval_seconds=15 capture_seconds=%s bar_minutes=%d",
                    seconds, service.experiment.bar_minutes)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            return 0
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
