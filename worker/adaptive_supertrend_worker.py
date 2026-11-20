"""Separate, disabled-by-default AST_V1 PAPER worker. No live mode or order adapter."""

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
from app.adaptive_supertrend.config import AstPolicy  # noqa: E402
from app.adaptive_supertrend.journal import verify  # noqa: E402
from app.adaptive_supertrend.market import history  # noqa: E402
from app.adaptive_supertrend.report import report  # noqa: E402
from app.adaptive_supertrend.service import AstPaperService, freeze  # noqa: E402
from app.collector.factory import make_live_snapshot_builder  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import get_logger  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.scalper.research_capture import (  # noqa: E402
    AST_PAPER_MODE,
    IST,
    ResearchSnapshotCollector,
    ResearchSnapshotRepository,
)

logger = get_logger(__name__)


def paper_only_builder(builder: object) -> None:
    if not isinstance(builder, FunctionType) or vars(builder):
        raise RuntimeError("AST_ORDER_CAPABILITY_FORBIDDEN")


def seconds(offset: int) -> str:
    if not 0 <= offset < 15:
        raise ValueError("AST_CAPTURE_OFFSET_INVALID")
    return ",".join(str(second) for second in range(offset, 60, 15))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=["PAPER"])
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--verify-journal", action="store_true", help="read-only streaming full audit")
    args = parser.parse_args(argv)
    settings = get_settings()
    if not (args.status or args.verify_journal) and not settings.adaptive_supertrend_enabled:
        logger.info("AST_DISABLED")
        return 0
    if settings.database_url is None:
        parser.error("DATABASE_URL is required")
    engine = build_engine(settings.database_url.get_secret_value())
    try:
        sessions = build_session_factory(engine)
        policy = AstPolicy.model_validate_json(settings.ast_config_json)
        if args.verify_journal:
            verify(sessions, full=True)
        if args.status or args.verify_journal:
            status = report(sessions, datetime.now(IST), policy)
            status["worker_enabled"] = settings.adaptive_supertrend_enabled
            print(json.dumps(status, sort_keys=True, indent=2))
            return 0
        service = AstPaperService(sessions, settings)
        market_settings = settings.model_copy(update={"kotak_strike_range": settings.scalper_strike_range})
        builder = make_live_snapshot_builder(market_settings, required_contract_provider=service.required_contracts)
        paper_only_builder(builder)
        collector = ResearchSnapshotCollector(
            ResearchSnapshotRepository(engine, mode=AST_PAPER_MODE),
            builder,
            start=time(9, 15),
            end=policy.execution.safety.forced_exit_time if policy.execution else time(15, 30),
            holidays=settings.configured_holidays,
        )

        def select_daily() -> None:
            at = datetime.now(IST)
            if (
                at.time().replace(tzinfo=None) >= time(9, 15)
                or at.weekday() >= 5
                or at.date() in settings.configured_holidays
                or policy.missing
            ):
                return
            try:
                assert policy.optimizer is not None
                bars = history(
                    sessions,
                    at,
                    policy.optimizer.lookback_sessions,
                    policy.max_sequence_gap_seconds,
                )
                freeze(sessions, bars, at.date(), at, policy, clock=lambda: datetime.now(IST))
            except Exception as exc:
                logger.error("AST_PARAMETER_FREEZE_BLOCKED error_type=%s reason=%s", type(exc).__name__, str(exc))

        def run() -> None:
            try:
                # Catch up before new network work. A failed historical observation blocks later processing.
                for snapshot_id in service.pending_snapshot_ids():
                    service.process_snapshot(snapshot_id)
                if service.pending_snapshot_ids():
                    return
                captured = collector.capture_once(datetime.now(IST))
                if captured:
                    service.process_snapshot(captured.snapshot_id)
            except Exception as exc:
                logger.error("AST_CYCLE_FAILED error_type=%s", type(exc).__name__)

        select_daily()
        scheduler = BlockingScheduler(timezone=IST)
        scheduler.add_job(
            select_daily,
            CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=IST),
            id="ast_parameter_freeze",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            run,
            CronTrigger(
                day_of_week="mon-fri", hour="9-15", second=seconds(settings.ast_capture_offset_seconds), timezone=IST
            ),
            id="ast_paper_capture",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=14,
        )
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            return 0
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
