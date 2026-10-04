"""Session- and contract-bound cumulative option-volume intervals."""

from dataclasses import dataclass
from enum import Enum
from statistics import median, mean

from app.alpha.atm import matching_contract
from app.alpha.clock import session_id
from app.data.models import MarketSnapshot, OptionContractSnapshot


class VolumeState(str, Enum):
    VALID_INTERVAL = "VALID_INTERVAL"
    VALID_ZERO = "VALID_ZERO"
    SESSION_BASELINE_ONLY = "SESSION_BASELINE_ONLY"
    MISSING = "MISSING"
    STALE = "STALE"
    RESET = "RESET"
    CORRECTED = "CORRECTED"
    GAP_VOLUME_INTERVAL = "GAP_VOLUME_INTERVAL"
    CONTRACT_CHANGED = "CONTRACT_CHANGED"


@dataclass(frozen=True)
class IntervalVolume:
    value: int | None
    state: VolumeState
    duration_seconds: float | None = None
    gap_cumulative_delta: int | None = None

    @property
    def reset(self) -> bool:
        return self.state in {VolumeState.RESET, VolumeState.CORRECTED}


def interval_volume(current_contract: OptionContractSnapshot,
                    previous_snapshot: MarketSnapshot | None,
                    current_snapshot: MarketSnapshot | None = None,
                    max_interval_seconds: int = 420,
                    max_source_age_seconds: int = 600) -> IntervalVolume:
    if current_snapshot is None:
        return IntervalVolume(None, VolumeState.MISSING)
    if current_contract.volume is None or current_contract.volume < 0:
        return IntervalVolume(None, VolumeState.MISSING)
    if current_contract.source_market_timestamp is not None:
        age = (current_snapshot.timestamp_ist - current_contract.source_market_timestamp).total_seconds()
        if age < 0 or age > max_source_age_seconds:
            return IntervalVolume(None, VolumeState.STALE)
    if previous_snapshot is None or session_id(previous_snapshot.timestamp_ist) != session_id(current_snapshot.timestamp_ist):
        return IntervalVolume(None, VolumeState.SESSION_BASELINE_ONLY)
    duration = (current_snapshot.timestamp_ist - previous_snapshot.timestamp_ist).total_seconds()
    if duration <= 0:
        return IntervalVolume(None, VolumeState.CORRECTED, duration)
    previous = matching_contract(previous_snapshot, current_contract)
    if previous is None:
        return IntervalVolume(None, VolumeState.CONTRACT_CHANGED, duration)
    if previous.volume is None or previous.volume < 0:
        return IntervalVolume(None, VolumeState.MISSING, duration)
    if previous.source_market_timestamp is not None:
        age = (previous_snapshot.timestamp_ist - previous.source_market_timestamp).total_seconds()
        if age < 0 or age > max_source_age_seconds:
            return IntervalVolume(None, VolumeState.STALE, duration)
    delta = current_contract.volume - previous.volume
    if delta < 0:
        return IntervalVolume(None, VolumeState.RESET, duration)
    if duration > max_interval_seconds:
        return IntervalVolume(None, VolumeState.GAP_VOLUME_INTERVAL, duration, delta)
    return IntervalVolume(delta, VolumeState.VALID_ZERO if delta == 0 else VolumeState.VALID_INTERVAL,
                          duration)


def volume_ratio(current: int | None, baseline: list[int], *, method: str = "MEDIAN",
                 minimum_baseline: float = 1.0, cap: float = 10.0) -> tuple[float | None, float | None]:
    valid = [value for value in baseline if value >= 0]
    if current is None or not valid:
        return None, None
    if method == "MEDIAN":
        typical = float(median(valid))
    elif method == "TRIMMED_MEAN":
        ordered = sorted(valid)
        trim = len(ordered) // 10
        typical = float(mean(ordered[trim:len(ordered)-trim] if trim else ordered))
    else:
        raise ValueError("invalid activity baseline method")
    if typical < minimum_baseline:
        return None, typical
    return min(current / typical, cap), typical


def combined_activity(ce_ratio: float | None, pe_ratio: float | None) -> float | None:
    return None if ce_ratio is None or pe_ratio is None else (ce_ratio + pe_ratio) / 2


def directional_volume_metrics(ce: int | None, pe: int | None) -> tuple[float | None, float | None]:
    """Research context only; neither statistic is a trade-side/direction inference."""
    if ce is None or pe is None:
        return None, None
    ratio = None if ce == 0 else pe / ce
    total = ce + pe
    return ratio, None if total <= 0 else (ce - pe) / total
