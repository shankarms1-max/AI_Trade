"""Explicit ACTS_V1 paper-only worker over Kotak market-data captures."""
from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
import argparse
import json
import sys
from types import FunctionType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from apscheduler.schedulers.blocking import (  # type: ignore[import-untyped]  # noqa: E402
    BlockingScheduler,
)
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]  # noqa: E402

from app.acts_paper.service import ActsPaperService  # noqa: E402
from app.acts_paper.alpha import LocalActsAlpha  # noqa: E402
from app.collector.factory import make_live_snapshot_builder  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.scalper.research_capture import (ACTS_PAPER_MODE, IST,  # noqa: E402
                                          ResearchSnapshotCollector,
                                          ResearchSnapshotRepository)

logger = get_logger(__name__)


def _paper_only_builder(builder: object) -> None:
    # The factory returns a plain market-data closure. Reject callable adapter
    # objects, which may carry executable broker capabilities.
    if not isinstance(builder, FunctionType) or vars(builder):
        raise RuntimeError("ACTS_PAPER_ORDER_CAPABILITY_FORBIDDEN")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["PAPER"], required=True)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir,
                      settings.log_max_bytes, settings.log_backup_count)
    if not args.status and not settings.acts_paper_enabled:
        logger.info("ACTS_PAPER_DISABLED")
        return 0
    if settings.database_url is None:
        logger.error("ACTS_PAPER_REFUSED reason=DATABASE_URL_REQUIRED")
        return 2
    engine = build_engine(settings.database_url.get_secret_value())
    try:
        sessions = build_session_factory(engine)
        service = ActsPaperService(sessions, settings)
        if args.status:
            print(json.dumps(service.status(datetime.now(IST)), indent=2, sort_keys=True))
            return 0
        local_alpha = LocalActsAlpha(sessions, settings)
        market_settings = settings.model_copy(update={
            "kotak_strike_range": settings.scalper_strike_range})
        builder = make_live_snapshot_builder(
            market_settings, required_contract_provider=service.required_contracts)
        _paper_only_builder(builder)
        market_observation = [None]

        def capture_market():
            market = builder()
            market_observation[0] = market
            return market

        collector = ResearchSnapshotCollector(
            ResearchSnapshotRepository(engine, mode=ACTS_PAPER_MODE), capture_market,
            start=time(9, 15),
            end=settings.scalper_forced_exit_time,
            holidays=settings.configured_holidays)

        def run() -> None:
            try:
                market_observation[0] = None
                captured = collector.capture_once(datetime.now(IST))
                if captured is not None:
                    if not captured.duplicate and market_observation[0] is not None:
                        try:
                            local_alpha.observe(market_observation[0])
                        except Exception as exc:
                            logger.error("ACTS_LOCAL_ALPHA_FAILED error_type=%s",
                                         type(exc).__name__)
                    service.process_snapshot(captured.snapshot_id)
            except Exception as exc:
                logger.error("ACTS_PAPER_CYCLE_FAILED error_type=%s", type(exc).__name__)

        scheduler = BlockingScheduler(timezone=IST)
        scheduler.add_job(
            run, CronTrigger(day_of_week="mon-fri", hour="9-15",
                             second="0,15,30,45", timezone=IST),
            id="acts_paper_capture", max_instances=1, coalesce=True,
            misfire_grace_time=14)
        logger.info("ACTS_PAPER_STARTED mode=PAPER capture_mode=%s interval_seconds=15",
                    ACTS_PAPER_MODE)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            return 0
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
