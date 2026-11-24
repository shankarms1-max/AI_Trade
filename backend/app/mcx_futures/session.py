"""MCX trading-session calendar (IST), configured data rather than code constants.

Default schedule: 09:00 IST open; 23:55 IST close from 2026-11-02 to
2027-03-12 (MCX circular MCX/TRD/550/2026, US winter time) and 23:30 IST close
otherwise.  Weekends are closed.  Holidays and per-date overrides (e.g. an
evening-only session that opens at 17:00) are supplied as configuration; the
default holiday list is EMPTY and must be loaded from the MCX holiday circular
by operations - it is not invented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from app.mcx_futures.spec import IST

OPEN, CLOSED, PRE_OPEN = "OPEN", "CLOSED", "PRE_OPEN"


@dataclass(frozen=True)
class CloseRule:
    start: date
    end: date
    close: time
    source: str


@dataclass(frozen=True)
class DayOverride:
    """open/close None together = closed all day; open later than default = evening-only."""

    open: time | None
    close: time | None
    reason: str
    source: str


@dataclass(frozen=True)
class SessionDay:
    session_id: str
    open_at: datetime
    close_at: datetime
    kind: str
    source: str

    def cutoffs(self, last_entry_minutes_before_close: int, forced_exit_minutes_before_close: int
                ) -> tuple[datetime, datetime]:
        """(last entry time, forced exit time) derived from the actual close of this day."""
        last_entry = self.close_at - timedelta(minutes=last_entry_minutes_before_close)
        forced = self.close_at - timedelta(minutes=forced_exit_minutes_before_close)
        return max(last_entry, self.open_at), max(forced, self.open_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "open_at": self.open_at.isoformat(),
            "close_at": self.close_at.isoformat(),
            "kind": self.kind,
            "source": self.source,
        }


@dataclass(frozen=True)
class SessionState:
    status: str
    session: SessionDay | None
    next_open: datetime | None


WINTER_2026 = CloseRule(
    date(2026, 11, 2), date(2027, 3, 12), time(23, 55), "MCX/TRD/550/2026 (US winter time, 23:55 IST close)"
)


@dataclass(frozen=True)
class SessionCalendar:
    source: str = "MCX_DEFAULT_SCHEDULE_V1"
    open_time: time = time(9, 0)
    default_close: time = time(23, 30)
    default_close_source: str = "MCX normal hours 09:00-23:30 IST (US summer time)"
    seasonal_closes: tuple[CloseRule, ...] = (WINTER_2026,)
    holidays: frozenset[date] = frozenset()
    overrides: dict[date, DayOverride] = field(default_factory=dict)
    weekend_days: frozenset[int] = frozenset({5, 6})

    def day(self, on: date) -> SessionDay | None:
        """The session on an IST calendar date, or None when closed."""
        open_time, close_time, source, kind = self.open_time, self.default_close, self.default_close_source, "REGULAR"
        for rule in self.seasonal_closes:
            if rule.start <= on <= rule.end:
                close_time, source = rule.close, rule.source
                break
        override = self.overrides.get(on)
        if override is not None:
            if override.open is None and override.close is None:
                return None
            open_time = override.open or open_time
            close_time = override.close or close_time
            source, kind = f"{override.source}:{override.reason}", "OVERRIDE"
        elif on in self.holidays or on.weekday() in self.weekend_days:
            return None
        if open_time >= close_time:
            raise ValueError(f"SESSION_WINDOW_INVALID:{on.isoformat()}")
        return SessionDay(
            session_id=on.isoformat(),
            open_at=datetime.combine(on, open_time, IST),
            close_at=datetime.combine(on, close_time, IST),
            kind=kind,
            source=source,
        )

    def next_open(self, after: datetime, horizon_days: int = 21) -> datetime | None:
        _aware(after)
        local = after.astimezone(IST)
        for offset in range(horizon_days + 1):
            item = self.day(local.date() + timedelta(days=offset))
            if item is not None and item.open_at > local:
                return item.open_at
        return None

    def state(self, at: datetime) -> SessionState:
        _aware(at)
        local = at.astimezone(IST)
        today = self.day(local.date())
        if today is not None and today.open_at <= local < today.close_at:
            return SessionState(OPEN, today, None)
        status = PRE_OPEN if today is not None and local < today.open_at else CLOSED
        return SessionState(status, today if status == PRE_OPEN else None, self.next_open(local))


def _aware(at: datetime) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("AWARE_DATETIME_REQUIRED")
