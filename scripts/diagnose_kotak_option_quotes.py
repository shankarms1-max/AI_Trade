"""One read-only REST quote probe: default to today's nearest-expiry ATM CE/PE.

Optional --instrument-token may be supplied once or twice with exact broker
numeric option IDs. No DB connection, persistence, login or order methods.
"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import logging
from math import isfinite
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.broker.kotak.depth import index_quotes, quote_identity  # noqa: E402

_SENSITIVE = re.compile(r"auth|session|secret|password|consumer|token|sid|rid|mpin|totp|mobile|ucc", re.I)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_TIME_VALUE = re.compile(r"[\d TZ:+./-]+")
_UNITS = {"UNITS", "LOTS", "CONTRACTS", "SHARES", "UNKNOWN"}


def _safe_value(value, *, timestamp=False, units=False):
    if value is None:
        return None
    if isinstance(value, bool):
        return "<redacted>"
    if isinstance(value, (int, float)):
        return value if len(str(value)) <= 32 and isfinite(value) else "<redacted>"
    if isinstance(value, str) and len(value) <= 64:
        if _NUMBER.fullmatch(value) or (timestamp and _TIME_VALUE.fullmatch(value)):
            return value
        if units and value.upper() in _UNITS:
            return value
    return "<redacted>"


def _metadata(row, prefix="", level=0):
    timestamps, quantities = {}, {}
    for key, value in row.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,47}", key):
            continue
        if _SENSITIVE.search(key):
            continue
        path = f"{prefix}.{key}" if prefix else key
        normalized = key.lower().replace("_", "")
        if ("time" in normalized or "date" in normalized) and key != "lstup_time":
            timestamps[path] = _safe_value(value, timestamp=True)
        elif normalized in {"quantityunit", "quantityunits", "depthunit", "depthunits",
                            "qtyunit", "qtyunits", "marketlot", "lot", "lotsize", "llotsize",
                            "ilotsize", "mktlot", "multiplier", "lmultiplier",
                            "pdeliveryunits", "ptrdunits", "iboardlotqty"}:
            quantities[path] = _safe_value(value, units=True)
        elif isinstance(value, dict) and level < 3:
            nested_times, nested_quantities = _metadata(value, path, level + 1)
            timestamps.update(nested_times)
            quantities.update(nested_quantities)
    return timestamps, quantities


def summarize_quote(row):
    """Whitelist only market fields; never render a broker object or error body."""
    timestamps, quantities = _metadata(row)
    symbol = row.get("display_symbol")
    symbol = symbol if isinstance(symbol, str) and re.fullmatch(r"NIFTY[A-Za-z0-9 ._-]{1,60}", symbol) else None
    result = {"exchange": row.get("exchange") if row.get("exchange") == "nse_fo" else None,
              "exchange_token": _safe_value(row.get("exchange_token")),
              "trading_symbol": symbol, "lstup_time": _safe_value(row.get("lstup_time"), timestamp=True),
              "other_timestamps": timestamps, "quantity_metadata": quantities}
    depth = row.get("depth")
    for side, label in (("buy", "bid"), ("sell", "ask")):
        levels = depth.get(side) if isinstance(depth, dict) else None
        first = levels[0] if isinstance(levels, list) and levels and isinstance(levels[0], dict) else {}
        result[f"best_{label}_price"] = _safe_value(first.get("price"))
        result[f"best_{label}_quantity"] = _safe_value(first.get("quantity"))
        _, metadata = _metadata(first, f"depth.{side}[0]")
        quantities.update(metadata)
    return result


def probe(client, tokens=None):
    from app.broker.kotak.market_data import KotakMarketDataAdapter
    from app.data.snapshot import calculate_atm_strike, filter_contracts_around_atm

    if tokens is None:
        broker = KotakMarketDataAdapter(client)
        spot = broker.get_nifty_spot()
        expiry = min(broker.get_nifty_expiries())
        contracts = filter_contracts_around_atm(
            broker.get_nifty_option_chain(expiry), calculate_atm_strike(spot), 0)
        identities = [quote_identity(item) for item in contracts]
        if len(identities) != 2 or None in identities or len(set(identities)) != 2:
            raise ValueError("No unambiguous ATM option pair")
        tokens = [identity[1] for identity in identities]
    if not 1 <= len(tokens) <= 2 or len(set(tokens)) != len(tokens) or any(
        not isinstance(token, str) or not re.fullmatch(r"[0-9]{1,12}", token) for token in tokens
    ):
        raise ValueError("Expected one or two exact numeric option instrument identifiers")
    requested = [("nse_fo", token) for token in tokens]
    response = client.quotes(instrument_tokens=[{"exchange_segment": exchange, "instrument_token": token}
                                                for exchange, token in requested], quote_type="all")
    KotakMarketDataAdapter._raise_for_error(response, "diagnostic quotes")
    rows = index_quotes(response, set(requested))
    if len(rows) != len(requested):
        raise ValueError("Missing or ambiguous quote response")
    return [summarize_quote(rows[key]) for key in requested]


def main(argv=None, *, client_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument-token", action="append", help="Exact numeric option ID; at most twice")
    args = parser.parse_args(argv)
    previous_level = logging.root.manager.disable
    saved_env = {key: os.environ.get(key) for key in ("NEO_LOG_LEVEL", "NEO_LOG_FILE_ENABLED")}
    try:
        # Set before importing the SDK: its default logger can write response bodies.
        os.environ["NEO_LOG_LEVEL"] = "NOLOG"
        os.environ["NEO_LOG_FILE_ENABLED"] = "false"
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            if client_factory is None:
                from app.broker.kotak.auth import create_market_data_client
                from app.core.config import get_settings
                client_factory = lambda: create_market_data_client(get_settings())
            client = client_factory()
            try:
                result = probe(client, args.instrument_token)
            finally:
                api = getattr(client, "api_client", None)
                rest = getattr(api, "rest_client", None)
                if rest is not None:
                    rest.close()
    except Exception:
        # No exception text, URLs, broker bodies, environment values or traceback.
        print("Quote diagnostic failed; no usable unambiguous response.", file=sys.stderr)
        return 1
    finally:
        logging.disable(previous_level)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print(json.dumps(result, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
