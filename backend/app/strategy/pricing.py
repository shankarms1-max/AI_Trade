from dataclasses import dataclass

from app.data.models import OptionContractSnapshot
from app.strategy.models import PricingBasis


@dataclass(frozen=True)
class SpreadPrice:
    short_price: float
    long_price: float
    net_credit: float
    basis: PricingBasis


def price_spread(
    short: OptionContractSnapshot, long: OptionContractSnapshot
) -> SpreadPrice | None:
    if short.bid is not None and short.bid > 0 and long.ask is not None and long.ask > 0:
        short_price, long_price = short.bid, long.ask
        basis = PricingBasis.BID_ASK
    elif short.ltp is not None and short.ltp > 0 and long.ltp is not None and long.ltp > 0:
        short_price, long_price = short.ltp, long.ltp
        basis = PricingBasis.LTP_ESTIMATE
    else:
        return None
    credit = short_price - long_price
    if credit <= 0:
        return None
    return SpreadPrice(short_price, long_price, credit, basis)
