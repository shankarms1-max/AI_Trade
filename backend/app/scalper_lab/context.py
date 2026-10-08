"""Causal market evidence shared by independent research strategies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from app.scalper.features import build_features
from app.scalper.models import ScalperMarketSnapshot, ScalperPriceObservation

IST = ZoneInfo("Asia/Kolkata")
HORIZONS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}
FUTURES_BASIS = "BROKER_VOLUME_WEIGHTED_AVERAGE"


@dataclass(frozen=True)
class ResearchObservation:
    snapshot_id: int
    snapshot: ScalperMarketSnapshot
    futures: dict[str, Any] | None = None


def _identity(quote: Any) -> list[Any]:
    return [value.isoformat() if isinstance(value, date) else value
            for value in quote.identity]


def _reference(history: list[ResearchObservation], at: datetime,
               seconds: int, interval: int) -> ResearchObservation | None:
    target = at - timedelta(seconds=seconds)
    result = next((row for row in reversed(history)
                   if row.snapshot.captured_at <= target), None)
    if result is None or (target - result.snapshot.captured_at).total_seconds() > interval * 1.5:
        return None
    return result


def _valid_quote(quote: Any, at: datetime, max_age: int) -> bool:
    source = quote.source_market_timestamp
    return bool(source is not None and quote.open_interest is not None
                and quote.open_interest >= 0 and 0 <= (at - source).total_seconds() <= max_age)


def _premium(quote: Any) -> float | None:
    if quote is None or quote.bid is None or quote.ask is None:
        return None
    if not (isfinite(quote.bid) and isfinite(quote.ask)
            and 0 < quote.bid <= quote.ask):
        return None
    return (quote.bid + quote.ask) / 2


def _ratio(quotes: list[Any], field: str) -> float | None:
    if not quotes or any(getattr(q, field) is None or getattr(q, field) < 0
                         for q in quotes):
        return None
    calls = sum(getattr(q, field) for q in quotes if q.option_type == "CE")
    puts = sum(getattr(q, field) for q in quotes if q.option_type == "PE")
    return puts / calls if calls > 0 else None


def _vwap(row: ResearchObservation, max_age: int) -> dict[str, Any]:
    """Accept only broker volume-weighted futures values with exact identity."""
    evidence = row.futures or {}
    raw = row.snapshot
    result: dict[str, Any] = {
        "status": "UNAVAILABLE", "value": None,
        "slope_bps_per_minute": None,
        "reason": "AUTHORITATIVE_FUTURES_VOLUME_AND_VWAP_REQUIRED",
        "source_timestamp": evidence.get("source_timestamp"),
        "cumulative_volume": evidence.get("cumulative_volume"),
        "basis": evidence.get("basis"),
        "instrument_token": evidence.get("instrument_token"),
    }
    volume, value = evidence.get("cumulative_volume"), evidence.get("vwap")
    if not isinstance(volume, int) or isinstance(volume, bool) or volume <= 0:
        result["status"] = "MISSING_FUTURES_VOLUME"
        return result
    if (evidence.get("basis") != FUTURES_BASIS
            or not isinstance(value, (int, float)) or isinstance(value, bool)
            or not isfinite(value) or value <= 0):
        result["status"] = "MISSING_AUTHORITATIVE_VWAP"
        return result
    source_fields = evidence.get("source_fields")
    if (not isinstance(source_fields, list)
            or any(not isinstance(field, str) for field in source_fields)
            or not {"avg_cost", "last_volume", "lstup_time"} <= set(source_fields)):
        result["status"] = "UNVERIFIED_VWAP_PROVENANCE"
        return result
    if (not raw.future_instrument_id or not raw.future_expiry or raw.nifty_future is None
            or evidence.get("symbol") != raw.future_instrument_id
            or evidence.get("expiry") != raw.future_expiry.isoformat()
            or not evidence.get("instrument_token")):
        result["status"] = "FUTURES_IDENTITY_MISMATCH"
        return result
    price = evidence.get("ltp")
    if not isinstance(price, (int, float)) or not isfinite(price) or abs(price - raw.nifty_future) > .01:
        result["status"] = "FUTURES_PRICE_MISMATCH"
        return result
    try:
        source = datetime.fromisoformat(evidence["source_timestamp"])
        age = (raw.captured_at - source).total_seconds()
    except (KeyError, TypeError, ValueError):
        result["status"] = "MISSING_FUTURES_TIMESTAMP"
        return result
    if source.tzinfo is None or not 0 <= age <= max_age:
        result["status"] = "STALE_OR_FUTURE_FUTURES_QUOTE"
        return result
    result.update(status="AVAILABLE", value=float(value), reason=None)
    return result


def _options(current: ResearchObservation, history: list[ResearchObservation],
             interval: int, max_age: int) -> dict[str, Any]:
    snapshot = current.snapshot
    refs = {name: _reference(history, snapshot.captured_at, seconds, interval)
            for name, seconds in HORIZONS.items()}
    valid = [q for q in snapshot.quotes if _valid_quote(q, snapshot.captured_at, max_age)]
    counts: dict[tuple[date, float, str], int] = {}
    for quote in valid:
        key = (quote.expiry, quote.strike, quote.option_type)
        counts[key] = counts.get(key, 0) + 1
    valid = [quote for quote in valid
             if counts[(quote.expiry, quote.strike, quote.option_type)] == 1]
    maps = {name: ({q.identity: q for q in ref.snapshot.quotes
                    if _valid_quote(q, ref.snapshot.captured_at, max_age)} if ref else {})
            for name, ref in refs.items()}
    contracts: list[dict[str, Any]] = []
    for quote in sorted(valid, key=lambda q: q.identity):
        row: dict[str, Any] = {
            "identity": _identity(quote), "strike": quote.strike,
            "option_type": quote.option_type, "open_interest": quote.open_interest,
            "volume": quote.volume, "premium": _premium(quote),
            "source_market_timestamp": (quote.source_market_timestamp.isoformat()
                                        if quote.source_market_timestamp else None),
            "delta_oi": {}, "premium_change": {}, "activity": {},
            "reference_timestamps": {},
        }
        for name, ref in refs.items():
            previous = maps[name].get(quote.identity)
            delta = (quote.open_interest - previous.open_interest
                     if previous is not None and quote.open_interest is not None
                     and previous.open_interest is not None else None)
            old_premium = _premium(previous)
            change = (row["premium"] - old_premium
                      if row["premium"] is not None and old_premium is not None else None)
            activity = "UNKNOWN"
            if delta is not None and change is not None:
                activity = ("WRITING" if delta > 0 and change <= 0 else
                            "LONG_BUILDUP" if delta > 0 else
                            "SHORT_COVERING" if delta < 0 and change > 0 else
                            "LONG_UNWINDING" if delta < 0 else "STABLE")
            row["delta_oi"][name] = delta
            row["premium_change"][name] = change
            row["activity"][name] = activity
            row["reference_timestamps"][name] = (
                ref.snapshot.captured_at.isoformat()
                if previous is not None and ref is not None else None)
        contracts.append(row)
    pcr: dict[str, dict[str, Any]] = {}
    for radius in (3, 5):
        basket = [q for q in valid if abs(q.strike - snapshot.atm_strike) <= radius * 50]
        complete = (len(basket) == len({(q.strike, q.option_type) for q in basket})
                    == 2 * (2 * radius + 1))
        now_oi = _ratio(basket, "open_interest") if complete else None
        now_vol = _ratio(basket, "volume") if complete else None
        changes, slopes, volume_changes, volume_slopes = {}, {}, {}, {}
        baseline, volume_baseline = {}, {}
        for name in ("1m", "3m", "5m"):
            old_basket = [maps[name][q.identity] for q in basket if q.identity in maps[name]]
            comparable = complete and len(old_basket) == len(basket)
            old_oi = _ratio(old_basket, "open_interest") if comparable else None
            old_vol = _ratio(old_basket, "volume") if comparable else None
            change = now_oi - old_oi if now_oi is not None and old_oi is not None else None
            ref = refs[name]
            elapsed = ((snapshot.captured_at - ref.snapshot.captured_at).total_seconds() / 60
                       if ref else None)
            baseline[name] = old_oi
            volume_baseline[name] = old_vol
            changes[name] = change
            slopes[name] = change / elapsed if change is not None and elapsed else None
            volume_changes[name] = (now_vol - old_vol if now_vol is not None
                                    and old_vol is not None else None)
            volume_change = volume_changes[name]
            volume_slopes[name] = (volume_change / elapsed
                                   if volume_change is not None and elapsed else None)
        pcr[str(radius)] = {"complete": complete, "oi": now_oi, "volume": now_vol,
                            "baseline_oi": baseline, "baseline_volume": volume_baseline,
                            "oi_change": changes, "oi_slope_per_minute": slopes,
                            "volume_change": volume_changes,
                            "volume_slope_per_minute": volume_slopes,
                            "comparison_basis": "SAME_CURRENT_CONTRACT_BASKET"}
    walls: dict[str, dict[str, Any] | None] = {}
    for side, label in (("CE", "call_resistance"), ("PE", "put_support")):
        eligible = [row for row in contracts if row["option_type"] == side
                    and row["open_interest"] > 0
                    and abs(row["strike"] - snapshot.nifty_spot) <= 500
                    and (row["strike"] >= snapshot.nifty_spot if side == "CE"
                         else row["strike"] <= snapshot.nifty_spot)]
        clusters: list[dict[str, Any]] = []
        for center in eligible:
            members = [row for row in contracts if row["option_type"] == side
                       and abs(row["strike"] - center["strike"]) <= 50]
            deltas = {name: (sum(row["delta_oi"][name] for row in members)
                             if all(row["delta_oi"][name] is not None for row in members)
                             else None) for name in HORIZONS}
            clusters.append({**center, "cluster_oi": sum(r["open_interest"] for r in members),
                             "cluster_strikes": [r["strike"] for r in members],
                             "cluster_delta_oi": deltas,
                             "wall_state": {name: ("UNKNOWN" if delta is None else
                                                   "STRENGTHENING" if delta > 0 else
                                                   "WEAKENING" if delta < 0 else "STABLE")
                                            for name, delta in deltas.items()}})
        walls[label] = min(
            clusters,
            key=lambda row: (-row["cluster_oi"],
                             abs(row["strike"] - snapshot.nifty_spot), row["strike"]),
        ) if clusters else None
    classifications = {}
    for direction, label in (("BEAR", "call_resistance"), ("BULL", "put_support")):
        wall = walls[label]
        activity = wall["activity"]["1m"] if wall else "UNKNOWN"
        classifications[direction] = ("SUPPORTIVE" if activity == "WRITING" else
                                      "CONTRADICTORY" if activity in
                                      {"LONG_BUILDUP", "SHORT_COVERING"} else "NEUTRAL")
    return {"contracts": contracts, "local_pcr": pcr, "walls": walls,
            "classification_by_direction": classifications,
            "reference_timestamps": {name: (ref.snapshot.captured_at.isoformat()
                                            if ref else None) for name, ref in refs.items()},
            "oi_basis": "SELF_COMPUTED_EXACT_CONTRACT_HISTORY"}


def context_at(current: ResearchObservation, history: list[ResearchObservation],
               interval: int = 15, max_age: int = 30) -> dict[str, Any]:
    """One shared context, computed once from rows strictly before current."""
    snap = current.snapshot
    day = snap.captured_at.astimezone(IST).date()
    history = [row for row in history if row.snapshot.captured_at.astimezone(IST).date() == day]
    if any(row.snapshot.captured_at >= snap.captured_at for row in history):
        raise ValueError("LAB_FUTURE_OBSERVATION")
    prices = [ScalperPriceObservation(
        captured_at=row.snapshot.captured_at, nifty_spot=row.snapshot.nifty_spot,
        nifty_future=row.snapshot.nifty_future,
        future_instrument_id=row.snapshot.future_instrument_id,
        future_expiry=row.snapshot.future_expiry) for row in history]
    fast_window = [row.snapshot for row in history[-11:]]
    features = build_features([*fast_window, snap],
                              expected_interval_seconds=interval,
                              context_history=prices)
    intraday = features.intraday
    assert intraday is not None
    vwap = _vwap(current, max_age)
    prior = _reference(history, snap.captured_at, 60, interval)
    if prior is not None:
        old = _vwap(prior, max_age)
        elapsed = (snap.captured_at - prior.snapshot.captured_at).total_seconds()
        if (vwap["value"] is not None and old["value"] is not None
                and prior.futures is not None and current.futures is not None
                and prior.futures["instrument_token"] == current.futures["instrument_token"]
                and vwap["cumulative_volume"] >= old["cumulative_volume"]
                and elapsed > 0):
            vwap["slope_bps_per_minute"] = (
                (vwap["value"] / old["value"] - 1) * 10000 * 60 / elapsed)
    vwap["slope_reference_at"] = prior.snapshot.captured_at.isoformat() if prior else None
    futures = intraday.futures_returns_bps
    regime = "RANGE"
    for sign, name in ((1, "BULLISH"), (-1, "BEARISH")):
        three, five, fifteen = (futures.get(key) for key in ("3m", "5m", "15m"))
        trend = (three is not None and five is not None and fifteen is not None
                 and sign * three >= 2 and sign * five >= 4 and sign * fifteen >= 8)
        structure = (intraday.range_position is not None
                     and (intraday.range_position >= .6 if sign > 0
                          else intraday.range_position <= .4))
        if intraday.opening_range_complete:
            distance = (intraday.opening_high_distance_bps if sign > 0
                        else intraday.opening_low_distance_bps)
            structure = structure and distance is not None and sign * distance >= 0
        alignment = (vwap["value"] is None or
                     (snap.nifty_future is not None
                      and vwap["slope_bps_per_minute"] is not None
                      and sign * (snap.nifty_future - vwap["value"]) > 0
                      and sign * vwap["slope_bps_per_minute"] > 0))
        if trend and structure and alignment:
            regime = name
            break
    options = _options(current, history, interval, max_age)
    prev_spot = history[-1].snapshot.nifty_spot if history else None
    fast = ("BULL" if prev_spot is not None and snap.nifty_spot > prev_spot + .5
            else "BEAR" if prev_spot is not None and snap.nifty_spot < prev_spot - .5
            else "NEUTRAL")
    return {"timestamp": snap.captured_at.isoformat(), "regime": regime,
            "regime_basis": ("FUTURES_TREND_STRUCTURE_WITH_VWAP" if vwap["value"] is not None
                             else "FUTURES_TREND_STRUCTURE_VWAP_UNAVAILABLE"),
            "vwap": vwap, "futures_returns_bps": futures,
            "structure": intraday.model_dump(mode="json"),
            "options": options, "fast_direction": fast,
            "previous_spot": prev_spot, "spot": snap.nifty_spot,
            "opening_high": features.opening_high,
            "opening_low": features.opening_low,
            "future": snap.nifty_future,
            "vix": snap.india_vix, "expiry": snap.expiry.isoformat(),
            "lot_size": snap.lot_size}
