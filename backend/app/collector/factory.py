from collections.abc import Callable

from app.broker.kotak.auth import create_market_data_client
from app.broker.kotak.market_data import KotakMarketDataAdapter
from app.core.config import Settings
from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.data.snapshot import build_market_snapshot


def make_live_snapshot_builder(
    settings: Settings,
    required_contract_provider: Callable[[], list[OptionContractSnapshot]] | None = None,
) -> Callable[[], MarketSnapshot]:
    client = create_market_data_client(settings)
    broker = KotakMarketDataAdapter(
        client,
        strike_range=settings.kotak_strike_range,
        option_chain_diagnostics=settings.kotak_option_chain_diagnostics,
    )

    def build() -> MarketSnapshot:
        return build_market_snapshot(
            broker,
            strike_step=settings.kotak_nifty_strike_step,
            strikes_each_side=settings.kotak_strike_range,
            required_contracts=required_contract_provider() if required_contract_provider else None,
        )

    return build
