"""Causal future-reference context for the unchanged ACTS V2 scorer.

The compatibility dictionary's `spot` key means reference price ONLY; no cash
feed or NIFTY model is used. Public provenance explicitly identifies the future.
Strike neighborhoods use observed indices/gaps, never NIFTY's 50-point grid.
"""
from datetime import datetime, timedelta
from collections import Counter

from app.alpha.clock import IST
from app.commodity_acts.config import InstrumentSpec
from app.commodity_acts.geometry import book_usable

HORIZONS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}


def _reference(history, at, seconds):
    target = at - timedelta(seconds=seconds)
    rows = [row for row in history if row.timestamp_ist <= target and (target - row.timestamp_ist).total_seconds() <= 22.5]
    return max(rows, key=lambda row: row.timestamp_ist) if rows else None


def _ratio(quotes, field):
    if not quotes or any(getattr(q, field) is None for q in quotes):
        return None
    calls = sum(getattr(q, field) for q in quotes if q.option_type.value == "CE")
    puts = sum(getattr(q, field) for q in quotes if q.option_type.value == "PE")
    return puts / calls if calls > 0 else None


def build_context(current, history, spec: InstrumentSpec, processed_at: datetime, max_age: int) -> dict:
    if any(row.instrument_id != current.instrument_id or row.timestamp_ist >= current.timestamp_ist for row in history):
        raise ValueError("COMMODITY_NONCAUSAL_OR_CROSS_INSTRUMENT_CONTEXT")
    history = [row for row in history if row.session_id == current.session_id
               and (row.future_token, row.future_instrument_id, row.future_expiry, row.expiry) ==
                   (current.future_token, current.future_instrument_id, current.future_expiry, current.expiry)]
    refs = {name: _reference(history, current.timestamp_ist, seconds) for name, seconds in HORIZONS.items()}
    good = [q for q in current.options if book_usable(q, current.timestamp_ist, max_age) and q.open_interest is not None]
    maps = {name: {q.identity: q for q in row.options if book_usable(q, row.timestamp_ist, max_age)} if row else {}
            for name, row in refs.items()}
    contracts = []
    for quote in good:
        deltas, activities = {}, {}
        for name in HORIZONS:
            prior = maps[name].get(quote.identity)
            delta = quote.open_interest - prior.open_interest if prior is not None and prior.open_interest is not None else None
            change = ((quote.bid + quote.ask - prior.bid - prior.ask) / 2) if prior else None
            deltas[name] = delta
            activities[name] = ("UNKNOWN" if delta is None or change is None else
                "WRITING" if delta > 0 and change <= 0 else "LONG_BUILDUP" if delta > 0 else
                "SHORT_COVERING" if delta < 0 and change > 0 else "LONG_UNWINDING" if delta < 0 else "STABLE")
        contracts.append({"strike": quote.strike, "option_type": quote.option_type.value,
                          "open_interest": quote.open_interest, "volume": quote.volume,
                          "activity": activities, "cluster_delta_oi": deltas})
    strikes = sorted({q.strike for q in current.options})
    atm_present = current.atm_strike in strikes
    atm_index = min(range(len(strikes)), key=lambda index: (abs(strikes[index] - current.atm_strike), strikes[index]))
    gap_counts = Counter(round(right - left, 8) for left, right in zip(strikes, strikes[1:]))
    modes = [gap for gap, count in gap_counts.items() if count == max(gap_counts.values())] if gap_counts else []
    step = modes[0] if len(modes) == 1 else None
    pcr = {}
    for radius in (3, 5):
        selected = set(strikes[max(0, atm_index - radius):atm_index + radius + 1])
        basket = [q for q in good if q.strike in selected]
        grid = sorted(selected)
        regular = step is not None and all(round(right - left, 8) == step for left, right in zip(grid, grid[1:]))
        complete = atm_present and regular and len(selected) == 2 * radius + 1 and len(basket) == 2 * len(selected)
        now_oi = _ratio(basket, "open_interest") if complete else None
        slopes = {}
        for name in HORIZONS:
            old = [maps[name][q.identity] for q in basket if q.identity in maps[name]]
            baseline = _ratio(old, "open_interest") if complete and len(old) == len(basket) else None
            elapsed = (current.timestamp_ist - refs[name].timestamp_ist).total_seconds() / 60 if refs[name] else None
            slopes[name] = (now_oi - baseline) / elapsed if now_oi is not None and baseline is not None and elapsed else None
        pcr[str(radius)] = {"oi": now_oi, "volume": _ratio(basket, "volume") if complete else None,
                           "oi_slope_per_minute": slopes, "complete": complete}
    walls = {}
    for side, key in (("CE", "call_resistance"), ("PE", "put_support")):
        eligible = [row for row in contracts if row["option_type"] == side and row["open_interest"] > 0
                    and step is not None and abs(row["strike"] - current.reference_price) <= 10 * step
                    and (row["strike"] >= current.reference_price if side == "CE" else row["strike"] <= current.reference_price)]
        clusters = []
        for center in eligible:
            members = [row for row in contracts if row["option_type"] == side and abs(row["strike"] - center["strike"]) <= step]
            deltas = {name: sum(row["cluster_delta_oi"][name] for row in members)
                      if all(row["cluster_delta_oi"][name] is not None for row in members) else None for name in HORIZONS}
            clusters.append({**center, "cluster_oi": sum(row["open_interest"] for row in members),
                "cluster_strikes": [row["strike"] for row in members], "cluster_delta_oi": deltas,
                "cluster_basis": "ONE_OBSERVED_MODAL_STRIKE_STEP"})
        walls[key] = min(clusters, key=lambda row: (-row["cluster_oi"], abs(row["strike"] - current.reference_price), row["strike"])) if clusters else None
    prices = [row.reference_price for row in history] + [current.reference_price]
    high, low = max(prices), min(prices)
    opening_start = datetime.combine(current.timestamp_ist.astimezone(IST).date(), spec.market_session.start, IST)
    opening_end = opening_start + timedelta(minutes=15)
    opening = [row for row in [*history, current] if opening_start <= row.timestamp_ist < opening_end]
    opening_complete = bool(opening and current.timestamp_ist >= opening_end
                            and min(row.timestamp_ist for row in opening) <= opening_start + timedelta(seconds=30)
                            and max(row.timestamp_ist for row in opening) >= opening_end - timedelta(seconds=30))
    # ACTS watch/setup timing follows market observations, including recovery.
    # Processing time is provenance, never a replacement strategy/event clock.
    return {"timestamp": current.timestamp_ist.isoformat(), "observed_at": current.timestamp_ist.isoformat(),
            "decided_at": processed_at.isoformat(),
            "reference_price": current.reference_price, "reference_basis": current.reference_basis,
            "spot": current.reference_price, "future": current.future_price,
            "future_instrument_id": current.future_instrument_id, "future_expiry": current.future_expiry,
            "futures_returns_bps": {name: (current.future_price / row.future_price - 1) * 10000 if row else None for name, row in refs.items()},
            "opening_high": max(row.reference_price for row in opening) if opening_complete else None,
            "opening_low": min(row.reference_price for row in opening) if opening_complete else None,
            "structure": {"range_position": (current.reference_price - low) / (high - low) if high > low else .5},
            "options": {"contracts": contracts, "walls": walls, "local_pcr": pcr,
                        "reference_timestamps": {name: row.timestamp_ist.isoformat() if row else None for name, row in refs.items()}},
            "lot_size": current.lot_size}
