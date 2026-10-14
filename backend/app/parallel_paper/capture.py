"""Reuse Kotak market-only infrastructure; fixed-basket volume always comes from REST all-quotes."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from app.broker.base import MarketDataBroker, MarketDataError
from app.broker.kotak.auth import create_market_data_client
from app.broker.kotak.market_data import KotakMarketDataAdapter
from app.core.config import Settings
from app.core.logging import get_logger
from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.data.snapshot import build_market_snapshot
from app.research.quotes import ContractIdentity
from app.scalper.repository import IST

logger = get_logger(__name__)


def capture_market(broker: MarketDataBroker, settings: Settings,
                   required: list[OptionContractSnapshot], basket: list[OptionContractSnapshot]) -> MarketSnapshot:
    market = build_market_snapshot(broker, settings.kotak_nifty_strike_step,
                                   settings.scalper_strike_range, required_contracts=required)
    if not basket:
        # Freeze the current first-session ATM pair; the persisted first snapshot
        # is the authoritative basket on subsequent cycles and after restart.
        for side in ("CE", "PE"):
            matches = [quote for quote in market.options
                       if quote.strike == market.atm_strike and quote.option_type.value == side]
            if len(matches) != 1:
                basket = []
                break
            basket.append(matches[0])
    identities = {ContractIdentity.of(quote) for quote in basket}
    try:
        refreshed = broker.capture_required_option_quotes(basket) if len(basket) == 2 else []
    except MarketDataError as exc:
        logger.warning("PARALLEL_OPTION_VOLUME_CAPTURE_FAILED error_type=%s", type(exc).__name__)
        refreshed = []
    keys = [ContractIdentity.of(quote) for quote in refreshed]
    if len(set(keys)) != len(keys) or not set(keys) <= identities:
        raise ValueError("PARALLEL_OPTION_VOLUME_IDENTITY_AMBIGUOUS")
    indexed = {ContractIdentity.of(quote): quote for quote in refreshed}

    def replace_basket(rows: list[OptionContractSnapshot]) -> list[OptionContractSnapshot]:
        return [indexed.get(ContractIdentity.of(quote), quote.model_copy(update={"volume": None}))
                if ContractIdentity.of(quote) in identities else quote for quote in rows]

    continuity = market.required_contracts
    if continuity is not None:
        continuity = continuity.model_copy(update={"quotes": replace_basket(continuity.quotes)})
    received = datetime.now(IST)
    logger.info("PARALLEL_OPTION_VOLUME_CAPTURE source=REST_ALL_LAST_VOLUME requested=%d matched=%d",
                len(basket), len(refreshed))
    return market.model_copy(update={"options": replace_basket(market.options),
                                     "required_contracts": continuity,
                                     "timestamp_ist": received, "response_received_at": received})


def make_snapshot_builder(
    settings: Settings, required_provider: Callable[[], list[OptionContractSnapshot]],
    basket_provider: Callable[[datetime], list[OptionContractSnapshot]],
) -> Callable[[], MarketSnapshot]:
    broker = KotakMarketDataAdapter(
        create_market_data_client(settings), strike_range=settings.scalper_strike_range,
        option_chain_diagnostics=settings.kotak_option_chain_diagnostics)

    def build() -> MarketSnapshot:
        return capture_market(broker, settings, required_provider(), basket_provider(datetime.now(IST)))

    return build
