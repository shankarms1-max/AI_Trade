"""Read-only audit of the unchanged Phase 14.1 Alpha history gates."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, cast

from app.alpha.atm import matching_contract, select_atm
from app.alpha.clock import lookback_window, session_id
from app.alpha.engine import AlphaConfig, RawComponents, raw_components
from app.alpha.price_alpha import (HorizonReturn, horizon_log_return,
                                   reference_identity, reference_price)
from app.alpha.volume_alpha import interval_volume
from app.data.models import MarketSnapshot


def _window(rows: list[MarketSnapshot], current: MarketSnapshot, minutes: int,
            config: AlphaConfig) -> list[MarketSnapshot]:
    return lookback_window(rows, current.timestamp_ist, minutes,
                           config.lookback_clock_mode, holidays=config.holiday_dates)


def _horizon(point: MarketSnapshot, earlier: list[MarketSnapshot],
             config: AlphaConfig) -> HorizonReturn:
    return horizon_log_return(
        point, earlier, config.price_source, config.price_horizon_seconds,
        max(config.min_horizon_seconds,
            config.price_horizon_seconds - config.horizon_tolerance_seconds),
        min(config.max_horizon_seconds,
            config.price_horizon_seconds + config.horizon_tolerance_seconds),
        config.max_source_age_seconds)


def _rank_rejections(points: list[MarketSnapshot], current: MarketSnapshot,
                     config: AlphaConfig, raws: dict[datetime, RawComponents],
                     current_raw: RawComponents, *, standardized: bool) -> dict[str, Any]:
    rejected: Counter[str] = Counter()
    survivors: Counter[str] = Counter()
    accepted = 0
    reference = reference_identity(current, config.price_source)
    current_horizon = cast(HorizonReturn | None, current_raw.horizon)
    target_seconds = current_horizon.actual_seconds if current_horizon else None
    for point in points:
        raw = raws[point.timestamp_ist]
        point_horizon = cast(HorizonReturn | None, raw.horizon)
        point_reference = reference_identity(point, config.price_source)
        if point_reference[0] != reference[0]:
            rejected["future_identity"] += 1
            continue
        if point_reference[1] != reference[1]:
            rejected["future_expiry"] += 1
            continue
        survivors["future_identity_and_expiry"] += 1
        if point_horizon is None or point_horizon.value is None:
            rejected["horizon_" + (
                point_horizon.reason if point_horizon and point_horizon.reason else "UNKNOWN")] += 1
            continue
        survivors["valid_horizon"] += 1
        if target_seconds is None:
            rejected["current_horizon"] += 1
            continue
        if point_horizon.actual_seconds is None or abs(
                point_horizon.actual_seconds - target_seconds) > 90:
            rejected["horizon_duration_mismatch"] += 1
            continue
        survivors["matching_horizon_duration"] += 1
        if standardized and raw.standardized_return is None:
            rejected["underlying_volatility_unavailable"] += 1
            continue
        survivors["standardized_return_available"] += 1
        if standardized and (raw.ce_token is None or raw.pe_token is None):
            rejected["atm_pair_unavailable"] += 1
            continue
        if standardized and (raw.ce_history_count <= 1 or raw.pe_history_count <= 1):
            rejected["option_price_history_short"] += 1
            continue
        if standardized and not raw.sequence_continuous:
            rejected["option_volume_sequence"] += 1
            continue
        if standardized and raw.atm_pair_incomplete:
            rejected["atm_pair_incomplete"] += 1
            continue
        if standardized and (raw.atm_distance_percent is None or
                             raw.atm_distance_percent > config.atm_max_distance_percent):
            rejected["atm_distance"] += 1
            continue
        survivors["atm_and_sequence_eligible"] += 1
        accepted += 1
    return {"candidates": len(points), "accepted": accepted,
            "survivors": dict(sorted(survivors.items())),
            "rejected": dict(sorted(rejected.items()))}


def _volume_intervals(rows: list[MarketSnapshot], current: MarketSnapshot,
                      config: AlphaConfig, contract) -> dict[str, Any]:
    window = _window(rows, current, config.volume_lookback_minutes, config)
    times = {item.timestamp_ist for item in window}
    states: Counter[str] = Counter()
    candidates = accepted = 0
    for index, point in enumerate(rows):
        if point.timestamp_ist not in times or session_id(point.timestamp_ist) != session_id(
                current.timestamp_ist):
            continue
        matched = matching_contract(point, contract)
        if matched is None:
            states["CONTRACT_MISSING"] += 1
            continue
        candidates += 1
        previous = rows[index - 1] if index else None
        interval = interval_volume(matched, previous, point,
                                   config.volume_max_interval_seconds,
                                   config.max_source_age_seconds)
        if interval.value is not None:
            accepted += 1
        else:
            states[interval.state.value] += 1
    return {"candidates": candidates, "accepted": accepted,
            "rejected": dict(sorted(states.items()))}


def _option_price_sequence(rows: list[MarketSnapshot], current: MarketSnapshot,
                           config: AlphaConfig, contract) -> dict[str, Any]:
    points = [*_window(rows, current, config.volatility_lookback_minutes, config), current]
    rejected: Counter[str] = Counter()
    count = 0
    prior_at: datetime | None = None
    for point in points:
        if session_id(point.timestamp_ist) != session_id(current.timestamp_ist):
            continue
        matched = matching_contract(point, contract)
        if matched is None or matched.ltp is None or matched.ltp <= 0:
            rejected["contract_or_price_missing"] += 1
            count = 0
            continue
        if prior_at is not None and count and (
                point.timestamp_ist - prior_at).total_seconds() > config.max_sequence_gap_seconds:
            rejected["sequence_gap"] += 1
            count = 0
        count += 1
        prior_at = point.timestamp_ist
    return {"continuous_price_count": count,
            "return_count": max(0, count - 1),
            "rejected": dict(sorted(rejected.items()))}


def diagnose_alpha_history(current: MarketSnapshot, history: list[MarketSnapshot],
                           config: AlphaConfig) -> dict[str, Any]:
    """Mirror gate order with original helpers; no Alpha records or decisions change."""
    unique = {point.timestamp_ist: point for point in reversed(history)
              if point.timestamp_ist < current.timestamp_ist}
    rows = [unique[key] for key in sorted(unique)]
    dependency = max(config.alpha1_lookback_minutes,
                     config.alpha2_lookback_minutes + config.volatility_lookback_minutes,
                     config.alpha2_lookback_minutes + config.volume_lookback_minutes,
                     config.volume_lookback_minutes, config.volatility_lookback_minutes
                     ) + config.max_horizon_seconds // 60 + 2
    rows = _window(rows, current, dependency, config)
    returns: dict[datetime, object] = {}
    for index, point in enumerate(rows):
        returns[point.timestamp_ist] = _horizon(point, rows[:index], config)
    raws = {point.timestamp_ist: raw_components(point, rows[:index], config, returns)
            for index, point in enumerate(rows)}
    current_raw = raw_components(current, rows, config, returns)
    price_points = _window(rows, current, config.alpha1_lookback_minutes, config)
    standardized_points = _window(rows, current, config.alpha2_lookback_minutes, config)
    volatility_points = _window(rows, current, config.volatility_lookback_minutes, config)
    volatility_rejections: Counter[str] = Counter()
    volatility_accepted = 0
    current_horizon = cast(HorizonReturn | None, current_raw.horizon)
    target = current_horizon.actual_seconds if current_horizon else None
    for point in volatility_points:
        item = cast(HorizonReturn, returns[point.timestamp_ist])
        if item.value is None:
            volatility_rejections[item.reason or "UNKNOWN"] += 1
        elif reference_identity(point, config.price_source) != reference_identity(
                current, config.price_source):
            volatility_rejections["REFERENCE_IDENTITY_MISMATCH"] += 1
        elif target is None or item.actual_seconds is None or abs(
                item.actual_seconds - target) > 90:
            volatility_rejections["HORIZON_DURATION_MISMATCH"] += 1
        else:
            volatility_accepted += 1
    ref = reference_price(current, config.price_source)
    atm = select_atm(current, ref) if ref is not None else None
    volume = ({"CE": _volume_intervals(rows, current, config, atm.call),
               "PE": _volume_intervals(rows, current, config, atm.put)}
              if atm is not None else None)
    option_prices = ({"CE": _option_price_sequence(rows, current, config, atm.call),
                      "PE": _option_price_sequence(rows, current, config, atm.put)}
                     if atm is not None else None)
    identities = Counter(f"{point.future_instrument_id}|{point.future_expiry}"
                         for point in rows)
    option_expiries = Counter(str(point.expiry) for point in rows)
    sessions = Counter(session_id(point.timestamp_ist) for point in rows)
    horizon_seconds = Counter(str(item.actual_seconds) for item in returns.values()
                              if isinstance(item, HorizonReturn) and item.value is not None)
    return {
        "loaded_rows": len(history), "causal_unique_rows": len(rows),
        "current_at": current.timestamp_ist.isoformat(),
        "current_future_identity": current.future_instrument_id,
        "current_future_expiry": str(current.future_expiry),
        "current_option_expiry": str(current.expiry),
        "historical_future_identity_split": dict(sorted(identities.items())),
        "historical_option_expiry_split": dict(sorted(option_expiries.items())),
        "historical_session_split": dict(sorted(sessions.items())),
        "historical_horizon_seconds": dict(sorted(horizon_seconds.items())),
        "current_horizon_reason": current_horizon.reason if current_horizon else None,
        "current_horizon_seconds": current_horizon.actual_seconds
        if current_horizon else None,
        "current_source_timestamp_available": current.source_market_timestamp is not None,
        "historical_source_timestamp_missing": sum(
            point.source_market_timestamp is None for point in rows),
        "historical_source_timestamp_stale": sum(
            point.source_market_timestamp is not None and
            not 0 <= (point.timestamp_ist - point.source_market_timestamp).total_seconds()
            <= config.max_source_age_seconds for point in rows),
        "alpha1_rank": _rank_rejections(price_points, current, config, raws,
                                        current_raw, standardized=False),
        "alpha2_rank": _rank_rejections(standardized_points, current, config, raws,
                                        current_raw, standardized=True),
        "volatility_returns": {"candidates": len(volatility_points),
                               "accepted": volatility_accepted,
                               "rejected": dict(sorted(volatility_rejections.items()))},
        "volume_intervals": volume,
        "option_price_sequences": option_prices,
        "current_volume_observations": current_raw.volume_observations,
        "current_volatility_observations": current_raw.volatility_observations,
    }
