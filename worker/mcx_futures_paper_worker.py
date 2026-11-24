"""ACTS_V3_MCX_FUTURES paper worker - ONE instrument per process (CRUDEOILM or NATGASMINI).

PAPER is the only mode: no broker order API is imported; market data comes through the two-method market-only
facade. Start-up refuses unless MCX_FUTURES_PAPER_ENABLED=true AND MCX_FUTURES_<INSTRUMENT>_ENABLED=true AND
MCX_FUTURES_MODE=PAPER. A kill-switch file (MCX_FUTURES_KILL_SWITCH_FILE) stops new observations immediately.

    python /app/worker/mcx_futures_paper_worker.py --mode PAPER --instrument CRUDEOILM            # scheduler
    python /app/worker/mcx_futures_paper_worker.py --mode PAPER --instrument CRUDEOILM --status   # read-only
    python /app/worker/mcx_futures_paper_worker.py --mode PAPER --instrument CRUDEOILM --health   # exit 0/1
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, "/app/backend")

from apscheduler.schedulers.blocking import BlockingScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.mcx_futures import runtime, store  # noqa: E402
from app.mcx_futures.bars import Observation  # noqa: E402
from app.mcx_futures.config import CONFIGS  # noqa: E402
from app.mcx_futures.costs import DEFAULT_SCHEDULE  # noqa: E402
from app.mcx_futures.models import INSTRUMENTS  # noqa: E402
from app.mcx_futures.session import SessionCalendar  # noqa: E402
from app.mcx_futures.settings import McxFuturesSettings  # noqa: E402
from app.mcx_futures.spec import resolve_futures  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
logger = get_logger("mcx_futures_paper_worker")
OFFSETS = {"CRUDEOILM": "5,20,35,50", "NATGASMINI": "10,25,40,55"}  # never collide with each other or Commodity ACTS


class Runner:
    """Holds the engine between observations; reloads from the store after any failure (store is the truth)."""

    def __init__(self, instrument, sessions, market, flags: McxFuturesSettings, clock=lambda: datetime.now(IST)):
        self.instrument, self.sessions, self.market, self.flags, self.clock = instrument, sessions, market, flags, clock
        self.cfg = CONFIGS[instrument]
        self.calendar = SessionCalendar(holidays=frozenset(date.fromisoformat(d) for d in flags.holidays))
        self.engine = self.cursor = self.spec = None
        self.spec_day: date | None = None

    def _resolve(self, today: date):
        rows = self.market.master_rows(self.instrument)
        return resolve_futures(rows, self.instrument, today, self.cfg.expiry_window_days,
                               master_date=today)

    def _load(self, now: datetime) -> None:
        self.cursor = store.load_cursor(self.sessions, self.instrument)
        state = self.cursor.get("state") or {}
        today = now.astimezone(IST).date()
        exposed = bool(state.get("position") or state.get("pending_entry"))
        if exposed and self.spec is not None:
            spec = self.spec  # never roll the contract under an open position
        else:
            spec = self._resolve(today)
        self.spec, self.spec_day = spec, today
        self.engine = runtime.open_engine(self.cfg, spec, DEFAULT_SCHEDULE, self.cursor, self.calendar)

    def tick(self) -> str:
        now = self.clock()
        if self.flags.killed():
            return "KILL_SWITCH_ACTIVE"
        if self.calendar.state(now).status != "OPEN":
            return "MARKET_CLOSED"
        try:
            if self.engine is None or self.spec_day != now.astimezone(IST).date():
                self._load(now)
            q = self.market.quote(self.spec.token, self.spec.trading_symbol)
            if q["ltp"] is None:
                raise ValueError("MCX_FUTURES_QUOTE_WITHOUT_LTP")
            obs = Observation(at=q["at"], ltp=q["ltp"], bid=q["bid"], ask=q["ask"], bid_qty=q["bid_qty"],
                              ask_qty=q["ask_qty"], cum_volume=q["cum_volume"], oi=q["oi"], source_ts=q["source_ts"])
            self.engine, self.cursor, events = runtime.step(self.sessions, self.engine, obs, self.cursor,
                                                           self.calendar, now)
            kinds = [e["type"] for e in events if e["type"] not in ("BAR", "MARK")]
            if kinds:
                logger.info("MCX_FUTURES_EVENTS instrument=%s types=%s", self.instrument, ",".join(kinds))
            return "OK"
        except store.StoreConflict as exc:
            logger.error("MCX_FUTURES_STORE_CONFLICT instrument=%s reason=%s", self.instrument, exc)
            self.engine = None  # reload authoritative state next tick; never double-apply
            return "STORE_CONFLICT"
        except Exception as exc:  # noqa: BLE001 - journal the reason; keep the scheduler alive
            reason = getattr(exc, "reason", None) or type(exc).__name__
            logger.warning("MCX_FUTURES_OBSERVATION_FAILED instrument=%s reason=%s", self.instrument, reason)
            self.engine = None
            return f"FAILED:{reason}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["PAPER"], required=True)
    ap.add_argument("--instrument", choices=INSTRUMENTS, required=True)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    flags = McxFuturesSettings.from_env()
    try:
        flags.validate()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_max_bytes, settings.log_backup_count)
    if settings.database_url is None:
        logger.error("MCX_FUTURES_REFUSED reason=DATABASE_URL_REQUIRED")
        return 2
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    if a.status or a.health:
        from app.api.mcx_futures import build_status

        row = next(r for r in build_status(sessions, settings=flags)["instruments"] if r["instrument"] == a.instrument)
        if a.status:
            print(json.dumps(row, indent=2, default=str))
            return 0
        return 0 if row["state"] in ("RUNNING", "MARKET_CLOSED", "IDLE") else 1
    if not flags.enabled(a.instrument):
        logger.info("MCX_FUTURES_DISABLED instrument=%s", a.instrument)
        return 0
    from app.mcx_futures.market import make_market

    runner = Runner(a.instrument, sessions, make_market(settings), flags)
    if a.once:
        print(runner.tick())
        return 0
    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(runner.tick, CronTrigger(day_of_week="mon-fri", second=OFFSETS[a.instrument], timezone=IST),
                      id=f"mcx_futures_{a.instrument}", max_instances=1, coalesce=True, misfire_grace_time=10)
    logger.info("MCX_FUTURES_PAPER_STARTED instrument=%s config=%s utc=%s", a.instrument,
                CONFIGS[a.instrument].config_id, datetime.now(timezone.utc).isoformat())
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
