"""Dedicated opt-in MCX ACTS research and paper worker. Read-only broker access."""
from datetime import datetime
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from apscheduler.schedulers.blocking import BlockingScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from app.alpha.clock import IST  # noqa: E402
from app.commodity_acts.broker import CommodityDataError, assert_market_only, make_adapter  # noqa: E402
from app.commodity_acts.config import CommoditySettings, INSTRUMENTS  # noqa: E402
from app.commodity_acts.read import summary  # noqa: E402
from app.commodity_acts.paper_read import summary as paper_summary  # noqa: E402
from app.commodity_acts.paper_coordinator import CommodityPaperCoordinator as CommodityResearch  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402

logger = get_logger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["PAPER"], required=True)
    parser.add_argument("--instrument", choices=INSTRUMENTS, required=True)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    config = CommoditySettings()
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_max_bytes, settings.log_backup_count)
    if not args.status and not config.enabled(args.instrument):
        logger.info("COMMODITY_RESEARCH_DISABLED instrument=%s", args.instrument)
        return 0
    spec = None
    if not args.status:
        try:
            spec = config.specification(args.instrument)
        except ValueError:
            logger.error("COMMODITY_RESEARCH_REFUSED instrument=%s reason=VERIFIED_SESSION_SETTINGS_REQUIRED", args.instrument)
            return 2
    if settings.database_url is None:
        logger.error("COMMODITY_RESEARCH_REFUSED reason=DATABASE_URL_REQUIRED")
        return 2
    engine = build_engine(settings.database_url.get_secret_value())
    try:
        sessions = build_session_factory(engine)
        if args.status:
            result = summary(sessions, config, datetime.now(IST))
            result["instruments"] = [row for row in result["instruments"] if row["instrument_id"] == args.instrument]
            paper = paper_summary(sessions, config, datetime.now(IST))
            paper["instruments"] = [row for row in paper["instruments"] if row["instrument_id"] == args.instrument]
            result["automated_paper"] = paper
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        assert spec is not None
        service = CommodityResearch(sessions, settings, config, spec)
        market = make_adapter(settings, strikes_each_side=config.strikes_each_side,
                              max_quote_age_seconds=config.max_quote_age_seconds)
        assert_market_only(market)

        def run():
            now = datetime.now(IST)
            try:
                result = service.capture_once(market, now)
                if result:
                    logger.info("COMMODITY_SHADOW_DECISION instrument=%s snapshot_id=%d status=%s", args.instrument, *result)
                return True
            except Exception as exc:
                reason = exc.reason if isinstance(exc, CommodityDataError) else "COMMODITY_CAPTURE_OR_RESEARCH_FAILED"
                try:
                    service.record_failure(reason, now)
                except Exception:
                    logger.error("COMMODITY_FAILURE_JOURNAL_UNAVAILABLE instrument=%s", args.instrument)
                logger.warning("COMMODITY_RESEARCH_UNAVAILABLE instrument=%s reason=%s", args.instrument, reason)
                return False
        if args.once:
            return 0 if run() else 2
        scheduler = BlockingScheduler(timezone=IST)
        # Offsets reduce simultaneous traffic; instrument-keyed persistence, not
        # offsets, provides isolation/idempotency. Session gate is authoritative.
        seconds = "3,18,33,48" if args.instrument == "CRUDEOILM" else "7,22,37,52"
        scheduler.add_job(run, CronTrigger(day_of_week="mon-fri", second=seconds, timezone=IST),
                          id=f"commodity_acts_{args.instrument}", max_instances=1, coalesce=True, misfire_grace_time=14)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            return 0
        return 0
    except Exception:
        logger.error("COMMODITY_RESEARCH_REFUSED instrument=%s reason=INITIALIZATION_FAILED", args.instrument)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
