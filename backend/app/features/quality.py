from app.data.models import MarketSnapshot
from app.features.models import FeatureDataQuality


def calculate_data_quality(
    snapshot: MarketSnapshot, minimum_coverage: float
) -> FeatureDataQuality:
    options = snapshot.options
    total = len(options)
    oi_change_present = sum(item.change_in_open_interest is not None for item in options)
    volume_present = sum(item.volume is not None for item in options)
    nonzero_change = sum(bool(item.change_in_open_interest) for item in options)
    nonzero_volume = sum(bool(item.volume) for item in options)
    coverage_denominator = total or 1
    mismatch = sum(
        item.open_interest is not None
        and item.previous_open_interest is not None
        and item.change_in_open_interest is not None
        and item.open_interest - item.previous_open_interest != item.change_in_open_interest
        for item in options
    )
    return FeatureDataQuality(
        contracts_total=total,
        calls_total=sum(item.option_type.value == "CE" for item in options),
        puts_total=sum(item.option_type.value == "PE" for item in options),
        contracts_with_ltp=sum(item.ltp is not None for item in options),
        contracts_with_open_interest=sum(item.open_interest is not None for item in options),
        contracts_with_previous_open_interest=sum(
            item.previous_open_interest is not None for item in options
        ),
        contracts_with_oi_change=oi_change_present,
        contracts_with_volume=volume_present,
        contracts_nonzero_volume=nonzero_volume,
        contracts_nonzero_oi_change=nonzero_change,
        calls_with_nonzero_oi_change=sum(
            item.option_type.value == "CE" and bool(item.change_in_open_interest)
            for item in options
        ),
        puts_with_nonzero_oi_change=sum(
            item.option_type.value == "PE" and bool(item.change_in_open_interest)
            for item in options
        ),
        oi_mismatch_count=mismatch,
        vix_available=snapshot.india_vix is not None,
        future_available=snapshot.nifty_future is not None,
        intraday_oi_usable=(
            oi_change_present / coverage_denominator >= minimum_coverage
            and nonzero_change > 0
        ),
        intraday_volume_usable=(
            volume_present / coverage_denominator >= minimum_coverage
            and nonzero_volume > 0
        ),
    )
