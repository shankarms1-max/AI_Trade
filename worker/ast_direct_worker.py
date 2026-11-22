"""Dedicated AST_FUTURES_V1_DIRECT PAPER worker: fixed 22/3.9, one NIFTY futures lot, no order capability."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from types import FunctionType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore[import-untyped]  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]  # noqa: E402
from app.adaptive_supertrend.capture import capture_once, make_reader  # noqa: E402
from app.adaptive_supertrend.direct import CAPTURE_MODE, VERSION  # noqa: E402
from app.adaptive_supertrend.direct.policy import DirectPolicy  # noqa: E402
from app.adaptive_supertrend.direct.report import read_heartbeat, report  # noqa: E402
from app.adaptive_supertrend.direct.service import JOURNAL, DirectPaperService  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.scalper.repository import IST  # noqa: E402

logger = get_logger(__name__)


def paper_only_builder(builder: object) -> None:
    if not isinstance(builder, FunctionType) or vars(builder):
        raise RuntimeError("AST_ORDER_CAPABILITY_FORBIDDEN")


def seconds(offset: int) -> str:
    if not 0 <= offset < 15:
        raise ValueError("AST_CAPTURE_OFFSET_INVALID")
    return ",".join(str(second) for second in range(offset, 60, 15))


def heartbeat_path(settings: Settings) -> Path:
    if settings.ast_direct_heartbeat_file:
        return Path(settings.ast_direct_heartbeat_file)
    return Path(os.getenv("LOG_DIR", settings.log_dir)) / "ast_direct_worker.heartbeat.json"


def kill_switch(settings: Settings) -> bool:
    """Operational safety control. Blocks entries and safety-exits an open paper position."""
    path = settings.ast_direct_kill_switch_file
    return settings.ast_direct_kill_switch or bool(path and Path(path).exists())


def write_heartbeat(path: Path, **fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"at": datetime.now(IST).isoformat(), "pid": os.getpid(), **fields}))
    temporary.replace(path)


class BlockerLog:
    """Logs a decision only when its status, blockers or position change."""

    def __init__(self) -> None:
        self.last: tuple | None = None

    def __call__(self, decision: dict) -> None:
        position = decision.get("position") or {}
        key = (decision.get("status"), tuple(decision.get("blockers") or ()), decision.get("fill_blocker"),
               position.get("state"))
        if key != self.last:
            self.last = key
            logger.info(
                "AST_DIRECT_STATE status=%s blockers=%s fill_blocker=%s position=%s at=%s",
                key[0], ",".join(key[1]) or "-", key[2] or "-", key[3] or "FLAT", decision.get("timestamp"),
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=["PAPER"])
    parser.add_argument("--status", action="store_true", help="read-only status")
    parser.add_argument("--health", action="store_true", help="heartbeat freshness only; no database")
    parser.add_argument("--verify-journal", action="store_true", help="read-only streaming full audit")
    parser.add_argument("--acknowledge-unresolved", metavar="TRADE_ID")
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)
    settings = get_settings()
    beat = heartbeat_path(settings)
    if args.health:
        return 0 if read_heartbeat(beat, datetime.now(IST))["worker"] == "RUNNING" else 1
    operator = args.status or args.verify_journal or args.acknowledge_unresolved
    if not operator:
        configure_logging(settings.log_level, settings.log_dir, settings.log_max_bytes, settings.log_backup_count)
    if not operator and not settings.ast_direct_enabled:
        logger.info("AST_DIRECT_DISABLED")
        return 0
    if settings.database_url is None:
        parser.error("DATABASE_URL is required")
    policy = DirectPolicy.model_validate_json(settings.ast_direct_config_json)
    engine = build_engine(settings.database_url.get_secret_value())
    try:
        sessions = build_session_factory(engine)

        def status() -> dict:
            return report(
                sessions, datetime.now(IST), policy, heartbeat=beat, kill_switch=kill_switch(get_settings()),
                enabled=settings.ast_direct_enabled,
            )

        if args.verify_journal:
            JOURNAL.verify(sessions, full=True)
        if args.acknowledge_unresolved:
            DirectPaperService(sessions, policy).acknowledge_unresolved(args.acknowledge_unresolved, args.note)
        if operator:
            print(json.dumps(status(), sort_keys=True, indent=2))
            return 0
        # Restores the authoritative cursor, journal head and any active position before any market access.
        service = DirectPaperService(sessions, policy)
        reader = make_reader(settings)
        paper_only_builder(reader)
        log = BlockerLog()
        current = status()
        logger.info(
            "AST_DIRECT_STARTED mode=PAPER strategy=%s policy=%s position=%s journal_head=%s",
            VERSION, policy.policy_hash, (current["position"] or {}).get("state", "FLAT"), current["journal_head"],
        )

        def run() -> None:
            try:
                kill = kill_switch(get_settings())
                # Catch up before new network work. A failed observation blocks later processing.
                for snapshot_id in service.pending_snapshot_ids():
                    log(service.process_snapshot(snapshot_id, kill_switch=kill))
                if service.pending_snapshot_ids():
                    return
                captured = capture_once(
                    sessions, reader, datetime.now(IST), settings.configured_holidays,
                    mode=CAPTURE_MODE, producer=VERSION,
                )
                if captured:
                    log(service.process_snapshot(captured, kill_switch=kill))
            except Exception as exc:
                logger.error("AST_DIRECT_CYCLE_FAILED error_type=%s reason=%s", type(exc).__name__, str(exc)[:200])

        def heartbeat() -> None:
            try:
                value = status()
                write_heartbeat(
                    beat, strategy_id=VERSION, mode="PAPER", in_session=value["in_session"],
                    status=value["status"], position=(value["position"] or {}).get("state"),
                    last_observation_at=value["last_observation_at"], kill_switch=value["kill_switch"],
                )
            except Exception as exc:
                logger.error("AST_DIRECT_HEARTBEAT_FAILED error_type=%s", type(exc).__name__)

        def session_audit() -> None:
            value = status()
            if value["forced_exit_status"] not in ("COMPLETE_FLAT", "SCHEDULED_15:20_IST"):
                logger.error("AST_DIRECT_POST_SESSION_EXPOSURE status=%s", value["forced_exit_status"])
            else:
                logger.info("AST_DIRECT_SESSION_COMPLETE gross_points=%s", value["today_gross_points"])

        heartbeat()
        scheduler = BlockingScheduler(timezone=IST)
        scheduler.add_job(
            run,
            CronTrigger(
                day_of_week="mon-fri", hour="9-15", second=seconds(settings.ast_direct_capture_offset_seconds),
                timezone=IST,
            ),
            id="ast_direct_paper_capture",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=14,
        )
        scheduler.add_job(heartbeat, CronTrigger(second="0,30", timezone=IST), id="ast_direct_heartbeat",
                          max_instances=1, coalesce=True)
        scheduler.add_job(session_audit, CronTrigger(day_of_week="mon-fri", hour=15, minute=31, timezone=IST),
                          id="ast_direct_session_audit", max_instances=1, coalesce=True)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            return 0
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
