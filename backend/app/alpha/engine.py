"""Causal Phase 14.1 research features; no broker or order access."""

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from statistics import pstdev

from app.alpha.atm import matching_contract, select_atm
from app.alpha.clock import LookbackClockMode, lookback_window, session_date, session_id, window_coverage
from app.alpha.models import (AlphaDirection, AlphaEvidenceQuality, AlphaFeatureSnapshot,
                              AlphaHypothesis, AlphaStatus, AlphaValidity, CalculationMode,
                              JointAlphaDirection, ParticipationState)
from app.alpha.price_alpha import horizon_log_return, reference_identity, reference_price, session_open_price
from app.alpha.quality import assess_quality
from app.alpha.ranks import percentile_rank
from app.alpha.volume_alpha import (VolumeState, combined_activity, directional_volume_metrics,
                                    interval_volume, volume_ratio)
from app.alpha.volatility import combined_option_volatility, observed_price_volatility
from app.data.models import MarketSnapshot


@dataclass(frozen=True)
class AlphaConfig:
    price_source: str = "FUTURE"
    price_horizon_seconds: int = 300
    horizon_tolerance_seconds: int = 120
    min_horizon_seconds: int = 240
    max_horizon_seconds: int = 420
    lookback_clock_mode: LookbackClockMode = LookbackClockMode.TRADING_MINUTES
    holiday_dates: frozenset[date] = frozenset()
    hypothesis_type: AlphaHypothesis = AlphaHypothesis.CONTINUATION
    alpha1_lookback_minutes: int = 800
    alpha2_lookback_minutes: int = 300
    volume_lookback_minutes: int = 300
    volatility_lookback_minutes: int = 300
    min_rank_observations: int = 50
    min_volume_observations: int = 5
    min_volatility_returns: int = 5
    max_sequence_gap_seconds: int = 600
    volume_max_interval_seconds: int = 420
    max_source_age_seconds: int = 600
    volatility_min_valid_level: float = 0.00001
    activity_baseline_method: str = "MEDIAN"
    activity_min_baseline: float = 1.0
    activity_ratio_cap: float = 10.0
    atm_max_distance_percent: float = 0.5
    # Retained only for legacy-Alpha-2 diagnostics, never used as a replacement divisor.
    volatility_epsilon: float = 1e-8
    strong_upper: float = 0.80
    strong_lower: float = 0.20
    moderate_upper: float = 0.70
    moderate_lower: float = 0.30
    required_consecutive_confirmations: int = 2

    def __post_init__(self) -> None:
        if not (0 <= self.strong_lower <= self.moderate_lower < self.moderate_upper
                <= self.strong_upper <= 1):
            raise ValueError("alpha thresholds must be ordered")
        if not (0 < self.min_horizon_seconds <= self.price_horizon_seconds <= self.max_horizon_seconds):
            raise ValueError("invalid horizon range")
        if self.activity_baseline_method not in {"MEDIAN", "TRIMMED_MEAN"}:
            raise ValueError("invalid activity baseline method")


@dataclass(frozen=True)
class RawComponents:
    price_return: float | None
    horizon: object | None = None
    legacy_open_return: float | None = None
    atm_strike: float | None = None
    ce_token: str | None = None
    pe_token: str | None = None
    ce_interval: int | None = None
    pe_interval: int | None = None
    ce_state: VolumeState = VolumeState.MISSING
    pe_state: VolumeState = VolumeState.MISSING
    ce_duration: float | None = None
    pe_duration: float | None = None
    ce_gap_delta: int | None = None
    pe_gap_delta: int | None = None
    ce_baseline: float | None = None
    pe_baseline: float | None = None
    ce_ratio: float | None = None
    pe_ratio: float | None = None
    activity: float | None = None
    activity_capped: bool = False
    participation: ParticipationState = ParticipationState.UNUSABLE
    put_call_ratio: float | None = None
    imbalance: float | None = None
    ce_volatility: float | None = None
    pe_volatility: float | None = None
    option_volatility: float | None = None
    impulse: float | None = None
    underlying_volatility: float | None = None
    standardized_return: float | None = None
    underlying_volatility_count: int = 0
    volatility_coverage_minutes: float = 0
    volume_observations: int = 0
    volatility_observations: int = 0
    ce_history_count: int = 0
    pe_history_count: int = 0
    atm_changed: bool = False
    atm_distance_points: float | None = None
    atm_distance_percent: float | None = None
    atm_pair_incomplete: bool = False
    local_oi_ce: int | None = None
    local_oi_pe: int | None = None
    broker_oi_ce: tuple[int | None, int | None, int | None] = (None, None, None)
    broker_oi_pe: tuple[int | None, int | None, int | None] = (None, None, None)
    sequence_continuous: bool = False
    validity_reasons: tuple[str, ...] = ()

    @property
    def volume_reset(self) -> bool:
        return self.ce_state in {VolumeState.RESET, VolumeState.CORRECTED} or self.pe_state in {VolumeState.RESET, VolumeState.CORRECTED}


