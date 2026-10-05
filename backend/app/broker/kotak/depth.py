"""Strict parsing of Kotak v3 REST quotes; no inferred prices or quantity units.

Schema: https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/market_data/quotes.md
The documented real response uses exchange/exchange_token and depth.buy/sell
rows containing price/quantity. The separate official REST field mapping defines
lstup_time as Last update time (Unix timestamp):
https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/market-data-apis/quotes.md
Neither quantity units nor a tick-size field are established by those schemas.

Evidence review (2026-10-06): installed 3.0.7 services/quotes.py returns JSON
unchanged. Upstream 6e1bfb5b460b49c1023f841c1e3a906c7a41ed53 test_quotes.py
tests transport/envelopes, not lstup_time or derivative quantity semantics.
The REST mapping, not a guess from SFeed or numeric magnitudes, supplies the
timestamp interpretation. It does not specify exchange-event vs broker-cache
update provenance. SFeed
DepthLevel forwards raw quantity without applying market_lot; this does not
establish what the REST server's derivative quantities measure.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.data.models import OptionContractSnapshot


def parse_quote_timestamp(value: Any) -> datetime | None:
    """Documented REST Unix seconds; no millisecond/1980-epoch guessing."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 12:
        return None
    seconds = int(text)
    if not 0 < seconds <= 253402300799:  # Last representable UTC second in year 9999.
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).astimezone(ZoneInfo("Asia/Kolkata"))
    except (ValueError, OverflowError, OSError):
        return None


def quote_identity(contract: OptionContractSnapshot) -> tuple[str, str] | None:
    token = contract.instrument_token
    if not token or contract.exchange != "nse_fo":
        return None
    if "|" in token:
        exchange, token = token.split("|", 1)
        if exchange != contract.exchange:
            return None
    # Tokens come from the broker chain; never look up or invent numeric IDs.
    if not token or "|" in token:
        return None
    return contract.exchange, token


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() and result >= 0 else None


def _touch(depth: dict, side: str) -> tuple[float | None, int | None]:
    levels = depth.get(side)
    if not isinstance(levels, list) or not levels or not isinstance(levels[0], dict):
        return None, None
    # Keep price and quantity from the same best level; never use total_buy/sell.
    price = _number(levels[0].get("price"))
    quantity = _number(levels[0].get("quantity"))
    valid_quantity = (quantity is not None and quantity == quantity.to_integral_value()
                      and quantity <= 2**63 - 1)
    return (float(price) if price is not None and 0 < price < Decimal("1e308") else None,
            int(quantity) if valid_quantity else None)


def quote_updates(row: dict) -> dict:
    depth = row.get("depth")
    depth = depth if isinstance(depth, dict) else {}
    bid, bid_quantity = _touch(depth, "buy")
    ask, ask_quantity = _touch(depth, "sell")
    return {"bid": bid, "ask": ask, "bid_quantity": bid_quantity,
            "ask_quantity": ask_quantity, "depth_unit": "UNKNOWN",
            "source_market_timestamp": parse_quote_timestamp(row.get("lstup_time"))}


def index_quotes(response: Any, requested: set[tuple[str, str]]) -> dict:
    rows = response.get("data") if isinstance(response, dict) else response
    if not isinstance(rows, list):
        return {}
    found, duplicates = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        exchange, token = row.get("exchange"), row.get("exchange_token")
        if not isinstance(exchange, str) or not isinstance(token, (str, int)) or isinstance(token, bool):
            continue
        identity = exchange, str(token)
        if identity not in requested:
            continue
        if identity in found:
            duplicates.add(identity)
        found[identity] = row
    return {key: row for key, row in found.items() if key not in duplicates}
