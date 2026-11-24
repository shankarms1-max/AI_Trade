"""Market-data-only reader for MCX outright futures (CRUDEOILM, NATGASMINI) via Kotak Neo.

Reuses the Commodity ACTS two-method facade (search_scrip + quotes only; `assert_market_only` refuses any object
exposing order/buy/sell/square-off capabilities). One quote request per observation per instrument (the futures
token only; no option chain), which keeps the shared consumer-key request load to ~4 calls/min per instrument.
Field semantics follow app.broker.kotak.depth: best bid/ask from the SAME depth level (`_touch`), `last_volume` =
session total traded volume, `open_int` = open interest, `lstup_time` = exchange timestamp.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.broker.kotak.depth import _touch, parse_quote_timestamp
from app.commodity_acts.broker import (CommodityDataError, MarketOnlySDK, SEGMENT, assert_market_only, integer,
                                       number)

IST = ZoneInfo("Asia/Kolkata")


class McxFuturesMarket:
    def __init__(self, client: Any, clock: Callable[[], datetime] = lambda: datetime.now(IST)):
        assert_market_only(client)
        self._client, self._clock = client, clock

    @staticmethod
    def _call(method: Callable, **kwargs):
        try:
            return method(**kwargs)
        except Exception:
            raise CommodityDataError("MCX_FUTURES_MARKET_REQUEST_FAILED") from None

    def master_rows(self, instrument: str) -> list[dict[str, Any]]:
        response = self._call(self._client.search_scrip, exchange_segment=SEGMENT, symbol=instrument,
                              ignore_50multiple=True)
        rows = response.get("data") if isinstance(response, dict) else response
        if not isinstance(rows, list):
            raise CommodityDataError("MCX_FUTURES_MASTER_UNAVAILABLE")
        return [r for r in rows if isinstance(r, dict) and r.get("pExchSeg") == SEGMENT]

    def quote(self, token: str, trading_symbol: str) -> dict[str, Any]:
        """One futures quote as plain fields for the engine's Observation (None where the broker gave nothing)."""
        started = self._clock()
        response = self._call(self._client.quotes,
                              instrument_tokens=[{"exchange_segment": SEGMENT, "instrument_token": str(token)}],
                              quote_type="all")
        received = self._clock()
        rows = response.get("data") if isinstance(response, dict) else response
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise CommodityDataError("MCX_FUTURES_QUOTE_UNAVAILABLE")
        row = rows[0]
        if row.get("exchange") != SEGMENT or str(row.get("exchange_token")) != str(token):
            raise CommodityDataError("MCX_FUTURES_QUOTE_IDENTITY_MISMATCH")
        if row.get("display_symbol") is not None and row["display_symbol"] != trading_symbol:
            raise CommodityDataError("MCX_FUTURES_QUOTE_IDENTITY_MISMATCH")
        depth = row.get("depth") if isinstance(row.get("depth"), dict) else {}
        bid, bid_qty = _touch(depth, "buy")
        ask, ask_qty = _touch(depth, "sell")
        return {"at": received, "request_started_at": started, "ltp": number(row.get("ltp"), positive=True),
                "bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty,
                "cum_volume": integer(row.get("last_volume")), "oi": integer(row.get("open_int")),
                "source_ts": parse_quote_timestamp(row.get("lstup_time"))}


def make_market(settings) -> McxFuturesMarket:
    from app.broker.kotak.auth import create_market_data_client

    sdk = create_market_data_client(settings)
    return McxFuturesMarket(MarketOnlySDK(sdk.search_scrip, sdk.quotes))
