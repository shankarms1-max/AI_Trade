from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.features.clusters import build_clusters, infer_strike_step
from app.features.engine import FeatureEngineConfig, build_market_features
from app.features.futures import calculate_futures_features
from app.features.oi import build_oi_features, calculate_concentration
from app.features.pcr import calculate_pcr
from app.features.quality import calculate_data_quality
from app.features.volatility import calculate_volatility_features

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2099, 10, 8)


def option(
    strike: float,
    kind: str,
    *,
    ltp: float | None = 100,
    oi: int | None = 1000,
    previous_oi: int | None = 900,
    change: int | None = 100,
    volume: int | None = 500,
) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        strike=strike,
        option_type=kind,
        expiry=EXPIRY,
        trading_symbol=f"NIFTY-{strike}-{kind}",
        ltp=ltp,
        open_interest=oi,
        previous_open_interest=previous_oi,
        change_in_open_interest=change,
        volume=volume,
    )


def snapshot(
    options: list[OptionContractSnapshot],
    *,
    timestamp: datetime | None = None,
    spot: float = 25000,
    future: float | None = 25025,
    vix: float | None = 14,
    expiry: date = EXPIRY,
) -> MarketSnapshot:
    return MarketSnapshot(
        timestamp_ist=timestamp or datetime(2099, 10, 1, 10, 0, tzinfo=IST),
        nifty_spot=spot,
        nifty_future=future,
        india_vix=vix,
        atm_strike=25000,
        expiry=expiry,
        options=options,
    )


def representative() -> MarketSnapshot:
    return snapshot(
        [
            option(24950, "CE", oi=600, change=60),
            option(25000, "CE", oi=1000, change=100),
            option(25050, "CE", oi=700, change=-50),
            option(24950, "PE", oi=800, change=-80),
            option(25000, "PE", oi=1200, change=120),
            option(25050, "PE", oi=900, change=90),
        ]
    )


def test_complete_data_quality_is_usable() -> None:
    quality = calculate_data_quality(representative(), 0.6)
    assert quality.contracts_total == 6
    assert quality.calls_total == quality.puts_total == 3
    assert quality.intraday_oi_usable is True
    assert quality.intraday_volume_usable is True


@pytest.mark.parametrize(
    ("field", "value", "oi_usable", "volume_usable"),
    [
        ("change_in_open_interest", None, False, True),
        ("change_in_open_interest", 0, False, True),
        ("volume", None, True, False),
        ("volume", 0, True, False),
    ],
)
def test_missing_or_zero_intraday_fields_are_not_claimed_usable(
    field: str, value, oi_usable: bool, volume_usable: bool
) -> None:
    item = option(25000, "CE").model_copy(update={field: value})
    quality = calculate_data_quality(snapshot([item]), 0.6)
    assert quality.intraday_oi_usable is oi_usable
    assert quality.intraday_volume_usable is volume_usable


def test_missing_vix_and_future_are_explicit() -> None:
    quality = calculate_data_quality(snapshot([option(25000, "CE")], vix=None, future=None), 0.6)
    assert quality.vix_available is False
    assert quality.future_available is False


def test_oi_rankings_additions_reductions_and_concentration() -> None:
    current = representative()
    features = build_oi_features(current, None, top_n=2, change_usable=True)
    assert [row.strike for row in features.top_call_oi] == [25000, 25050]
    assert [row.strike for row in features.top_put_oi] == [25000, 25050]
    assert features.top_call_oi_additions[0].strike == 25000  # type: ignore[index]
    assert features.top_put_oi_reductions[0].strike == 24950  # type: ignore[index]
    concentration = calculate_concentration(features)
    assert concentration.call.top_1_share == pytest.approx(1000 / 2300)
    assert concentration.call.top_3_share == 1
    assert concentration.call.top_5_share == 1
    assert concentration.put.weighted_average_strike_by_oi == pytest.approx(
        (24950 * 800 + 25000 * 1200 + 25050 * 900) / 2900
    )


@pytest.mark.parametrize(
    ("prior_ltp", "current_ltp", "oi_change", "expected"),
    [
        (100, 110, 10, "LONG_BUILDUP"),
        (100, 90, 10, "SHORT_BUILDUP"),
        (100, 90, -10, "LONG_UNWINDING"),
        (100, 110, -10, "SHORT_COVERING"),
    ],
)
def test_price_oi_position_classification(
    prior_ltp: float, current_ltp: float, oi_change: int, expected: str
) -> None:
    prior = snapshot([option(25000, "CE", ltp=prior_ltp)])
    current = snapshot(
        [option(25000, "CE", ltp=current_ltp, change=oi_change)],
        timestamp=prior.timestamp_ist + timedelta(minutes=3),
    )
    row = build_oi_features(current, prior, top_n=5, change_usable=True).contracts[0]
    assert row.positioning_class == expected
    assert row.price_change == current_ltp - prior_ltp


