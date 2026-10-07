"""Short-window features computed only from observations available at time t."""
from __future__ import annotations

from datetime import time
from statistics import median

from app.scalper.models import ScalperFeatures, ScalperMarketSnapshot


def _change(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return (current / previous - 1.0) * 10_000.0


def _sum_side(snapshot: ScalperMarketSnapshot, field: str, option_type: str) -> float | None:
    values = [getattr(item, field) for item in snapshot.quotes
              if item.option_type == option_type and abs(item.strike - snapshot.atm_strike) <= 150
              and getattr(item, field) is not None]
    return float(sum(values)) if values else None


def build_features(history: list[ScalperMarketSnapshot], lookback: int = 12,
                   expected_interval_seconds: int | None = None) -> ScalperFeatures:
    """Build deterministic features from an ordered, inclusive history."""
    if not history:
        raise ValueError("SCALPER_FEATURE_HISTORY_EMPTY")
    sorted_history = sorted(history, key=lambda item: item.captured_at)
    if (sorted_history != history or
            len({item.captured_at for item in sorted_history}) != len(sorted_history)):
        raise ValueError("SCALPER_FEATURE_HISTORY_NOT_STRICT")
    current = sorted_history[-1]
    ordered = [item for item in sorted_history
               if item.captured_at.date() == current.captured_at.date()]
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
    opening = [item.nifty_spot for item in ordered
               if item.captured_at.date() == current.captured_at.date()
               and item.captured_at.time().replace(tzinfo=None) <= time(9, 30)]
    basis = (current.nifty_future - current.nifty_spot
             if current.nifty_future is not None else None)
    previous_basis = None
    if prior and prior[-1].nifty_future is not None:
        previous_basis = prior[-1].nifty_future - prior[-1].nifty_spot
    call_now, put_now = (_sum_side(current, "open_interest", side) for side in ("CE", "PE"))
    call_volume, put_volume = (_sum_side(current, "volume", side) for side in ("CE", "PE"))
    call_previous = put_previous = None
    previous_call_volume = previous_put_volume = None
    if prior:
        call_previous, put_previous = (_sum_side(prior[-1], "open_interest", side)
                                       for side in ("CE", "PE"))
        previous_call_volume, previous_put_volume = (
            _sum_side(prior[-1], "volume", side) for side in ("CE", "PE"))
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
    if ((call_now is None or put_now is None)
            and (call_volume is None or put_volume is None)):
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
        futures_basis_change=(basis - previous_basis
                              if basis is not None and previous_basis is not None else None),
        call_participation_change=(call_now - call_previous
                                   if call_now is not None and call_previous is not None else None),
        put_participation_change=(put_now - put_previous
                                  if put_now is not None and put_previous is not None else None),
        call_volume_change=(call_volume - previous_call_volume
                            if call_volume is not None and previous_call_volume is not None
                            else None),
        put_volume_change=(put_volume - previous_put_volume
                           if put_volume is not None and previous_put_volume is not None
                           else None),
        executable_book_coverage=len(books) / len(current.quotes),
        median_spread_pct=median(spreads) if spreads else None,
        warnings=warnings,
    )
