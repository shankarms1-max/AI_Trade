"""Safely test Kotak's exact INDIA VIX REST index identifier."""

import json
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.broker.kotak.auth import create_market_data_client  # noqa: E402
from app.broker.kotak.instruments import NSE_CASH  # noqa: E402
from app.broker.kotak.vix import INDIA_VIX_IDENTIFIER, summarize_vix_quote  # noqa: E402
from app.core.config import get_settings  # noqa: E402


def main() -> int:
    try:
        client = create_market_data_client(get_settings())
        response = client.quotes(
            instrument_tokens=[
                {
                    "instrument_token": INDIA_VIX_IDENTIFIER,
                    "exchange_segment": NSE_CASH,
                }
            ],
            quote_type="ltp",
        )
        summary = summarize_vix_quote(
            response, method="REST quotes exact INDIA VIX identifier"
        )
    except Exception as exc:
        summary = {
            "method": "REST quotes exact INDIA VIX identifier",
            "success": False,
            "response_type": None,
            "returned_symbol": None,
            "returned_token": None,
            "ltp": None,
            "safe_error_message": f"Kotak request failed ({type(exc).__name__})",
        }
    print(json.dumps(summary, separators=(",", ":")))
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