def _dedup_prior(history: list[MarketSnapshot], current: MarketSnapshot) -> list[MarketSnapshot]:
    # The first persisted observation at a timestamp is canonical; later corrections
    # may be examined in RESEARCH_RECOMPUTE but cannot change live history.
    by_time = {}
    for point in history:
        if point.timestamp_ist < current.timestamp_ist:
            by_time.setdefault(point.timestamp_ist, point)
    return [by_time[key] for key in sorted(by_time)]


def _window(history: list[MarketSnapshot], current: MarketSnapshot, minutes: int,
            mode: LookbackClockMode = LookbackClockMode.TRADING_MINUTES,
            holidays: frozenset[date] = frozenset()):
    return lookback_window(history, current.timestamp_ist, minutes, mode, holidays=holidays)


def _previous(history: list[MarketSnapshot], current: MarketSnapshot):
    eligible = [item for item in history if item.timestamp_ist < current.timestamp_ist]
    return None if not eligible else max(eligible, key=lambda item: item.timestamp_ist)


def _contract_intervals(history: list[MarketSnapshot], current: MarketSnapshot, contract,
                        minutes: int, config: AlphaConfig) -> list[int]:
    points = _window(history, current, minutes, config.lookback_clock_mode, config.holiday_dates)
    eligible_times = {point.timestamp_ist for point in points}
    values = []
    for index, point in enumerate(history):
        if point.timestamp_ist not in eligible_times:
            continue
        if session_id(point.timestamp_ist) != session_id(current.timestamp_ist):
            continue
        matched = matching_contract(point, contract)
        if matched is None:
            continue
        previous = history[index - 1] if index > 0 else None
        interval = interval_volume(matched, previous, point, config.volume_max_interval_seconds,
                                   config.max_source_age_seconds)
        if interval.value is not None:
            values.append(interval.value)
    return values


def _contract_prices(history: list[MarketSnapshot], current: MarketSnapshot, contract,
                     minutes: int, config: AlphaConfig) -> tuple[list[float], int]:
    points = [*(_window(history, current, minutes, config.lookback_clock_mode, config.holiday_dates)), current]
    points = [item for item in points if session_id(item.timestamp_ist) == session_id(current.timestamp_ist)]
    prices: list[float] = []
    for point in points:
        matched = matching_contract(point, contract)
        if matched is None or matched.ltp is None or matched.ltp <= 0:
            prices = []
            continue
        if prices and (point.timestamp_ist - prior_time).total_seconds() > config.max_sequence_gap_seconds:
            prices = []
        prices.append(matched.ltp)
        prior_time = point.timestamp_ist
    return prices, len(prices)


def _underlying_returns(history: list[MarketSnapshot], current: MarketSnapshot,
                        config: AlphaConfig, return_cache: dict[datetime, object] | None = None) -> list[tuple[MarketSnapshot, float, int]]:
    points = _window(history, current, config.volatility_lookback_minutes,
                     config.lookback_clock_mode, config.holiday_dates)
    result = []
    for point in points:
        horizon = None if return_cache is None else return_cache.get(point.timestamp_ist)
        if horizon is None:
            earlier = [item for item in history if item.timestamp_ist < point.timestamp_ist]
            horizon = horizon_log_return(point, earlier, config.price_source,
                                         config.price_horizon_seconds, config.min_horizon_seconds,
                                         config.max_horizon_seconds, config.max_source_age_seconds)
        if horizon.value is not None and horizon.actual_seconds is not None:
            result.append((point, horizon.value, horizon.actual_seconds))
    return result


