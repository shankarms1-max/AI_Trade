from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

FEATURE_VERSION = "phase3_v1"


class FeatureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeatureDataQuality(FeatureModel):
    static_oi_usable: bool | None = None
    static_oi_coverage: float | None = None
    broker_oi_change_available: bool | None = None
    broker_oi_change_nonzero: bool | None = None
    local_delta_oi_usable: bool | None = None
    local_delta_oi_reason: str | None = None
    contracts_total: int
    calls_total: int
    puts_total: int
    contracts_with_ltp: int
    contracts_with_open_interest: int
    contracts_with_previous_open_interest: int
    contracts_with_oi_change: int
    contracts_with_volume: int
    contracts_nonzero_volume: int
    contracts_nonzero_oi_change: int
    calls_with_nonzero_oi_change: int
    puts_with_nonzero_oi_change: int
    oi_mismatch_count: int
    vix_available: bool
    future_available: bool
    intraday_oi_usable: bool
    intraday_volume_usable: bool


class ContractFeature(FeatureModel):
    strike: float
    option_type: Literal["CE", "PE"]
    open_interest: int | None
    previous_open_interest: int | None
    change_in_open_interest: int | None
    volume: int | None
    ltp: float | None
    distance_from_spot: float
    distance_from_atm: float
    moneyness: Literal["ITM", "ATM", "OTM"]
    price_change: float | None = None
    price_change_pct: float | None = None
    oi_change: int | None = None
    positioning_class: Literal[
        "LONG_BUILDUP",
        "SHORT_BUILDUP",
        "LONG_UNWINDING",
        "SHORT_COVERING",
        "INSUFFICIENT_DATA",
    ] = "INSUFFICIENT_DATA"


class OIFeatures(FeatureModel):
    contracts: list[ContractFeature]
    top_call_oi: list[ContractFeature]
    top_put_oi: list[ContractFeature]
    top_call_oi_additions: list[ContractFeature] | None
    top_put_oi_additions: list[ContractFeature] | None
    top_call_oi_reductions: list[ContractFeature] | None
    top_put_oi_reductions: list[ContractFeature] | None


class PCRFeatures(FeatureModel):
    scope: Literal["LOCAL_WINDOW_PCR"] = "LOCAL_WINDOW_PCR"
    put_oi_total: int
    call_oi_total: int
    put_oi_change_total: int | None
    call_oi_change_total: int | None
    local_pcr_oi: float | None
    local_pcr_oi_change: float | None


class SideConcentration(FeatureModel):
    top_1_share: float | None
    top_3_share: float | None
    top_5_share: float | None
    weighted_average_strike_by_oi: float | None


class OIConcentration(FeatureModel):
    call: SideConcentration
    put: SideConcentration


class OICluster(FeatureModel):
    side: Literal["POTENTIAL_SUPPORT_CLUSTER", "POTENTIAL_RESISTANCE_CLUSTER", "PUT_OI_ADDITION_CLUSTER", "CALL_OI_ADDITION_CLUSTER"]
    low_strike: float
    high_strike: float
    center_strike: float
    total_oi: int
    share_of_side_oi: float
    max_oi_strike: float
    max_oi: int
    distance_from_spot: float
    strength_score: float


class SupportResistanceFeatures(FeatureModel):
    inferred_strike_step: float | None
    potential_support_clusters: list[OICluster]
    potential_resistance_clusters: list[OICluster]
    put_oi_addition_clusters: list[OICluster] | None
    call_oi_addition_clusters: list[OICluster] | None


class FuturesFeatures(FeatureModel):
    future_available: bool
    futures_basis: float | None
    futures_basis_pct: float | None


class VolatilityFeatures(FeatureModel):
    india_vix: float | None
    vix_available: bool
    vix_regime: Literal["LOW", "NORMAL", "ELEVATED", "HIGH"] | None


class PriceStructureFeatures(FeatureModel):
    spot_change_from_previous_snapshot: float | None
    spot_change_pct_from_previous_snapshot: float | None
    future_change_from_previous_snapshot: float | None
    future_change_pct_from_previous_snapshot: float | None
    distance_from_atm: float
    distance_to_nearest_potential_support_cluster: float | None
    distance_to_nearest_potential_resistance_cluster: float | None
    session_open_proxy: float
    collector_observed_high: float
    collector_observed_low: float
    observed_opening_range_high: float | None
    observed_opening_range_low: float | None
    observed_opening_range_complete: bool
    vwap: None = None
    vwap_available: Literal[False] = False


class MarketFeatureSnapshot(FeatureModel):
    snapshot_id: int
    timestamp: datetime
    expiry: date
    spot: float
    future: float | None
    atm_strike: float
    data_quality: FeatureDataQuality
    oi_features: OIFeatures
    pcr_features: PCRFeatures
    oi_concentration: OIConcentration
    support_resistance: SupportResistanceFeatures
    futures_features: FuturesFeatures
    volatility_features: VolatilityFeatures
    price_structure_features: PriceStructureFeatures
    feature_version: str = FEATURE_VERSION
