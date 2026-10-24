"""Exchange-session time utilities. Observed session dates define holiday eligibility."""

from datetime import date, datetime, time
from enum import Enum
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

IST = ZoneInfo("Asia/Kolkata")
OPEN = time(9, 15)
CLOSE = time(15, 30)


@dataclass(frozen=True)
class SessionClock:
    """Explicit opt-in clock; the default NSE clock/IDs remain unchanged."""
    identity: str = "NSE"
    opening: time = OPEN
    closing: time = CLOSE

    def __post_init__(self):
        if not self.identity or not self.opening < self.closing:
            raise ValueError("INVALID_RESEARCH_SESSION_CLOCK")


_clock: ContextVar[SessionClock] = ContextVar("alpha_session_clock", default=SessionClock())


@contextmanager
def using_session(clock: SessionClock):
    """Task/thread-local scope, restored even on failure; never a process global."""
    token = _clock.set(clock)
    try:
        yield
    finally:
        _clock.reset(token)


class LookbackClockMode(str, Enum):
    WALL_CLOCK = "WALL_CLOCK"
    TRADING_MINUTES = "TRADING_MINUTES"
    SESSION_ONLY = "SESSION_ONLY"


def session_date(timestamp: datetime) -> date:
    return timestamp.astimezone(IST).date()


def session_id(timestamp: datetime) -> str:
    return f"{_clock.get().identity}_{session_date(timestamp).isoformat()}"


def trading_minutes_between(start: datetime, end: datetime, observed_dates: set[date],
                            holidays: frozenset[date] = frozenset()) -> float:
    if end <= start:
        return 0.0
    total = 0.0
    for day in sorted(observed_dates):
        if day.weekday() >= 5 or day in holidays:
            continue
        opening = datetime.combine(day, _clock.get().opening, IST)
        closing = datetime.combine(day, _clock.get().closing, IST)
        left, right = max(start, opening), min(end, closing)
        if right > left:
            total += (right - left).total_seconds() / 60
    return total


def lookback_window(items, now: datetime, minutes: int, mode: LookbackClockMode,
                    timestamp_getter=lambda item: item.timestamp_ist,
                    holidays: frozenset[date] = frozenset()):
    ordered = sorted((item for item in items if timestamp_getter(item) < now),
                     key=timestamp_getter)
    dates = {session_date(timestamp_getter(item)) for item in ordered} | {session_date(now)}
    result = []
    for item in ordered:
        earlier = timestamp_getter(item)
        if session_date(earlier) in holidays:
            continue
        if mode == LookbackClockMode.SESSION_ONLY and session_date(earlier) != session_date(now):
            continue
        elapsed = ((now - earlier).total_seconds() / 60 if mode != LookbackClockMode.TRADING_MINUTES
                   else trading_minutes_between(earlier, now, dates, holidays))
        if elapsed <= minutes:
            result.append(item)
    return result


def window_coverage(items, now: datetime, mode: LookbackClockMode,
                    timestamp_getter=lambda item: item.timestamp_ist,
                    holidays: frozenset[date] = frozenset()) -> tuple[float, int, int]:
    if not items:
        return 0.0, 0, 0
    dates = {session_date(timestamp_getter(item)) for item in items} | {session_date(now)}
    earliest = min(timestamp_getter(item) for item in items)
    duration = ((now - earliest).total_seconds() / 60 if mode == LookbackClockMode.WALL_CLOCK
                else trading_minutes_between(earliest, now, dates, holidays))
    return duration, len(dates), len(items)