def raw_components(current: MarketSnapshot, history: list[MarketSnapshot],
                   config: AlphaConfig, return_cache: dict[datetime, object] | None = None) -> RawComponents:
    history = _dedup_prior(history, current)
    horizon = horizon_log_return(current, history, config.price_source,
                                 config.price_horizon_seconds,
                                 max(config.min_horizon_seconds, config.price_horizon_seconds - config.horizon_tolerance_seconds),
                                 min(config.max_horizon_seconds, config.price_horizon_seconds + config.horizon_tolerance_seconds),
                                 config.max_source_age_seconds)
    price_return = horizon.value
    opening = session_open_price(current, history, config.price_source)
    prior = None if horizon.actual_seconds is None else next(
        (p for p in history if int((current.timestamp_ist - p.timestamp_ist).total_seconds()) == horizon.actual_seconds
         and reference_identity(p, config.price_source) == reference_identity(current, config.price_source)), None)
    legacy = None
    if opening and prior is not None:
        prior_price = reference_price(prior, config.price_source)
        now_price = reference_price(current, config.price_source)
        if prior_price is not None and now_price is not None:
            legacy = (now_price - prior_price) / opening
    comparable = _underlying_returns(history, current, config, return_cache) if price_return is not None else []
    comparable = [(point, value, seconds) for point, value, seconds in comparable
                  if reference_identity(point, config.price_source) == reference_identity(current, config.price_source)
                  and horizon.actual_seconds is not None and abs(seconds - horizon.actual_seconds) <= 90]
    underlying_vol = None
    if len(comparable) >= config.min_volatility_returns:
        level = pstdev(value for _, value, _ in comparable)
        if level >= config.volatility_min_valid_level:
            underlying_vol = level
    standardized = None if underlying_vol is None or price_return is None else price_return / underlying_vol
    coverage = window_coverage([point for point, _, _ in comparable], current.timestamp_ist,
                               config.lookback_clock_mode, holidays=config.holiday_dates)[0]
    ref = reference_price(current, config.price_source)
    atm = None if ref is None else select_atm(current, ref)
    if atm is None:
        return RawComponents(price_return, horizon=horizon, legacy_open_return=legacy,
                             underlying_volatility=underlying_vol, standardized_return=standardized,
                             underlying_volatility_count=len(comparable), volatility_coverage_minutes=coverage,
                             validity_reasons=((horizon.reason,) if horizon.reason else ("ALPHA_ATM_DATA_MISSING",)))
    same_session = [item for item in history if session_id(item.timestamp_ist) == session_id(current.timestamp_ist)]
    previous = _previous(same_session, current)
    ce = interval_volume(atm.call, previous, current, config.volume_max_interval_seconds,
                         config.max_source_age_seconds)
    pe = interval_volume(atm.put, previous, current, config.volume_max_interval_seconds,
                         config.max_source_age_seconds)
    ce_baselines = _contract_intervals(history, current, atm.call, config.volume_lookback_minutes, config)
    pe_baselines = _contract_intervals(history, current, atm.put, config.volume_lookback_minutes, config)
    ce_ratio, ce_average = volume_ratio(ce.value, ce_baselines, method=config.activity_baseline_method,
                                        minimum_baseline=config.activity_min_baseline, cap=config.activity_ratio_cap)
    pe_ratio, pe_average = volume_ratio(pe.value, pe_baselines, method=config.activity_baseline_method,
                                        minimum_baseline=config.activity_min_baseline, cap=config.activity_ratio_cap)
    if min(len(ce_baselines), len(pe_baselines)) < config.min_volume_observations:
        ce_ratio = pe_ratio = None
    activity = combined_activity(ce_ratio, pe_ratio)
    participation = (ParticipationState.UNUSABLE if activity is None else
                     ParticipationState.LOW if activity < 0.5 else
                     ParticipationState.NORMAL if activity < 1.5 else
                     ParticipationState.ELEVATED if activity < 3 else ParticipationState.EXTREME)
    put_call, imbalance = directional_volume_metrics(ce.value, pe.value)
    ce_prices, ce_count = _contract_prices(history, current, atm.call, config.volatility_lookback_minutes, config)
    pe_prices, pe_count = _contract_prices(history, current, atm.put, config.volatility_lookback_minutes, config)
    ce_vol, ce_returns = observed_price_volatility(ce_prices, config.min_volatility_returns)
    pe_vol, pe_returns = observed_price_volatility(pe_prices, config.min_volatility_returns)
    option_vol = combined_option_volatility(ce_vol, pe_vol)
    # Historical Phase 14 composite is recorded only for comparison.
    legacy_impulse = (None if legacy is None or activity is None or option_vol is None
                      or option_vol <= config.volatility_epsilon else legacy * activity / option_vol)
    prior_atm = None if previous is None else select_atm(previous, reference_price(previous, config.price_source) or 0)
    changed = prior_atm is not None and (prior_atm.call.instrument_token != atm.call.instrument_token
                                        or prior_atm.put.instrument_token != atm.put.instrument_token)
    ce_prior = None if previous is None else matching_contract(previous, atm.call)
    pe_prior = None if previous is None else matching_contract(previous, atm.put)
    local_ce = (atm.call.open_interest - ce_prior.open_interest
                if ce_prior and atm.call.open_interest is not None and ce_prior.open_interest is not None
                and ce.state in {VolumeState.VALID_INTERVAL, VolumeState.VALID_ZERO}
                and ce.duration_seconds is not None and ce.duration_seconds <= config.max_sequence_gap_seconds else None)
    local_pe = (atm.put.open_interest - pe_prior.open_interest
                if pe_prior and atm.put.open_interest is not None and pe_prior.open_interest is not None
                and pe.state in {VolumeState.VALID_INTERVAL, VolumeState.VALID_ZERO}
                and pe.duration_seconds is not None and pe.duration_seconds <= config.max_sequence_gap_seconds else None)
    continuity = ce.state in {VolumeState.VALID_INTERVAL, VolumeState.VALID_ZERO} and pe.state in {VolumeState.VALID_INTERVAL, VolumeState.VALID_ZERO}
    reasons = []
    if horizon.reason:
        reasons.append(horizon.reason)
    if not atm.call.instrument_token or not atm.put.instrument_token or not continuity:
        reasons.append("ATM_CONTRACT_CONTINUITY_INVALID")
    if atm.nearest_pair_incomplete:
        reasons.append("ATM_PAIR_INCOMPLETE")
    if atm.distance_percent > config.atm_max_distance_percent:
        reasons.append("ATM_SELECTION_DISTANCE_EXCESSIVE")
    return RawComponents(
        price_return=price_return, horizon=horizon, legacy_open_return=legacy,
        atm_strike=atm.strike, ce_token=atm.call.instrument_token, pe_token=atm.put.instrument_token,
        ce_interval=ce.value, pe_interval=pe.value, ce_state=ce.state, pe_state=pe.state,
        ce_duration=ce.duration_seconds, pe_duration=pe.duration_seconds,
        ce_gap_delta=ce.gap_cumulative_delta, pe_gap_delta=pe.gap_cumulative_delta,
        ce_baseline=ce_average, pe_baseline=pe_average, ce_ratio=ce_ratio, pe_ratio=pe_ratio,
        activity=activity, activity_capped=bool(
            (ce_average is not None and ce.value is not None and ce.value > ce_average * config.activity_ratio_cap)
            or (pe_average is not None and pe.value is not None and pe.value > pe_average * config.activity_ratio_cap)),
        participation=participation, put_call_ratio=put_call, imbalance=imbalance,
        ce_volatility=ce_vol, pe_volatility=pe_vol, option_volatility=option_vol,
        impulse=legacy_impulse, underlying_volatility=underlying_vol,
        standardized_return=standardized, underlying_volatility_count=len(comparable),
        volatility_coverage_minutes=coverage,
        volume_observations=min(len(ce_baselines), len(pe_baselines)),
        volatility_observations=min(ce_returns, pe_returns), ce_history_count=ce_count,
        pe_history_count=pe_count, atm_changed=changed,
        atm_distance_points=atm.distance_points, atm_distance_percent=atm.distance_percent,
        atm_pair_incomplete=atm.nearest_pair_incomplete, local_oi_ce=local_ce, local_oi_pe=local_pe,
        broker_oi_ce=(atm.call.open_interest, atm.call.previous_open_interest,
                      atm.call.change_in_open_interest),
        broker_oi_pe=(atm.put.open_interest, atm.put.previous_open_interest,
                      atm.put.change_in_open_interest),
        sequence_continuous=continuity, validity_reasons=tuple(reasons),
    )


