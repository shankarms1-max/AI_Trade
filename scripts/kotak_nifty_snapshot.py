"""Fetch one real, read-only NIFTY snapshot from Kotak Neo."""

from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.broker.kotak.auth import create_market_data_client  # noqa: E402
from app.broker.base import MarketDataError  # noqa: E402
from app.broker.kotak.market_data import KotakMarketDataAdapter  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.data.snapshot import build_market_snapshot  # noqa: E402
from pydantic import ValidationError  # noqa: E402


def main() -> int:
    try:
        settings = get_settings()
    except ValidationError:
        print(
            "Configuration error: set KOTAK_CONSUMER_KEY in an untracked .env file.",
            file=sys.stderr,
        )
        return 2
    configure_logging(settings.log_level)
    try:
        client = create_market_data_client(settings)
        broker = KotakMarketDataAdapter(
            client,
            strike_range=settings.kotak_strike_range,
            option_chain_diagnostics=settings.kotak_option_chain_diagnostics,
        )
        snapshot = build_market_snapshot(
            broker,
            strike_step=settings.kotak_nifty_strike_step,
            strikes_each_side=settings.kotak_strike_range,
        )
    except MarketDataError as exc:
        print(f"Snapshot failed: {exc}", file=sys.stderr)
        return 1
    print(snapshot.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

