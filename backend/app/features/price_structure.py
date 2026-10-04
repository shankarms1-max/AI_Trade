from datetime import time
from zoneinfo import ZoneInfo

from app.data.models import MarketSnapshot
from app.features.models import OICluster, PriceStructureFeatures

IST = ZoneInfo("Asia/Kolkata")


def _change(current: float | None, prior: float | None) -> tuple[float | None, float | None]:
    if current is None or prior in (None, 0):
        return None, None
    value = current - prior
    return value, value / prior * 100


def calculate_price_structure(
    snapshot: MarketSnapshot,
    same_day_history: list[MarketSnapshot],
    support: list[OICluster],
    resistance: list[OICluster],
    *,
    opening_start: time,
    opening_end: time,
    opening_min_samples: int,
) -> PriceStructureFeatures:
    earlier = sorted(
        (item for item in same_day_history if item.timestamp_ist < snapshot.timestamp_ist),
        key=lambda item: item.timestamp_ist,
    )
    previous = earlier[-1] if earlier else None
    spot_change, spot_pct = _change(
        snapshot.nifty_spot, previous.nifty_spot if previous else None
    )
    future_change, future_pct = _change(
        snapshot.nifty_future, previous.nifty_future if previous else None
    )
    observed = earlier + [snapshot]
    spots = [item.nifty_spot for item in observed]
    opening = [
        item
        for item in observed
        if opening_start
        <= item.timestamp_ist.astimezone(IST).time().replace(tzinfo=None)
        <= opening_end
    ]
    opening_spots = [item.nifty_spot for item in opening]
    complete = bool(
        len(opening) >= opening_min_samples
        and opening[0].timestamp_ist.astimezone(IST).time().replace(tzinfo=None) <= opening_start
        and opening[-1].timestamp_ist.astimezone(IST).time().replace(tzinfo=None) >= opening_end
    )
    return PriceStructureFeatures(
        spot_change_from_previous_snapshot=spot_change,
        spot_change_pct_from_previous_snapshot=spot_pct,
        future_change_from_previous_snapshot=future_change,
        future_change_pct_from_previous_snapshot=future_pct,
        distance_from_atm=snapshot.nifty_spot - snapshot.atm_strike,
        distance_to_nearest_potential_support_cluster=(
            min(abs(item.center_strike - snapshot.nifty_spot) for item in support)
            if support else None
        ),
        distance_to_nearest_potential_resistance_cluster=(
            min(abs(item.center_strike - snapshot.nifty_spot) for item in resistance)
            if resistance else None
        ),
        session_open_proxy=observed[0].nifty_spot,
        collector_observed_high=max(spots),
        collector_observed_low=min(spots),
        observed_opening_range_high=max(opening_spots) if opening_spots else None,
        observed_opening_range_low=min(opening_spots) if opening_spots else None,
        observed_opening_range_complete=complete,
    )