def direction(rank: float | None, config: AlphaConfig,
              signed_return: float | None = None) -> AlphaDirection:
    if rank is None or signed_return is None or not isfinite(signed_return):
        return AlphaDirection.INSUFFICIENT_DATA
    if signed_return == 0:
        return AlphaDirection.NEUTRAL
    rank_bull = rank >= config.moderate_upper
    rank_bear = rank <= config.moderate_lower
    if config.hypothesis_type == AlphaHypothesis.CONTINUATION:
        if signed_return > 0 and rank_bull:
            return AlphaDirection.STRONG_BULLISH if rank >= config.strong_upper else AlphaDirection.BULLISH
        if signed_return < 0 and rank_bear:
            return AlphaDirection.STRONG_BEARISH if rank <= config.strong_lower else AlphaDirection.BEARISH
    else:
        if signed_return > 0 and rank_bull:
            return AlphaDirection.STRONG_BEARISH if rank >= config.strong_upper else AlphaDirection.BEARISH
        if signed_return < 0 and rank_bear:
            return AlphaDirection.STRONG_BULLISH if rank <= config.strong_lower else AlphaDirection.BULLISH
    return AlphaDirection.NEUTRAL


def rank_sign_conflict(rank: float | None, signed_return: float | None,
                       config: AlphaConfig) -> bool:
    if rank is None or signed_return is None or config.hypothesis_type != AlphaHypothesis.CONTINUATION:
        return False
    return signed_return < 0 and rank >= config.moderate_upper or signed_return > 0 and rank <= config.moderate_lower


