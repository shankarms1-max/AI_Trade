from datetime import date, datetime, time, timedelta
import threading
from zoneinfo import ZoneInfo

import pytest

from app.collector.service import CollectorService, TradingCalendar, collection_bucket, collection_trigger
from app.data.models import MarketSnapshot
from app.db.repositories import SnapshotRepository

IST = ZoneInfo("Asia/Kolkata")


def at(day: int, hour: int, minute: int) -> datetime:
    return datetime(2099, 10, day, hour, minute, tzinfo=IST)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (at(3, 10, 0), "WEEKEND"),
        (at(1, 9, 17), "OUTSIDE_MARKET"),
        (at(1, 15, 28), "OUTSIDE_MARKET"),
        (at(1, 9, 18), "OPEN"),
        (at(1, 15, 24), "OPEN"),
        (at(1, 15, 27), "OPEN"),
        (at(1, 15, 27).replace(second=4, microsecond=264742), "OPEN"),
        (at(1, 15, 27).replace(second=59, microsecond=999999), "OPEN"),
        (at(1, 15, 30), "OUTSIDE_MARKET"),
        (at(1, 9, 17).replace(second=59), "OUTSIDE_MARKET"),
    ],
)
def test_market_time_eligibility(moment: datetime, expected: str) -> None:
    calendar = TradingCalendar(time(9, 18), time(15, 27))
    assert calendar.eligibility(moment) == expected


def test_configured_holiday_is_skipped() -> None:
    holiday = date(2099, 10, 1)
    calendar = TradingCalendar(time(9, 18), time(15, 27), frozenset({holiday}))
    assert calendar.eligibility(at(1, 10, 0)) == "HOLIDAY"


def test_collection_bucket_uses_ist_and_three_minute_floor() -> None:
    assert collection_bucket(at(1, 10, 17), 3) == at(1, 10, 15)


def test_clock_aligned_schedule_has_all_124_session_observations():
    trigger = collection_trigger(3)
    calendar = TradingCalendar(time(9, 18), time(15, 27))
    # An arbitrary process startup time must not offset every job by four seconds.
    current = at(1, 9, 16).replace(second=4, microsecond=264742)
    previous = None
    observations = []
    while current < at(1, 15, 31):
        fired = trigger.get_next_fire_time(previous, current)
        if calendar.eligibility(fired + timedelta(microseconds=100)) == "OPEN":
            observations.append(fired)
        previous, current = fired, fired + timedelta(microseconds=1)
    assert len(observations) == 124
    assert observations[0] == at(1, 9, 18)
    assert observations[-2:] == [at(1, 15, 24), at(1, 15, 27)]
    assert at(1, 15, 30) not in observations
    assert all(b - a == timedelta(minutes=3) for a, b in zip(observations, observations[1:]))


@pytest.mark.parametrize("moment,collect", [
    (at(1, 9, 17).replace(second=59), False),
    (at(1, 9, 18).replace(microsecond=100), True),
    (at(1, 15, 24).replace(second=4), True),
    (at(1, 15, 27).replace(second=4), True),
    (at(1, 15, 28), False),
    (at(1, 15, 30), False),
])
def test_scheduled_boundary_actually_collects(moment, collect, repository, market_snapshot):
    calls = []
    def builder():
        calls.append(moment)
        return market_snapshot.model_copy(update={"timestamp_ist": moment})
    service = CollectorService(builder, repository, TradingCalendar(time(9, 18), time(15, 27)))
    result = service.run_scheduled(moment)
    assert (result is not None) is collect
    assert len(calls) == int(collect)


def test_scheduled_skip_never_builds_snapshot(
    repository: SnapshotRepository,
    market_snapshot: MarketSnapshot,
) -> None:
    called = False

    def builder() -> MarketSnapshot:
        nonlocal called
        called = True
        return market_snapshot

    service = CollectorService(
        builder, repository, TradingCalendar(time(9, 18), time(15, 27))
    )
    assert service.run_scheduled(at(3, 10, 0)) is None
    assert called is False


def test_no_overlapping_collection(
    repository: SnapshotRepository,
    market_snapshot: MarketSnapshot,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def builder() -> MarketSnapshot:
        entered.set()
        release.wait(timeout=2)
        return market_snapshot

    service = CollectorService(
        builder, repository, TradingCalendar(time(9, 18), time(15, 27))
    )
    thread = threading.Thread(target=service.collect_once)
    thread.start()
    assert entered.wait(timeout=2)
    assert service.collect_once() is None
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_scheduler_style_run_survives_failure_and_retries(
    repository: SnapshotRepository,
    market_snapshot: MarketSnapshot,
) -> None:
    calls = 0

    def builder() -> MarketSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary broker failure")
        return market_snapshot

    service = CollectorService(
        builder, repository, TradingCalendar(time(9, 18), time(15, 27))
    )
    assert service.run_scheduled(at(1, 10, 0)) is None
    assert service.run_scheduled(at(1, 10, 3)) is not None
    assert calls == 2
