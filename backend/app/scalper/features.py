"""Short-window features computed only from observations available at time t."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import time, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from app.scalper.models import (IntradayContext, ScalperFeatures, ScalperMarketSnapshot,
                                ScalperPriceObservation)

IST = ZoneInfo("Asia/Kolkata")


PriceObservation = ScalperMarketSnapshot | ScalperPriceObservation


def _same_future(left: PriceObservation, right: PriceObservation) -> bool:
    # Missing identity is not evidence that two futures prices are comparable.
    return bool(left.future_instrument_id and left.future_expiry
                and (left.future_instrument_id, left.future_expiry)
                == (right.future_instrument_id, right.future_expiry)
                and left.nifty_future is not None and right.nifty_future is not None)


def intraday_context(ordered: Sequence[PriceObservation], interval: int) -> IntradayContext:
    current = ordered[-1]
    returns, references = {}, {}
    future_returns: dict[str, float | None] = {}
    basis_change = None
    for label, minutes in (("1m", 1), ("3m", 3), ("5m", 5), ("15m", 15)):
        target = current.captured_at - timedelta(minutes=minutes)
        reference = next((item for item in reversed(ordered) if item.captured_at <= target), None)
        # Elapsed-time horizons, no positional proxies or stale gap-spanning endpoints.
        if reference and (target - reference.captured_at).total_seconds() > interval * 1.5:
            reference = None
        returns[label] = _change(current.nifty_spot, reference.nifty_spot) if reference else None
        references[label] = reference.captured_at if reference else None
        comparable = reference is not None and _same_future(current, reference)
        future_returns[label] = None
        if comparable:
            assert reference is not None
            assert current.nifty_future is not None and reference.nifty_future is not None
            future_returns[label] = _change(current.nifty_future, reference.nifty_future)
            if label == "5m":
                basis_change = ((current.nifty_future - current.nifty_spot)
                                - (reference.nifty_future - reference.nifty_spot))
    high = max(item.nifty_spot for item in ordered)
    low = min(item.nifty_spot for item in ordered)
    opening = [item for item in ordered
               if time(9, 15) <= item.captured_at.astimezone(IST).time() < time(9, 30)]
    opening_complete = (current.captured_at.astimezone(IST).time() >= time(9, 30)
                        and len(opening) >= max(2, int(900 / interval * .8))
                        and opening[0].captured_at.astimezone(IST).time() <= time(9, 16)
                        and opening[-1].captured_at.astimezone(IST).time() >= time(9, 29))
    votes = [0 if abs(value) < 1 else 1 if value > 0 else -1
             for value in returns.values() if value is not None]
    return IntradayContext(
        session_first_at=ordered[0].captured_at, session_open=ordered[0].nifty_spot,
        session_high=high, session_low=low,
        session_move_bps=_change(current.nifty_spot, ordered[0].nifty_spot) or 0.0,
        session_range_bps=_change(high, low) or 0.0,
        range_position=(current.nifty_spot - low) / (high - low) if high > low else None,
        returns_bps=returns, horizon_reference_at=references,
        horizon_agreement=sum(votes) / len(votes) if votes else 0.0,
        opening_range_complete=opening_complete,
        opening_high_distance_bps=(
            _change(current.nifty_spot, max(item.nifty_spot for item in opening))
            if opening_complete else None),
        opening_low_distance_bps=(
            _change(current.nifty_spot, min(item.nifty_spot for item in opening))
            if opening_complete else None),
        futures_returns_bps=future_returns, futures_basis_change_5m=basis_change)


def _change(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return (current / previous - 1.0) * 10_000.0


def _participation_delta(current: ScalperMarketSnapshot, previous: ScalperMarketSnapshot,
                         field: str, side: str) -> float | None:
    def values(snapshot):
        return {item.identity: getattr(item, field) for item in snapshot.quotes
                if item.option_type == side and abs(item.strike - snapshot.atm_strike) <= 150
                and getattr(item, field) is not None}
    now, before = values(current), values(previous)
    common = now.keys() & before.keys()
    return float(sum(now[key] - before[key] for key in common)) if common else None


def build_features(history: list[ScalperMarketSnapshot], lookback: int = 12,
                   expected_interval_seconds: int | None = None, *,
                   context_history: list[ScalperPriceObservation] | None = None) -> ScalperFeatures:
    """Build deterministic features from an ordered, inclusive history."""
    if not history:
        raise ValueError("SCALPER_FEATURE_HISTORY_EMPTY")
    sorted_history = sorted(history, key=lambda item: item.captured_at)
    if (sorted_history != history or
            len({item.captured_at for item in sorted_history}) != len(sorted_history)):
        raise ValueError("SCALPER_FEATURE_HISTORY_NOT_STRICT")
    current = sorted_history[-1]
    day = current.captured_at.astimezone(IST).date()
    ordered = [item for item in sorted_history if item.captured_at.astimezone(IST).date() == day]
    context: Sequence[PriceObservation] = ordered
    if context_history is not None:
        # The lightweight query supplies strictly prior observations from this session.
        # Current data comes from the same capture as the fast window, never a later DB row.
        context = [*context_history, current]
        times = [item.captured_at for item in context]
        if (times != sorted(set(times))
                or any(item.astimezone(IST).date() != day for item in times)):
            raise ValueError("SCALPER_CONTEXT_HISTORY_NOT_STRICT")
    window = ordered[-max(2, lookback):]
    prior = window[:-1]
    returns: dict[str, float | None] = {}
    for label, distance in (("one", 1), ("three", 3), ("five", 5)):
        returns[label] = (_change(current.nifty_spot, window[-1-distance].nifty_spot)
                          if len(window) > distance else None)
    usable_returns: list[tuple[float, float]] = []
    for key, weight in (("one", .5), ("three", .3), ("five", .2)):
        value = returns[key]
        if value is not None:
            usable_returns.append((value, weight))
    weight_total = sum(weight for _, weight in usable_returns)
    momentum = (sum(value * weight for value, weight in usable_returns) / weight_total
                if weight_total else 0.0)
    rolling_reference = (sum(item.nifty_spot for item in prior[-5:]) / len(prior[-5:])
                         if prior else None)
    local = prior[-min(8, len(prior)):]
    local_high = max((item.nifty_spot for item in local), default=None)
    local_low = min((item.nifty_spot for item in local), default=None)
    opening = [item.nifty_spot for item in context
               if item.captured_at.date() == current.captured_at.date()
               and time(9, 15) <= item.captured_at.astimezone(IST).time() < time(9, 30)]
    basis = (current.nifty_future - current.nifty_spot
             if current.nifty_future is not None else None)
    previous_basis = None
    future_return = None
    if prior and _same_future(current, prior[-1]):
        assert current.nifty_future is not None and prior[-1].nifty_future is not None
        previous_basis = prior[-1].nifty_future - prior[-1].nifty_spot
        future_return = _change(current.nifty_future, prior[-1].nifty_future)
    participation = {}
    for side, label in (("CE", "call"), ("PE", "put")):
        for field, suffix in (("open_interest", "participation"), ("volume", "volume")):
            participation[f"{label}_{suffix}_change"] = (
                _participation_delta(current, prior[-1], field, side) if prior else None)
    books = [item for item in current.quotes if item.bid is not None and item.ask is not None
             and item.bid > 0 and item.ask >= item.bid]
    spreads: list[float] = []
    for item in books:
        assert item.bid is not None and item.ask is not None
        if item.ask + item.bid > 0:
            spreads.append(((item.ask - item.bid) / ((item.ask + item.bid) / 2.0))
                           * 100.0)
    warnings: list[str] = ["UNDERLYING_VWAP_UNAVAILABLE"]
    if len(window) < min(5, lookback):
        warnings.append("SHORT_FEATURE_HISTORY")
    if (prior and expected_interval_seconds is not None and
            (current.captured_at - prior[-1].captured_at).total_seconds()
            > expected_interval_seconds * 1.5):
        warnings.append("MISSING_FAST_OBSERVATION")
    if basis is None:
        warnings.append("FUTURES_UNAVAILABLE")
    if all(value is None for value in participation.values()):
        warnings.append("PARTICIPATION_UNAVAILABLE")
    return ScalperFeatures(
        timestamp=current.captured_at,
        sample_count=len(window),
        returns_bps=returns,
        momentum=momentum,
        rolling_reference=rolling_reference,
        opening_high=max(opening) if opening else None,
        opening_low=min(opening) if opening else None,
        local_high=local_high,
        local_low=local_low,
        futures_basis=basis,
        futures_return_bps=future_return,
        intraday=intraday_context(context, expected_interval_seconds or 15),
        futures_basis_change=(basis - previous_basis
                              if basis is not None and previous_basis is not None else None),
        call_participation_change=participation["call_participation_change"],
        put_participation_change=participation["put_participation_change"],
        call_volume_change=participation["call_volume_change"],
        put_volume_change=participation["put_volume_change"],
        executable_book_coverage=len(books) / len(current.quotes),
        median_spread_pct=median(spreads) if spreads else None,
        warnings=warnings,
    )
