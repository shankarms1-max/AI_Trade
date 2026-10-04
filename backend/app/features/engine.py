from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from app.data.models import MarketSnapshot
from app.features.clusters import build_clusters, infer_strike_step
from app.features.futures import calculate_futures_features
from app.features.models import MarketFeatureSnapshot, SupportResistanceFeatures
from app.features.oi import build_oi_features, calculate_concentration
from app.features.pcr import calculate_pcr
from app.features.price_structure import calculate_price_structure
from app.features.quality import calculate_data_quality
from app.features.volatility import calculate_volatility_features

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class FeatureEngineConfig:
    top_n: int = 5
    minimum_data_coverage: float = 0.6
    cluster_maximum_fraction: float = 0.6
    opening_range_start: time = time(9, 15)
    opening_range_end: time = time(9, 30)
    opening_range_min_samples: int = 4
    vix_low: float = 12
    vix_elevated: float = 16
    vix_high: float = 22


def build_market_features(
    snapshot_id: int,
    snapshot: MarketSnapshot,
    history: list[MarketSnapshot],
    config: FeatureEngineConfig = FeatureEngineConfig(),
) -> MarketFeatureSnapshot:
    quality = calculate_data_quality(snapshot, config.minimum_data_coverage)
    prior_comparable = next(
        (
            item
            for item in sorted(history, key=lambda value: value.timestamp_ist, reverse=True)
            if item.timestamp_ist < snapshot.timestamp_ist and item.expiry == snapshot.expiry
        ),
        None,
    )
    oi = build_oi_features(
        snapshot,
        prior_comparable,
        top_n=config.top_n,
        change_usable=quality.intraday_oi_usable,
    )
    step = infer_strike_step(oi.contracts)
    cluster_args = {
        "rows": oi.contracts,
        "spot": snapshot.nifty_spot,
        "strike_step": step,
        "maximum_fraction": config.cluster_maximum_fraction,
    }
    support = build_clusters(
        **cluster_args,
        option_type="PE",
        metric=lambda row: row.open_interest or 0,
        label="POTENTIAL_SUPPORT_CLUSTER",
    )
    resistance = build_clusters(
        **cluster_args,
        option_type="CE",
        metric=lambda row: row.open_interest or 0,
        label="POTENTIAL_RESISTANCE_CLUSTER",
    )
    put_additions = call_additions = None
    if quality.intraday_oi_usable:
        put_additions = build_clusters(
            **cluster_args,
            option_type="PE",
            metric=lambda row: max(0, row.change_in_open_interest or 0),
            label="PUT_OI_ADDITION_CLUSTER",
        )
        call_additions = build_clusters(
            **cluster_args,
            option_type="CE",
            metric=lambda row: max(0, row.change_in_open_interest or 0),
            label="CALL_OI_ADDITION_CLUSTER",
        )
    same_day = [
        item
        for item in history
        if item.timestamp_ist.astimezone(IST).date()
        == snapshot.timestamp_ist.astimezone(IST).date()
    ]
    return MarketFeatureSnapshot(
        snapshot_id=snapshot_id,
        timestamp=snapshot.timestamp_ist,
        expiry=snapshot.expiry,
        spot=snapshot.nifty_spot,
        future=snapshot.nifty_future,
        atm_strike=snapshot.atm_strike,
        data_quality=quality,
        oi_features=oi,
        pcr_features=calculate_pcr(oi, quality.intraday_oi_usable),
        oi_concentration=calculate_concentration(oi),
        support_resistance=SupportResistanceFeatures(
            inferred_strike_step=step,
            potential_support_clusters=support,
            potential_resistance_clusters=resistance,
            put_oi_addition_clusters=put_additions,
            call_oi_addition_clusters=call_additions,
        ),
        futures_features=calculate_futures_features(
            snapshot.nifty_spot, snapshot.nifty_future
        ),
        volatility_features=calculate_volatility_features(
            snapshot.india_vix,
            low=config.vix_low,
            elevated=config.vix_elevated,
            high=config.vix_high,
        ),
        price_structure_features=calculate_price_structure(
            snapshot,
            same_day,
            support,
            resistance,
            opening_start=config.opening_range_start,
            opening_end=config.opening_range_end,
            opening_min_samples=config.opening_range_min_samples,
        ),
    )


def config_from_settings(settings) -> FeatureEngineConfig:
    return FeatureEngineConfig(
        top_n=settings.feature_top_n,
        minimum_data_coverage=settings.feature_min_data_coverage,
        cluster_maximum_fraction=settings.feature_cluster_max_fraction,
        opening_range_start=settings.feature_opening_range_start,
        opening_range_end=settings.feature_opening_range_end,
        opening_range_min_samples=settings.feature_opening_range_min_samples,
        vix_low=settings.feature_vix_low,
        vix_elevated=settings.feature_vix_elevated,
        vix_high=settings.feature_vix_high,
    )
