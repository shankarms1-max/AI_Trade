from dataclasses import dataclass

from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.shadow.models import ShadowPricingBasis, ShadowTrade, ShadowTradeMark


@dataclass(frozen=True)
class Valuation:
    short_price: float
    long_price: float
    exit_debit: float
    basis: ShadowPricingBasis
    quote_age_seconds: float | None = None
    leg_time_skew_seconds: float | None = None
    depth_available: bool = False
    fill_quality_state: str = "UNVERIFIED_QUOTE_TIME"


def _matching_contract(snapshot: MarketSnapshot, leg) -> OptionContractSnapshot | None:
    matches = [
        item for item in snapshot.options
        if item.expiry == leg.expiry
        and item.option_type.value == leg.option_type
        and item.strike == leg.strike
    ]
    if leg.instrument_token:
        exact = [item for item in matches if item.instrument_token == leg.instrument_token]
        return exact[0] if len(exact) == 1 else None
    return matches[0] if len(matches) == 1 else None


def value_trade(trade: ShadowTrade, snapshot: MarketSnapshot) -> Valuation | None:
    short = _matching_contract(snapshot, trade.short_leg)
    long = _matching_contract(snapshot, trade.long_leg)
    if short is None or long is None:
        return None
    if (short.ask is not None and short.ask > 0 and long.bid is not None and long.bid >= 0
            and short.bid is not None and short.bid <= short.ask
            and long.ask is not None and long.ask >= long.bid):
        debit = short.ask - long.bid
        times = (short.source_market_timestamp, long.source_market_timestamp)
        age = max((snapshot.timestamp_ist - value).total_seconds() for value in times) if all(times) else None
        skew = abs((times[0] - times[1]).total_seconds()) if all(times) else None
        depth = short.ask_quantity is not None and long.bid_quantity is not None
        state = ("UNVERIFIED_QUOTE_TIME" if age is None else
                 "STALE_OR_SKEWED_QUOTE" if age < 0 or age > 30 or skew > 10 else
                 "NEXT_OBSERVATION_SIMULATION")
        return Valuation(short.ask, long.bid, debit, ShadowPricingBasis.BID_ASK,
                         age, skew, depth, state)
    if (
        trade.entry_pricing_basis == ShadowPricingBasis.LTP_ESTIMATE
        and short.ltp is not None and short.ltp >= 0
        and long.ltp is not None and long.ltp >= 0
    ):
        return Valuation(
            short.ltp, long.ltp, short.ltp - long.ltp,
            ShadowPricingBasis.LTP_ESTIMATE,
        )
    return None


def create_mark(
    trade: ShadowTrade, snapshot_id: int, snapshot: MarketSnapshot, valuation: Valuation
) -> ShadowTradeMark:
    pnl = trade.entry_credit - valuation.exit_debit
    warnings: list[str] = []
    tolerance = 1e-6
    if pnl > trade.max_profit_per_unit + tolerance or pnl < -trade.max_loss_per_unit - tolerance:
        warnings.append("PAYOUT_ANOMALY")
    return ShadowTradeMark(
        shadow_trade_id=trade.id or 0,
        market_snapshot_id=snapshot_id,
        timestamp=snapshot.timestamp_ist,
        short_price=valuation.short_price,
        long_price=valuation.long_price,
        valuation_basis=valuation.basis,
        quote_age_seconds=valuation.quote_age_seconds,
        leg_time_skew_seconds=valuation.leg_time_skew_seconds,
        depth_available=valuation.depth_available,
        fill_quality_state=valuation.fill_quality_state,
        exit_debit=valuation.exit_debit,
        pnl_per_unit=pnl,
        pnl_per_lot=None if trade.lot_size is None else pnl * trade.lot_size,
        spot=snapshot.nifty_spot,
        warnings=warnings,
        created_at=snapshot.timestamp_ist,
    )
