"""One causal, session-anchored sampled futures OHLC series for all indicators."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite

from app.parallel_paper.config import ExperimentConfig
from app.scalper.repository import IST


@dataclass(frozen=True)
class MarketPoint:
    at: datetime
    price: float | None
    future_id: str | None
    future_expiry: date | None
    volume: int | None = None
    volume_reason: str | None = "OPTION_BASKET_VOLUME_UNAVAILABLE"
    volume_counters: tuple[int, ...] | None = None


@dataclass(frozen=True)
class Bar:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None
    valid: bool = True
    reason: str | None = None
    volume_reason: str | None = None


def session_start(at: datetime) -> datetime:
    return at.astimezone(IST).replace(hour=9, minute=15, second=0, microsecond=0)


def _price_ok(point: MarketPoint, reference: MarketPoint) -> bool:
    return (point.price is not None and isfinite(point.price) and point.price > 0
            and bool(reference.future_id) and reference.future_expiry is not None
            and (point.future_id, point.future_expiry) == (
                reference.future_id, reference.future_expiry))


def coverage(points: list[MarketPoint], start: datetime, end: datetime,
             reference: MarketPoint, gap: int) -> str | None:
    if len(points) < 2:
        return "INSUFFICIENT_PRICE_SAMPLES"
    if any(not _price_ok(point, reference) for point in points):
        return "FUTURES_IDENTITY_OR_PRICE_UNAVAILABLE"
    if ((points[0].at - start).total_seconds() > gap
            or (end - points[-1].at).total_seconds() > gap
            or any((right.at - left.at).total_seconds() > gap
                   for left, right in zip(points, points[1:]))):
        return "PRICE_SEQUENCE_GAP"
    return None


def build_bars(points: list[MarketPoint], at: datetime,
               config: ExperimentConfig) -> list[Bar]:
    if any(point.at > at for point in points):
        raise ValueError("PARALLEL_FUTURE_OBSERVATION")
    points = sorted([point for point in points if point.at >= session_start(at)],
                    key=lambda point: point.at)
    if not points:
        return []
    reference = points[-1]
    step = timedelta(minutes=config.bar_minutes)
    start = session_start(at)
    bars = []
    while start + step <= at:
        end = start + step
        rows = [point for point in points if start <= point.at < end]
        reason = coverage(rows, start, end, reference, config.max_sequence_gap_seconds)
        prices = [point.price for point in rows if _price_ok(point, reference)]
        # Invalid bars remain explicit boundaries; indicators cannot skip gaps.
        values = [float(value) for value in prices if value is not None]
        baseline = next((point for point in reversed(points) if point.at <= start), None)
        latest = next((point for point in reversed(points) if point.at <= end), None)
        interval = [point for point in points if baseline and baseline.at <= point.at <= end]
        volume = None
        volume_reason: str | None = "VOLUME_BOUNDARY_UNAVAILABLE"
        if (baseline is not None and latest is not None and baseline is not latest
                and (start - baseline.at).total_seconds() <= config.max_sequence_gap_seconds
                and (end - latest.at).total_seconds() <= config.max_sequence_gap_seconds):
            if any(point.volume is None for point in interval):
                volume_reason = next(point.volume_reason or "OPTION_BASKET_VOLUME_UNAVAILABLE"
                                     for point in interval if point.volume is None)
            elif any(right.volume < left.volume  # type: ignore[operator]
                     or (left.volume_counters is not None and right.volume_counters is not None
                         and (len(left.volume_counters) != len(right.volume_counters)
                              or any(b < a for a, b in zip(left.volume_counters, right.volume_counters))))
                     for left, right in zip(interval, interval[1:])):
                volume_reason = "OPTION_BASKET_COUNTER_RESET"
            else:
                assert baseline.volume is not None and latest.volume is not None
                volume, volume_reason = latest.volume - baseline.volume, None
        bars.append(Bar(start, end, values[0] if values else 0,
                        max(values) if values else 0, min(values) if values else 0,
                        values[-1] if values else 0, volume, reason is None,
                        reason, volume_reason))
        start = end
    return bars


def contiguous_bars(bars: list[Bar]) -> list[Bar]:
    result = []
    for bar in reversed(bars):
        if not bar.valid:
            break
        result.append(bar)
    return list(reversed(result))


def opening_range(points: list[MarketPoint], at: datetime, minutes: int,
                  config: ExperimentConfig) -> dict:
    if any(point.at > at for point in points):
        raise ValueError("PARALLEL_FUTURE_OBSERVATION")
    start = session_start(at)
    end = start + timedelta(minutes=minutes)
    rows = sorted([point for point in points if start <= point.at < end],
                  key=lambda point: point.at)
    reason = ("OPENING_RANGE_NOT_COMPLETE" if at < end else
              coverage(rows, start, end, points[-1], config.max_sequence_gap_seconds)
              if points else "INSUFFICIENT_PRICE_SAMPLES")
    prices = [point.price for point in rows if points and _price_ok(point, points[-1])]
    values = [float(value) for value in prices if value is not None]
    high, low = (max(values), min(values)) if values else (None, None)
    return {"range_minutes": minutes, "range_start": start.isoformat(),
            "range_end": end.isoformat(), "complete": reason is None,
            "opening_high": high, "opening_low": low,
            "range_size": high - low if high is not None and low is not None else None,
            "samples": len(rows), "reason": reason}