def joint_direction(first: AlphaDirection, second: AlphaDirection) -> JointAlphaDirection:
    if AlphaDirection.INSUFFICIENT_DATA in {first, second}:
        return JointAlphaDirection.INSUFFICIENT_DATA
    bullish = {AlphaDirection.BULLISH, AlphaDirection.STRONG_BULLISH}
    bearish = {AlphaDirection.BEARISH, AlphaDirection.STRONG_BEARISH}
    if first == second == AlphaDirection.STRONG_BULLISH:
        return JointAlphaDirection.STRONG_BULLISH_CONFIRMATION
    if first in bullish and second in bullish:
        return JointAlphaDirection.BULLISH_CONFIRMATION
    if first == second == AlphaDirection.STRONG_BEARISH:
        return JointAlphaDirection.STRONG_BEARISH_CONFIRMATION
    if first in bearish and second in bearish:
        return JointAlphaDirection.BEARISH_CONFIRMATION
    if first == second == AlphaDirection.NEUTRAL:
        return JointAlphaDirection.NEUTRAL
    return JointAlphaDirection.CONFLICT


def _direction_family(joint: JointAlphaDirection) -> str | None:
    if "BULLISH" in joint.value:
        return "BULLISH"
    if "BEARISH" in joint.value:
        return "BEARISH"
    return None


def signal_persistence(joint: JointAlphaDirection, prior_alpha: list[AlphaFeatureSnapshot],
                       *, timestamp: datetime, session: str, reference_id: str | None,
                       reference_expiry, option_expiry, ce_token: str | None, pe_token: str | None,
                       hypothesis: AlphaHypothesis, max_gap_seconds: int,
                       validity: AlphaValidity) -> tuple[int, str | None]:
    family = _direction_family(joint)
    if validity != AlphaValidity.VALID or family is None:
        return 0, "INVALID_OR_NEUTRAL_SIGNAL"
    ordered = sorted((item for item in prior_alpha if item.timestamp < timestamp),
                     key=lambda item: item.timestamp, reverse=True)
    if not ordered:
        return 1, "NO_PRIOR_SIGNAL"
    count = 1
    previous_time = timestamp
    reset_reason = None
    for prior in ordered:
        gap = (previous_time - prior.timestamp).total_seconds()
        if gap <= 0 or gap > max_gap_seconds:
            reset_reason = "SIGNIFICANT_POLLING_GAP"
        elif prior.session_id != session:
            reset_reason = "SESSION_CHANGED"
        elif prior.reference_instrument_id != reference_id or prior.reference_expiry != reference_expiry:
            reset_reason = "REFERENCE_CHANGED"
        elif prior.expiry != option_expiry or prior.hypothesis_type != hypothesis:
            reset_reason = "EXPIRY_OR_HYPOTHESIS_CHANGED"
        elif prior.atm_ce_token != ce_token or prior.atm_pe_token != pe_token:
            reset_reason = "CONTRACT_POLICY_CHANGED"
        elif prior.validity_state != AlphaValidity.VALID:
            reset_reason = "PRIOR_ALPHA_INVALID"
        elif _direction_family(prior.joint_alpha_direction) != family:
            reset_reason = "OPPOSITE_OR_NEUTRAL_SIGNAL"
        if reset_reason:
            break
        count += 1
        previous_time = prior.timestamp
    return count, reset_reason


def consecutive_confirmation_count(joint: JointAlphaDirection,
                                   prior_alpha: list[AlphaFeatureSnapshot]) -> int:
    """Legacy helper; production uses signal_persistence with session/continuity gates."""
    family = _direction_family(joint)
    if family is None:
        return 0
    count = 1
    for prior in reversed(sorted(prior_alpha, key=lambda item: item.timestamp)):
        if _direction_family(prior.joint_alpha_direction) != family:
            break
        count += 1
    return count


def _historical_raw_values(history: list[MarketSnapshot], current: MarketSnapshot,
                           minutes: int, config: AlphaConfig, selector,
                           raw_cache: dict[datetime, RawComponents] | None = None) -> list[tuple[MarketSnapshot, RawComponents, float]]:
    points = _window(history, current, minutes, config.lookback_clock_mode, config.holiday_dates)
    results = []
    for point in points:
        if raw_cache is None:
            earlier = [item for item in history if item.timestamp_ist < point.timestamp_ist]
            raw = raw_components(point, earlier, config)
        else:
            raw = raw_cache[point.timestamp_ist]
        value = selector(raw)
        if value is not None and isfinite(value):
            results.append((point, raw, value))
    return results


