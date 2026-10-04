from app.features.models import OIFeatures, PCRFeatures


def calculate_pcr(features: OIFeatures, change_usable: bool) -> PCRFeatures:
    calls = [row for row in features.contracts if row.option_type == "CE"]
    puts = [row for row in features.contracts if row.option_type == "PE"]
    call_oi = sum(row.open_interest or 0 for row in calls)
    put_oi = sum(row.open_interest or 0 for row in puts)
    call_change = sum(row.change_in_open_interest or 0 for row in calls) if change_usable else None
    put_change = sum(row.change_in_open_interest or 0 for row in puts) if change_usable else None
    return PCRFeatures(
        put_oi_total=put_oi,
        call_oi_total=call_oi,
        put_oi_change_total=put_change,
        call_oi_change_total=call_change,
        local_pcr_oi=put_oi / call_oi if call_oi else None,
        local_pcr_oi_change=(
            put_change / call_change
            if change_usable and call_change not in (None, 0) and put_change is not None
            else None
        ),
    )