def test_positioning_requires_prior_same_expiry() -> None:
    current = representative()
    assert build_oi_features(current, None, top_n=5, change_usable=True).contracts[0].positioning_class == "INSUFFICIENT_DATA"
    other_expiry = date(2099, 10, 15)
    prior = snapshot(
        [option(25000, "CE").model_copy(update={"expiry": other_expiry})],
        expiry=other_expiry,
    )
    features = build_market_features(1, current, [prior])
    assert all(row.positioning_class == "INSUFFICIENT_DATA" for row in features.oi_features.contracts)


def test_local_window_pcr_and_zero_denominator() -> None:
    oi = build_oi_features(representative(), None, top_n=5, change_usable=True)
    pcr = calculate_pcr(oi, True)
    assert pcr.scope == "LOCAL_WINDOW_PCR"
    assert pcr.local_pcr_oi == pytest.approx(2900 / 2300)
    assert pcr.local_pcr_oi_change == pytest.approx(130 / 110)
    zero = snapshot([option(25000, "CE", oi=0), option(25000, "PE", oi=10)])
    zero_pcr = calculate_pcr(build_oi_features(zero, None, top_n=5, change_usable=True), True)
    assert zero_pcr.local_pcr_oi is None


def test_oi_change_pcr_unavailable_when_quality_insufficient() -> None:
    current = snapshot([option(25000, "CE", change=0), option(25000, "PE", change=0)])
    quality = calculate_data_quality(current, 0.6)
    pcr = calculate_pcr(build_oi_features(current, None, top_n=5, change_usable=False), False)
    assert quality.intraday_oi_usable is False
    assert pcr.local_pcr_oi_change is None
    assert pcr.call_oi_change_total is None


def test_cluster_detection_merging_step_and_strength_are_deterministic() -> None:
    current = representative()
    rows = build_oi_features(current, None, top_n=5, change_usable=True).contracts
    step = infer_strike_step(rows)
    clusters = build_clusters(
        rows,
        option_type="PE",
        spot=25000,
        strike_step=step,
        maximum_fraction=0.6,
        metric=lambda row: row.open_interest or 0,
        label="POTENTIAL_SUPPORT_CLUSTER",
    )
    assert step == 50
    assert len(clusters) == 1
    assert clusters[0].low_strike == 24950
    assert clusters[0].high_strike == 25050
    assert clusters[0].strength_score == pytest.approx(clusters[0].strength_score)
    full = build_market_features(1, current, [])
    assert full.support_resistance.put_oi_addition_clusters
    assert full.support_resistance.call_oi_addition_clusters


def test_isolated_high_oi_strike_forms_single_strike_cluster() -> None:
    current = snapshot([
        option(24950, "CE", oi=10), option(25000, "CE", oi=10), option(25050, "CE", oi=100)
    ])
    features = build_market_features(1, current, [])
    cluster = features.support_resistance.potential_resistance_clusters[0]
    assert cluster.low_strike == cluster.high_strike == 25050


def test_futures_basis_and_missing_future() -> None:
    available = calculate_futures_features(25000, 25025)
    assert available.futures_basis == 25
    assert available.futures_basis_pct == pytest.approx(0.1)
    missing = calculate_futures_features(25000, None)
    assert missing.future_available is False and missing.futures_basis is None


@pytest.mark.parametrize(
    ("vix", "regime"),
    [(11.9, "LOW"), (12, "NORMAL"), (16, "ELEVATED"), (22, "HIGH"), (None, None)],
)
def test_vix_research_buckets(vix: float | None, regime: str | None) -> None:
    result = calculate_volatility_features(vix, low=12, elevated=16, high=22)
    assert result.vix_regime == regime
    assert result.vix_available is (vix is not None)


def test_price_structure_prior_session_range_and_partial_opening_range() -> None:
    start = datetime(2099, 10, 1, 9, 18, tzinfo=IST)
    prior = snapshot([option(25000, "CE")], timestamp=start, spot=24990, future=25010)
    current = snapshot(
        [option(25000, "CE", ltp=101)], timestamp=start + timedelta(minutes=3), spot=25010, future=25035
    )
    result = build_market_features(2, current, [prior]).price_structure_features
    assert result.spot_change_from_previous_snapshot == 20
    assert result.future_change_from_previous_snapshot == 25
    assert result.session_open_proxy == 24990
    assert result.collector_observed_high == 25010
    assert result.collector_observed_low == 24990
    assert result.observed_opening_range_complete is False
    assert result.vwap is None and result.vwap_available is False


def test_observed_opening_range_complete_requires_boundary_coverage() -> None:
    start = datetime(2099, 10, 1, 9, 15, tzinfo=IST)
    history = [
        snapshot([option(25000, "CE")], timestamp=start + timedelta(minutes=offset), spot=25000 + offset)
        for offset in (0, 5, 10)
    ]
    current = snapshot([option(25000, "CE")], timestamp=start + timedelta(minutes=15), spot=25015)
    config = FeatureEngineConfig(
        opening_range_start=time(9, 15), opening_range_end=time(9, 30), opening_range_min_samples=4
    )
    result = build_market_features(4, current, history, config).price_structure_features
    assert result.observed_opening_range_complete is True
    assert result.observed_opening_range_low == 25000
    assert result.observed_opening_range_high == 25015