def build_alpha_features(snapshot_id: int, current: MarketSnapshot,
                         history: list[MarketSnapshot], prior_alpha: list[AlphaFeatureSnapshot],
                         config: AlphaConfig = AlphaConfig(),
                         calculation_mode: CalculationMode = CalculationMode.HISTORICAL_REPLAY) -> AlphaFeatureSnapshot:
    history = _dedup_prior(history, current)
    # Keep just the causal dependency cone: rank history plus each historical
    # row's volume/volatility baseline and one underlying horizon of padding.
    dependency_minutes = max(
        config.alpha1_lookback_minutes,
        config.alpha2_lookback_minutes + config.volatility_lookback_minutes,
        config.alpha2_lookback_minutes + config.volume_lookback_minutes,
        config.volume_lookback_minutes, config.volatility_lookback_minutes,
    ) + config.max_horizon_seconds // 60 + 2
    history = _window(history, current, dependency_minutes,
                      config.lookback_clock_mode, config.holiday_dates)
    return_cache = {}
    for index, point in enumerate(history):
        return_cache[point.timestamp_ist] = horizon_log_return(
            point, history[:index], config.price_source, config.price_horizon_seconds,
            max(config.min_horizon_seconds, config.price_horizon_seconds - config.horizon_tolerance_seconds),
            min(config.max_horizon_seconds, config.price_horizon_seconds + config.horizon_tolerance_seconds),
            config.max_source_age_seconds)
    raw_cache = {point.timestamp_ist: raw_components(point, history[:index], config, return_cache)
                 for index, point in enumerate(history)}
    raw = raw_components(current, history, config, return_cache)
    reference = reference_identity(current, config.price_source)
    price_rows = _historical_raw_values(history, current, config.alpha1_lookback_minutes,
                                        config, lambda item: item.price_return, raw_cache)
    price_rows = [(point, item, value) for point, item, value in price_rows
                  if reference_identity(point, config.price_source) == reference
                  and raw.horizon is not None and raw.horizon.actual_seconds is not None
                  and item.horizon is not None and item.horizon.actual_seconds is not None
                  and abs(item.horizon.actual_seconds - raw.horizon.actual_seconds) <= 90]
    standardized_rows = _historical_raw_values(history, current, config.alpha2_lookback_minutes,
                                                config, lambda item: item.standardized_return, raw_cache)
    standardized_rows = [(point, item, value) for point, item, value in standardized_rows
                         if reference_identity(point, config.price_source) == reference
                         and raw.horizon is not None and raw.horizon.actual_seconds is not None
                         and item.horizon is not None and item.horizon.actual_seconds is not None
                         and abs(item.horizon.actual_seconds - raw.horizon.actual_seconds) <= 90
                         and item.ce_token is not None and item.pe_token is not None
                         and item.ce_history_count > 1 and item.pe_history_count > 1
                         and item.sequence_continuous and not item.atm_pair_incomplete
                         and item.atm_distance_percent is not None
                         and item.atm_distance_percent <= config.atm_max_distance_percent]
    rank1 = None if raw.price_return is None else percentile_rank(
        raw.price_return, [value for _, _, value in price_rows], config.min_rank_observations)
    hard_atm_valid = (raw.atm_strike is not None and raw.ce_token is not None and raw.pe_token is not None
                      and raw.sequence_continuous and raw.ce_history_count > 1 and raw.pe_history_count > 1
                      and not raw.atm_pair_incomplete
                      and raw.atm_distance_percent is not None
                      and raw.atm_distance_percent <= config.atm_max_distance_percent)
    rank2 = (None if raw.standardized_return is None or not hard_atm_valid else
             percentile_rank(raw.standardized_return,
                             [value for _, _, value in standardized_rows], config.min_rank_observations))
    first = direction(rank1, config, raw.price_return)
    second = direction(rank2, config, raw.price_return)
    joint = joint_direction(first, second)
    validity = (AlphaValidity.INVALID if raw.price_return is None else
                AlphaValidity.VALID if rank1 is not None and rank2 is not None and hard_atm_valid else
                AlphaValidity.PARTIAL)
    count, reset = signal_persistence(
        joint, prior_alpha, timestamp=current.timestamp_ist, session=session_id(current.timestamp_ist),
        reference_id=reference[0], reference_expiry=reference[1], option_expiry=current.expiry,
        ce_token=raw.ce_token, pe_token=raw.pe_token, hypothesis=config.hypothesis_type,
        max_gap_seconds=config.max_sequence_gap_seconds, validity=validity)
    quality = (AlphaEvidenceQuality.INSUFFICIENT if validity == AlphaValidity.INVALID else assess_quality(
        alpha1_available=rank1 is not None, alpha2_available=rank2 is not None,
        atm_available=hard_atm_valid, volume_available=raw.activity is not None,
        volatility_available=raw.underlying_volatility is not None,
        sequence_continuous=raw.sequence_continuous,
        rank_count=min(len(price_rows), len(standardized_rows)), minimum_rank=config.min_rank_observations))
    if validity != AlphaValidity.VALID and quality in {AlphaEvidenceQuality.HIGH, AlphaEvidenceQuality.MEDIUM}:
        quality = AlphaEvidenceQuality.LOW
    if raw.participation == ParticipationState.LOW and quality == AlphaEvidenceQuality.HIGH:
        quality = AlphaEvidenceQuality.MEDIUM
    warnings = list(raw.validity_reasons)
    if rank1 is None or rank2 is None:
        warnings.append("INSUFFICIENT_ALPHA_HISTORY")
    if raw.standardized_return is None:
        warnings.append("VOLATILITY_TOO_SMALL" if raw.underlying_volatility_count >= config.min_volatility_returns
                        else "UNDERLYING_VOLATILITY_INSUFFICIENT")
    if not hard_atm_valid:
        warnings.append("ATM_CONTRACT_CONTINUITY_INVALID")
    if raw.volume_reset:
        warnings.append("ALPHA_VOLUME_RESET_DETECTED")
    if raw.activity_capped:
        warnings.append("ACTIVITY_RATIO_CAPPED")
    if rank_sign_conflict(rank1, raw.price_return, config) or rank_sign_conflict(rank2, raw.price_return, config):
        warnings.append("RANK_SIGN_CONFLICT")
    if joint == JointAlphaDirection.CONFLICT:
        warnings.append("ALPHA_SIGNAL_CONFLICT")
    if current.source_market_timestamp is None:
        warnings.append("SOURCE_MARKET_TIMESTAMP_UNAVAILABLE")
    status = (AlphaStatus.WARMING_UP if quality == AlphaEvidenceQuality.INSUFFICIENT else
              AlphaStatus.READY if validity == AlphaValidity.VALID and quality in {
                  AlphaEvidenceQuality.HIGH, AlphaEvidenceQuality.MEDIUM} else AlphaStatus.PARTIAL)
    price_points = [point for point, _, _ in price_rows]
    eligible_history = _window(history, current, config.alpha1_lookback_minutes,
                               config.lookback_clock_mode, config.holiday_dates)
    coverage, sessions, observations = window_coverage(eligible_history, current.timestamp_ist,
                                                       config.lookback_clock_mode,
                                                       holidays=config.holiday_dates)
    trading_coverage, rank_sessions, _ = window_coverage(price_points, current.timestamp_ist,
                                                          LookbackClockMode.TRADING_MINUTES,
                                                          holidays=config.holiday_dates)
    legacy_rows = _historical_raw_values(history, current, config.alpha2_lookback_minutes,
                                         config, lambda item: item.impulse, raw_cache)
    legacy_rank = None if raw.impulse is None else percentile_rank(
        raw.impulse, [value for _, _, value in legacy_rows], config.min_rank_observations)
    strength = 0 if joint in {JointAlphaDirection.CONFLICT, JointAlphaDirection.INSUFFICIENT_DATA,
                              JointAlphaDirection.NEUTRAL} else max(abs(rank1 - 0.5), abs(rank2 - 0.5)) * 2
    return AlphaFeatureSnapshot(
        snapshot_id=snapshot_id, timestamp=current.timestamp_ist, expiry=current.expiry,
        price_source=config.price_source, price_return=raw.price_return, alpha_1=rank1,
        atm_strike=raw.atm_strike, atm_ce_token=raw.ce_token, atm_pe_token=raw.pe_token,
        atm_ce_interval_volume=raw.ce_interval, atm_pe_interval_volume=raw.pe_interval,
        ce_volume_baseline=raw.ce_baseline, pe_volume_baseline=raw.pe_baseline,
        ce_volume_ratio=raw.ce_ratio, pe_volume_ratio=raw.pe_ratio,
        atm_volume_activity=raw.activity, put_call_interval_volume_ratio=raw.put_call_ratio,
        signed_interval_volume_imbalance=raw.imbalance, ce_observed_volatility=raw.ce_volatility,
        pe_observed_volatility=raw.pe_volatility, atm_option_volatility=raw.option_volatility,
        directional_impulse_raw=raw.impulse, alpha_2=rank2, alpha_1_direction=first,
        alpha_2_direction=second, joint_alpha_direction=joint,
        consecutive_confirmation_count=count, confirmed=count >= config.required_consecutive_confirmations,
        evidence_quality=quality, status=status, rank_observations_alpha1=len(price_rows),
        rank_observations_alpha2=len(standardized_rows), volume_baseline_observations=raw.volume_observations,
        volatility_return_observations=raw.volatility_observations, warnings=sorted(set(warnings)),
        session_date=session_date(current.timestamp_ist), session_id=session_id(current.timestamp_ist),
        hypothesis_type=config.hypothesis_type, calculation_mode=calculation_mode,
        lookback_clock_mode=config.lookback_clock_mode.value, effective_history_minutes=coverage,
        history_session_count=sessions, history_observation_count=observations,
        target_horizon_seconds=config.price_horizon_seconds,
        actual_horizon_seconds=raw.horizon.actual_seconds if raw.horizon else None,
        horizon_error_seconds=raw.horizon.error_seconds if raw.horizon else None,
        signed_log_return=raw.price_return, legacy_open_normalized_return=raw.legacy_open_return,
        rank_history_count=len(price_rows), rank_history_trading_minutes=trading_coverage,
        rank_history_sessions=rank_sessions,
        reference_instrument_id=raw.horizon.reference_instrument_id if raw.horizon else None,
        reference_expiry=raw.horizon.reference_expiry if raw.horizon else None,
        reference_source_timestamp=raw.horizon.reference_source_timestamp if raw.horizon else None,
        reference_age_seconds=raw.horizon.reference_age_seconds if raw.horizon else None,
        ce_volume_state=raw.ce_state.value, pe_volume_state=raw.pe_state.value,
        ce_interval_duration_seconds=raw.ce_duration, pe_interval_duration_seconds=raw.pe_duration,
        ce_gap_cumulative_delta=raw.ce_gap_delta, pe_gap_cumulative_delta=raw.pe_gap_delta,
        atm_changed_since_previous_snapshot=raw.atm_changed, atm_ce_history_count=raw.ce_history_count,
        atm_pe_history_count=raw.pe_history_count, atm_distance_points=raw.atm_distance_points,
        atm_distance_percent=raw.atm_distance_percent, activity_score=raw.activity,
        volatility_context_score=raw.underlying_volatility, participation_state=raw.participation,
        activity_baseline_method=config.activity_baseline_method, activity_baseline_count=raw.volume_observations,
        activity_ratio_capped=raw.activity_capped, underlying_horizon_volatility=raw.underlying_volatility,
        standardized_return=raw.standardized_return, volatility_observation_count=raw.underlying_volatility_count,
        volatility_coverage_minutes=raw.volatility_coverage_minutes,
        legacy_alpha2_raw=raw.impulse, legacy_alpha2_rank=legacy_rank,
        validity_state=validity, signal_strength=strength,
        confirmation_reset_reason=reset,
        broker_oi_current_ce=raw.broker_oi_ce[0], broker_oi_previous_ce=raw.broker_oi_ce[1],
        broker_oi_change_ce=raw.broker_oi_ce[2], local_oi_change_ce=raw.local_oi_ce,
        broker_oi_current_pe=raw.broker_oi_pe[0], broker_oi_previous_pe=raw.broker_oi_pe[1],
        broker_oi_change_pe=raw.broker_oi_pe[2], local_oi_change_pe=raw.local_oi_pe,
        source_market_timestamp=current.source_market_timestamp,
        request_started_at=current.request_started_at,
        response_received_at=current.response_received_at,
        snapshot_persisted_at=current.snapshot_persisted_at,
        feature_calculated_at=datetime.now(current.timestamp_ist.tzinfo),
    )


