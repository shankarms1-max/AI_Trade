from dataclasses import dataclass

from app.data.models import MarketSnapshot, OptionContractSnapshot


@dataclass(frozen=True)
class ATMSelection:
    strike: float
    call: OptionContractSnapshot
    put: OptionContractSnapshot
    distance_points: float = 0
    distance_percent: float = 0
    nearest_pair_incomplete: bool = False


def select_atm(snapshot: MarketSnapshot, reference_price: float) -> ATMSelection | None:
    calls = {
        item.strike: item for item in snapshot.options
        if item.expiry == snapshot.expiry and item.option_type.value == "CE"
    }
    puts = {
        item.strike: item for item in snapshot.options
        if item.expiry == snapshot.expiry and item.option_type.value == "PE"
    }
    common = calls.keys() & puts.keys()
    if not common:
        return None
    # Lower strike wins an exact equidistant tie; this is stable and documented.
    strike = min(common, key=lambda value: (abs(value - reference_price), value))
    all_strikes = calls.keys() | puts.keys()
    nearest = min(all_strikes, key=lambda value: (abs(value - reference_price), value))
    distance = abs(strike - reference_price)
    return ATMSelection(strike, calls[strike], puts[strike], distance,
                        100 * distance / reference_price if reference_price > 0 else 0,
                        nearest not in common)


def matching_contract(snapshot: MarketSnapshot, contract: OptionContractSnapshot):
    if not contract.instrument_token:
        return None
    return next((
        item for item in snapshot.options
        if item.exchange == contract.exchange
        and item.instrument_token == contract.instrument_token
        and item.expiry == contract.expiry and item.strike == contract.strike
        and item.option_type == contract.option_type
    ), None)
