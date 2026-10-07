from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
import threading
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.core.logging import get_logger
from app.data.models import MarketSnapshot
from app.db.repositories import SaveResult, SnapshotRepository

logger = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class TradingCalendar:
    start_time: time
    end_time: time
    holidays: frozenset[date] = frozenset()

    def eligibility(self, now: datetime) -> str:
        local = now.astimezone(IST)
        if local.weekday() >= 5:
            return "WEEKEND"
        if local.date() in self.holidays:
            return "HOLIDAY"
        if local.time().replace(tzinfo=None) < self.start_time:
            return "OUTSIDE_MARKET"
        # The configured end is an inclusive observation minute. Scheduler
        # dispatch (even a clock-aligned job) occurs slightly after second zero.
        if local.time().replace(tzinfo=None, second=0, microsecond=0) > self.end_time:
            return "OUTSIDE_MARKET"
        return "OPEN"


def collection_trigger(interval_minutes: int) -> CronTrigger:
    """Align collection to IST wall-clock buckets, independent of startup time."""
    if not 1 <= interval_minutes <= 60:
        raise ValueError("interval_minutes must be between 1 and 60")
    return CronTrigger(minute=f"*/{interval_minutes}", second=0, timezone=IST)


def collection_bucket(timestamp: datetime, interval_minutes: int) -> datetime:
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    local = timestamp.astimezone(IST)
    minute = (local.minute // interval_minutes) * interval_minutes
    return local.replace(minute=minute, second=0, microsecond=0)


class CollectorService:
    def __init__(
        self,
        snapshot_builder: Callable[[], MarketSnapshot],
        repository: SnapshotRepository,
        calendar: TradingCalendar,
        interval_minutes: int = 3,
        after_snapshot: Callable[[int], object] | None = None,
        snapshot_observer: Callable[[SaveResult], object] | None = None,
        failure_observer: Callable[[BaseException], object] | None = None,
    ) -> None:
        self._snapshot_builder = snapshot_builder
        self._repository = repository
        self._calendar = calendar
        self._interval_minutes = interval_minutes
        self._after_snapshot = after_snapshot
        self._snapshot_observer = snapshot_observer
        self._failure_observer = failure_observer
        self._lock = threading.Lock()

    def collect_once(self, *, suppress_errors: bool = False) -> SaveResult | None:
        if not self._lock.acquire(blocking=False):
            logger.warning("COLLECTOR_RUN_SKIPPED_OVERLAP")
            return None
        run_id: int | None = None
        try:
            started_at = datetime.now(IST)
            run_id = self._repository.create_collector_run(started_at)
            logger.info("COLLECTOR_RUN_STARTED")
            snapshot = self._snapshot_builder()
            logger.info("SNAPSHOT_FETCHED contracts=%d", len(snapshot.options))
            result = self._repository.save_market_snapshot(
                snapshot,
                collection_bucket(snapshot.timestamp_ist, self._interval_minutes),
                run_id,
            )
            logger.info(
                "SNAPSHOT_STORED snapshot_id=%d contracts=%d duplicate=%s",
                result.snapshot_id,
                result.contracts,
                str(result.duplicate).lower(),
            )
            if self._snapshot_observer is not None:
                self._snapshot_observer(result)
            if self._after_snapshot is not None:
                try:
                    self._after_snapshot(result.snapshot_id)
                except Exception as exc:
                    logger.error(
                        "PIPELINE_AFTER_SNAPSHOT_FAILED snapshot_id=%d error_type=%s",
                        result.snapshot_id,
                        type(exc).__name__,
                    )
            logger.info("COLLECTOR_RUN_SUCCESS")
            return result
        except Exception as exc:
            if self._failure_observer is not None:
                try:
                    self._failure_observer(exc)
                except Exception:
                    logger.error("COLLECTOR_FAILURE_OBSERVER_FAILED")
            if run_id is not None:
                try:
                    self._repository.mark_collector_failed(run_id, exc)
                except Exception:
                    logger.error("COLLECTOR_RUN_FAILURE_STATUS_UNAVAILABLE")
            logger.error("COLLECTOR_RUN_FAILED error_type=%s", type(exc).__name__)
            if not suppress_errors:
                raise
            return None
        finally:
            self._lock.release()

    def run_scheduled(self, now: datetime | None = None) -> SaveResult | None:
        reason = self._calendar.eligibility(now or datetime.now(IST))
        if reason != "OPEN":
            logger.info("COLLECTOR_RUN_SKIPPED_%s", reason)
            return None
        return self.collect_once(suppress_errors=True)
