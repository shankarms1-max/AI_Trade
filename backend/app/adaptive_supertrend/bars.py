"""Causal sampled futures bars; explicit gaps and contract boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import isfinite

from app.parallel_paper.bars import Bar, MarketPoint, coverage, session_start
from app.scalper.repository import IST


@dataclass(frozen=True)
class FutureBar(Bar):
    future_id: str | None = None
    future_expiry: date | None = None


def build_bars(points: list[MarketPoint], at: datetime, gap: int = 45) -> list[Bar]:
    if at.tzinfo is None or any(point.at.tzinfo is None or point.at > at for point in points):
        raise ValueError("AST_CAUSAL_AWARE_TIMESTAMPS_REQUIRED")
    ordered = sorted(points, key=lambda point: point.at)
    if len({point.at for point in ordered}) != len(ordered):
        raise ValueError("AST_DUPLICATE_PRICE_TIMESTAMP")
    buckets: dict[datetime, list[MarketPoint]] = {}
    dates = sorted({point.at.astimezone(IST).date() for point in ordered})
    for point in ordered:
        local = point.at.astimezone(IST)
        anchor = session_start(local)
        if not time(9, 15) <= local.time().replace(tzinfo=None) < time(15, 30):
            continue
        index = int((local - anchor).total_seconds()) // 300
        start = anchor + timedelta(minutes=index * 5)
        buckets.setdefault(start, []).append(point)
    result: list[Bar] = []
    previous_identity = None
    for day in dates:
        start = datetime.combine(day, time(9, 15), IST)
        close = datetime.combine(day, time(15, 30), IST)
        while start + timedelta(minutes=5) <= min(at, close):
            end = start + timedelta(minutes=5)
            rows = buckets.get(start, [])
            reason = coverage(rows, start, end, rows[-1], gap) if rows else "PRICE_SEQUENCE_GAP"
            if rows and (rows[-1].future_expiry is None or rows[-1].future_expiry < start.date()):
                reason = "AST_FUTURES_EXPIRY_INVALID"
            values = [float(row.price) for row in rows if row.price is not None and isfinite(row.price)]
            identity = (rows[-1].future_id, rows[-1].future_expiry) if rows else None
            if previous_identity is not None and identity != previous_identity:
                reason = "AST_FUTURES_CONTRACT_BOUNDARY"
            if identity is not None:
                previous_identity = identity
            result.append(
                FutureBar(
                    start,
                    end,
                    values[0] if values else 0,
                    max(values) if values else 0,
                    min(values) if values else 0,
                    values[-1] if values else 0,
                    None,
                    reason is None,
                    reason,
                    None,
                    identity[0] if identity else None,
                    identity[1] if identity else None,
                )
            )
            start = end
    return result


def opening_range(bars: list[Bar], at: datetime) -> dict:
    if any(bar.end > at for bar in bars):
        raise ValueError("AST_FUTURE_BAR")
    start = session_start(at)
    end = start + timedelta(minutes=15)
    rows = [bar for bar in bars if start <= bar.start < end]
    complete = at >= end and len(rows) == 3 and all(bar.valid for bar in rows)
    return {
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "opening_range_date": start.date().isoformat(),
        "complete": complete,
        "opening_high": max(bar.high for bar in rows) if complete else None,
        "opening_low": min(bar.low for bar in rows) if complete else None,
        "reason": None if complete else "AST_OPENING_RANGE_UNAVAILABLE",
    }