def config_from_settings(settings) -> AlphaConfig:
    return AlphaConfig(
        price_source=settings.alpha_price_source,
        price_horizon_seconds=settings.alpha_price_horizon_seconds,
        horizon_tolerance_seconds=settings.alpha_horizon_tolerance_seconds,
        min_horizon_seconds=settings.alpha_min_horizon_seconds,
        max_horizon_seconds=settings.alpha_max_horizon_seconds,
        lookback_clock_mode=LookbackClockMode(settings.alpha_lookback_clock_mode),
        holiday_dates=settings.configured_holidays,
        hypothesis_type=AlphaHypothesis(settings.alpha_hypothesis_type),
        alpha1_lookback_minutes=settings.alpha1_lookback_minutes,
        alpha2_lookback_minutes=settings.alpha2_lookback_minutes,
        volume_lookback_minutes=settings.alpha_volume_lookback_minutes,
        volatility_lookback_minutes=settings.alpha_volatility_lookback_minutes,
        min_rank_observations=settings.alpha_min_rank_observations,
        min_volume_observations=settings.alpha_min_volume_observations,
        min_volatility_returns=settings.alpha_min_volatility_returns,
        max_sequence_gap_seconds=settings.alpha_max_sequence_gap_seconds,
        volume_max_interval_seconds=settings.volume_max_interval_seconds,
        max_source_age_seconds=settings.alpha_stale_after_seconds,
        volatility_min_valid_level=settings.volatility_min_valid_level,
        activity_baseline_method=settings.alpha_activity_baseline_method,
        activity_min_baseline=settings.alpha_activity_min_baseline,
        activity_ratio_cap=settings.alpha_activity_ratio_cap,
        atm_max_distance_percent=settings.alpha_atm_max_distance_percent,
        volatility_epsilon=settings.alpha_volatility_epsilon,
        strong_upper=settings.alpha_strong_upper,
        strong_lower=settings.alpha_strong_lower,
        moderate_upper=settings.alpha_moderate_upper,
        moderate_lower=settings.alpha_moderate_lower,
        required_consecutive_confirmations=settings.alpha_required_consecutive_confirmations,
    )
