from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.features.models import ContractFeature, OIConcentration, OIFeatures, SideConcentration


def _moneyness(option_type: str, strike: float, spot: float) -> str:
    if strike == spot:
        return "ATM"
    if option_type == "CE":
        return "ITM" if strike < spot else "OTM"
    return "ITM" if strike > spot else "OTM"


def _position(current: OptionContractSnapshot, prior: OptionContractSnapshot | None):
    if (
        prior is None
        or current.ltp is None
        or prior.ltp is None
        or prior.ltp == 0
        or current.change_in_open_interest in (None, 0)
    ):
        return None, None, "INSUFFICIENT_DATA"
    price_change = current.ltp - prior.ltp
    if price_change == 0:
        return 0.0, 0.0, "INSUFFICIENT_DATA"
    price_pct = price_change / prior.ltp * 100
    oi_up = current.change_in_open_interest > 0
    if price_change > 0 and oi_up:
        label = "LONG_BUILDUP"
    elif price_change < 0 and oi_up:
        label = "SHORT_BUILDUP"
    elif price_change < 0:
        label = "LONG_UNWINDING"
    else:
        label = "SHORT_COVERING"
    return price_change, price_pct, label


def build_oi_features(
    snapshot: MarketSnapshot,
    prior_comparable: MarketSnapshot | None,
    *,
    top_n: int,
    change_usable: bool,
) -> OIFeatures:
    prior = {
        (item.expiry, item.strike, item.option_type.value): item
        for item in (prior_comparable.options if prior_comparable else [])
    }
    rows: list[ContractFeature] = []
    for item in snapshot.options:
        previous = prior.get((item.expiry, item.strike, item.option_type.value))
        price_change, price_pct, label = _position(item, previous)
        rows.append(
            ContractFeature(
                strike=item.strike,
                option_type=item.option_type.value,
                open_interest=item.open_interest,
                previous_open_interest=item.previous_open_interest,
                change_in_open_interest=item.change_in_open_interest,
                volume=item.volume,
                ltp=item.ltp,
                distance_from_spot=item.strike - snapshot.nifty_spot,
                distance_from_atm=item.strike - snapshot.atm_strike,
                moneyness=_moneyness(item.option_type.value, item.strike, snapshot.nifty_spot),
                price_change=price_change,
                price_change_pct=price_pct,
                oi_change=item.change_in_open_interest,
                positioning_class=label,
            )
        )
    rows.sort(key=lambda row: (row.strike, row.option_type))

    def side(kind: str) -> list[ContractFeature]:
        return [row for row in rows if row.option_type == kind]

    def rank_oi(values: list[ContractFeature]) -> list[ContractFeature]:
        return sorted(
            (row for row in values if row.open_interest is not None),
            key=lambda row: (-row.open_interest, row.strike),  # type: ignore[operator]
        )[:top_n]

    def rank_change(values: list[ContractFeature], positive: bool) -> list[ContractFeature]:
        eligible = [
            row
            for row in values
            if row.change_in_open_interest is not None
            and ((row.change_in_open_interest > 0) if positive else (row.change_in_open_interest < 0))
        ]
        return sorted(
            eligible,
            key=lambda row: (-abs(row.change_in_open_interest or 0), row.strike),
        )[:top_n]

    calls, puts = side("CE"), side("PE")
    return OIFeatures(
        contracts=rows,
        top_call_oi=rank_oi(calls),
        top_put_oi=rank_oi(puts),
        top_call_oi_additions=rank_change(calls, True) if change_usable else None,
        top_put_oi_additions=rank_change(puts, True) if change_usable else None,
        top_call_oi_reductions=rank_change(calls, False) if change_usable else None,
        top_put_oi_reductions=rank_change(puts, False) if change_usable else None,
    )


def _concentration(rows: list[ContractFeature]) -> SideConcentration:
    values = sorted(
        ((row.open_interest or 0, row.strike) for row in rows if (row.open_interest or 0) >= 0),
        reverse=True,
    )
    total = sum(value for value, _ in values)
    share = lambda count: sum(value for value, _ in values[:count]) / total if total else None
    weighted = sum(value * strike for value, strike in values) / total if total else None
    return SideConcentration(
        top_1_share=share(1),
        top_3_share=share(3),
        top_5_share=share(5),
        weighted_average_strike_by_oi=weighted,
    )


def calculate_concentration(features: OIFeatures) -> OIConcentration:
    return OIConcentration(
        call=_concentration([row for row in features.contracts if row.option_type == "CE"]),
        put=_concentration([row for row in features.contracts if row.option_type == "PE"]),
    )
