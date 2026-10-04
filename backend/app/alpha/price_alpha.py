"""Causal, same-session underlying horizon returns."""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite, log

from app.alpha.clock import session_id
from app.data.models import MarketSnapshot


@dataclass(frozen=True)
class HorizonReturn:
    value: float | None
    actual_seconds: int | None = None
    error_seconds: int | None = None
    reason: str | None = None
    reference_instrument_id: str | None = None
    reference_expiry: object | None = None
    reference_source_timestamp: datetime | None = None
    reference_age_seconds: float | None = None


def reference_price(snapshot: MarketSnapshot, source: str) -> float | None:
    return snapshot.nifty_spot if source == "SPOT" else snapshot.nifty_future


def reference_identity(snapshot: MarketSnapshot, source: str) -> tuple[str | None, object | None]:
    if source == "SPOT":
        return "NSE_NIFTY_50_SPOT", None
    return snapshot.future_instrument_id, snapshot.future_expiry


def closest_horizon_snapshot(history: list[MarketSnapshot], now: datetime,
                             horizon_seconds: int, tolerance_seconds: int,
                             source: str) -> MarketSnapshot | None:
    candidates = []
    for item in history:
        elapsed = (now - item.timestamp_ist).total_seconds()
        price = reference_price(item, source)
        if elapsed <= 0 or price is None or price <= 0 or not isfinite(price):
            continue
        error = abs(elapsed - horizon_seconds)
        if error <= tolerance_seconds:
            candidates.append((error, -item.timestamp_ist.timestamp(), item))
    return None if not candidates else min(candidates, key=lambda row: row[:2])[2]


def session_open_price(current: MarketSnapshot, history: list[MarketSnapshot],
                       source: str) -> float | None:
    identity = reference_identity(current, source)
    points = [item for item in history + [current]
              if session_id(item.timestamp_ist) == session_id(current.timestamp_ist)
              and item.timestamp_ist <= current.timestamp_ist
              and reference_identity(item, source) == identity
              and reference_price(item, source) is not None]
    return None if not points else reference_price(min(points, key=lambda x: x.timestamp_ist), source)


def horizon_log_return(current: MarketSnapshot, history: list[MarketSnapshot], source: str,
                       target_seconds: int = 300, min_seconds: int = 240,
                       max_seconds: int = 420, max_source_age_seconds: int = 600) -> HorizonReturn:
    identity, expiry = reference_identity(current, source)
    source_time = current.source_market_timestamp
    age = ((current.timestamp_ist - source_time).total_seconds()
           if source_time is not None else None)
    base = dict(reference_instrument_id=identity, reference_expiry=expiry,
                reference_source_timestamp=source_time, reference_age_seconds=age)
    now_price = reference_price(current, source)
    if identity is None or (source == "FUTURE" and expiry is None):
        return HorizonReturn(None, reason="REFERENCE_IDENTITY_UNAVAILABLE", **base)
    if now_price is None or now_price <= 0 or not isfinite(now_price):
        return HorizonReturn(None, reason="REFERENCE_PRICE_INVALID", **base)
    if age is not None and (age < 0 or age > max_source_age_seconds):
        return HorizonReturn(None, reason="STALE_REFERENCE_PRICE", **base)
    same = [point for point in history
            if session_id(point.timestamp_ist) == session_id(current.timestamp_ist)
            and reference_identity(point, source) == (identity, expiry)]
    candidates = []
    for point in same:
        elapsed = (current.timestamp_ist - point.timestamp_ist).total_seconds()
        price = reference_price(point, source)
        point_age = ((point.timestamp_ist - point.source_market_timestamp).total_seconds()
                     if point.source_market_timestamp is not None else None)
        if (elapsed <= 0 or price is None or price <= 0 or not isfinite(price)
                or point_age is not None and (point_age < 0 or point_age > max_source_age_seconds)):
            continue
        if min_seconds <= elapsed <= max_seconds:
            candidates.append((abs(elapsed - target_seconds), -point.timestamp_ist.timestamp(), point))
    if not candidates:
        return HorizonReturn(None, reason="HORIZON_UNAVAILABLE", **base)
    prior = min(candidates, key=lambda row: row[:2])[2]
    elapsed = int((current.timestamp_ist - prior.timestamp_ist).total_seconds())
    return HorizonReturn(log(now_price / reference_price(prior, source)), elapsed,
                         elapsed - target_seconds, **base)


def calculate_price_return(current: MarketSnapshot, history: list[MarketSnapshot],
                           source: str, horizon_seconds: int,
                           tolerance_seconds: int) -> float | None:
    """Compatibility entry point; Phase 14.1 returns signed log return."""
    return horizon_log_return(current, history, source, horizon_seconds,
                              max(1, horizon_seconds - tolerance_seconds),
                              horizon_seconds + tolerance_seconds).value
