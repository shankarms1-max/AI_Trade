from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from app.broker.base import MarketDataBroker
from app.core.logging import get_logger
from app.data.models import MarketSnapshot, OptionContractSnapshot

logger = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def calculate_atm_strike(spot: float, strike_step: int = 50) -> float:
    if spot <= 0:
        raise ValueError("spot must be positive")
    if strike_step <= 0:
        raise ValueError("strike_step must be positive")
    units = Decimal(str(spot)) / Decimal(str(strike_step))
    return float(units.quantize(Decimal("1"), rounding=ROUND_HALF_UP) * strike_step)


def filter_contracts_around_atm(
    contracts: list[OptionContractSnapshot],
    atm_strike: float,
    strikes_each_side: int,
) -> list[OptionContractSnapshot]:
    if strikes_each_side < 0:
        raise ValueError("strikes_each_side must not be negative")
    available = sorted({contract.strike for contract in contracts})
    if not available:
        return []
    closest_index = min(
        range(len(available)), key=lambda index: abs(available[index] - atm_strike)
    )
    selected = set(
        available[
            max(0, closest_index - strikes_each_side) : closest_index + strikes_each_side + 1
        ]
    )
    return sorted(
        (contract for contract in contracts if contract.strike in selected),
        key=lambda contract: (contract.strike, contract.option_type.value),
    )


def nearest_expiry(expiries: list[date]) -> date:
    if not expiries:
        raise ValueError("expiry cannot be resolved")
    return min(expiries)


def build_market_snapshot(
    broker: MarketDataBroker,
    strike_step: int = 50,
    strikes_each_side: int = 10,
) -> MarketSnapshot:
    try:
        spot = broker.get_nifty_spot()
        expiry = nearest_expiry(broker.get_nifty_expiries())
        atm = calculate_atm_strike(spot, strike_step)
        contracts = filter_contracts_around_atm(
            broker.get_nifty_option_chain(expiry), atm, strikes_each_side
        )
        logger.info("OPTION_CHAIN_FILTERED contracts=%d", len(contracts))
        snapshot = MarketSnapshot(
            timestamp_ist=datetime.now(IST),
            nifty_spot=spot,
            nifty_future=broker.get_nifty_future(),
            india_vix=broker.get_india_vix(),
            lot_size=broker.get_nifty_lot_size(),
            atm_strike=atm,
            expiry=expiry,
            source="KOTAK_NEO",
            options=contracts,
        )
    except Exception:
        logger.exception("SNAPSHOT_FAILED")
        raise
    logger.info("SNAPSHOT_CREATED")
    return snapshot

