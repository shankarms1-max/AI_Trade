from datetime import date
from decimal import Decimal, InvalidOperation
import json
from math import ceil
from time import monotonic, sleep
from typing import Any

from app.broker.base import BrokerSessionError, MarketDataBroker, MarketDataError
from app.broker.kotak.client import KotakSDKClient
from app.broker.kotak.depth import (index_quotes, quote_identity, quote_updates,
                                   required_quote_updates)
from app.broker.kotak.instruments import (
    NIFTY_INDEX_IDENTIFIER,
    NIFTY_UNDERLYING,
    NSE_CASH,
    NSE_FO,
)
from app.broker.kotak.option_chain import (
    extract_option_chain_payload,
    normalize_option_chain,
    option_chain_data_quality,
    summarize_option_chain_response,
    summarize_option_chain_structure,
)
from app.broker.kotak.vix import INDIA_VIX_IDENTIFIER, parse_vix_ltp
from app.core.logging import get_logger
from app.data.models import OptionContractSnapshot

logger = get_logger(__name__)


def _positive_float_or_none(value: Any) -> float | None:
    if value is None or (isinstance(value, str) and value.strip() in {"", "-"}):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return float(number)


def _positive_int_or_none(value: Any) -> int | None:
    number = _positive_float_or_none(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def option_chain_request_count(strike_range: int) -> int:
    """Return a backend-valid count covering ATM plus both requested sides."""

    if strike_range < 0:
        raise ValueError("strike_range must not be negative")
    required_strikes = (strike_range * 2) + 1
    count = max(10, ceil(required_strikes / 10) * 10)
    if count % 10 != 0:
        raise ValueError("Kotak option-chain count must be a multiple of 10")
    return count


class KotakMarketDataAdapter(MarketDataBroker):
    """Kotak Neo v3 read-only market-data adapter."""

    def __init__(
        self,
        client: KotakSDKClient,
        strike_range: int = 10,
        option_chain_diagnostics: bool = False,
    ) -> None:
        self._client = client
        self._strike_range = strike_range
        self._option_chain_diagnostics = option_chain_diagnostics
        self._cached_nifty_future: float | None = None
        self._cached_nifty_future_identity: tuple[str | None, date | None] = (None, None)
        self._cached_nifty_lot_size: int | None = None
        self._future_from_chain_resolved = False

    @staticmethod
    def _sdk_call(operation: str, method: Any, **kwargs: Any) -> Any:
        """Call the SDK without allowing raw broker responses into exceptions/logs."""

        try:
            return method(**kwargs)
        except Exception as exc:
            exception_name = type(exc).__name__.lower()
            if "auth" in exception_name or "session" in exception_name:
                raise BrokerSessionError(
                    f"Kotak session invalid during {operation}"
                ) from None
            raise MarketDataError(f"Kotak {operation} request failed") from None

    @staticmethod
    def _raise_for_error(response: Any, operation: str) -> None:
        failed = isinstance(response, dict) and (
            response.get("error")
            or response.get("Error")
            or response.get("Error Message")
            or str(response.get("stat", "")).lower() in {"not_ok", "not ok", "error"}
        )
        if failed:
            text = str(response).lower()
            if any(word in text for word in ("session", "login", "403", "unauthor")):
                raise BrokerSessionError(f"Kotak session invalid during {operation}")
            raise MarketDataError(f"Kotak {operation} failed")

    def _index_ltp(self, identifier: str) -> float:
        response = self._sdk_call(
            f"quote for {identifier}",
            self._client.quotes,
            instrument_tokens=[
                {"instrument_token": identifier, "exchange_segment": NSE_CASH}
            ],
            quote_type="ltp",
        )
        self._raise_for_error(response, f"quote for {identifier}")
        quotes = response.get("data") if isinstance(response, dict) else response
        if not isinstance(quotes, list) or not quotes:
            raise MarketDataError(f"Kotak returned no quote for {identifier}")
        try:
            return float(quotes[0]["ltp"])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise MarketDataError(f"Kotak returned no LTP for {identifier}") from exc

    def get_nifty_spot(self) -> float:
        ltp = self._index_ltp(NIFTY_INDEX_IDENTIFIER)
        logger.info("NIFTY_SPOT_FETCHED")
        return ltp

    def get_india_vix(self) -> float | None:
        try:
            response = self._sdk_call(
                "quote for INDIA VIX index",
                self._client.quotes,
                instrument_tokens=[
                    {
                        "instrument_token": INDIA_VIX_IDENTIFIER,
                        "exchange_segment": NSE_CASH,
                    }
                ],
                quote_type="ltp",
            )
            self._raise_for_error(response, "quote for INDIA VIX index")
            ltp = parse_vix_ltp(response)
            if ltp is None:
                raise MarketDataError("Kotak returned no LTP for INDIA VIX index")
        except BrokerSessionError:
            raise
        except MarketDataError:
            logger.warning("VIX_FETCH_FAILED")
            return None
        logger.info("VIX_FETCHED")
        return ltp

    def _cache_nifty_future(self, payload: dict[str, Any]) -> None:
        self._future_from_chain_resolved = True
        self._cached_nifty_future = None
        self._cached_nifty_future_identity = (None, None)
        future = payload.get("future")
        if not isinstance(future, dict):
            logger.warning("NIFTY_FUTURE_UNAVAILABLE reason=missing")
            return
        expiry_value = future.get("expiry")
        try:
            future_expiry = date.fromisoformat(str(expiry_value))
        except (TypeError, ValueError):
            logger.warning("NIFTY_FUTURE_UNAVAILABLE reason=invalid_expiry")
            return
        if future_expiry < date.today():
            logger.warning("NIFTY_FUTURE_UNAVAILABLE reason=expired")
            return
        ltp = _positive_float_or_none(future.get("ltp"))
        if ltp is None:
            logger.warning("NIFTY_FUTURE_UNAVAILABLE reason=invalid_ltp")
            return
        self._cached_nifty_future = ltp
        symbol = future.get("symbol")
        if isinstance(symbol, str) and symbol.strip():
            self._cached_nifty_future_identity = (symbol.strip(), future_expiry)

    def _cache_nifty_lot_size(self, payload: dict[str, Any]) -> None:
        self._cached_nifty_lot_size = None
        common_data = payload.get("common_data")
        if not isinstance(common_data, dict):
            logger.warning("NIFTY_LOT_SIZE_UNAVAILABLE reason=missing_common_data")
            return
        self._cached_nifty_lot_size = _positive_int_or_none(common_data.get("mktLot"))
        if self._cached_nifty_lot_size is None:
            logger.warning("NIFTY_LOT_SIZE_UNAVAILABLE reason=invalid_mktLot")

    def get_nifty_expiries(self) -> list[date]:
        response = self._sdk_call(
            "expiry lookup",
            self._client.expiries,
            exchange=NSE_FO, underlying=NIFTY_UNDERLYING, instrument_type="option"
        )
        self._raise_for_error(response, "expiry lookup")
        values = response.get("expiries") if isinstance(response, dict) else None
        if not isinstance(values, list):
            raise MarketDataError("Kotak returned no NIFTY expiries")
        today = date.today()
        expiries: list[date] = []
        for value in values:
            try:
                parsed = date.fromisoformat(str(value))
            except ValueError:
                continue
            if parsed >= today:
                expiries.append(parsed)
        if not expiries:
            raise MarketDataError("No current NIFTY expiry could be resolved")
        logger.info("EXPIRY_RESOLVED")
        return sorted(set(expiries))

    def get_nifty_option_chain(self, expiry: date) -> list[OptionContractSnapshot]:
        request_count = option_chain_request_count(self._strike_range)
        expiry_value = expiry.isoformat()
        response = self._sdk_call(
            "option-chain lookup",
            self._client.option_chain,
            exchange=NSE_FO,
            underlying=NIFTY_UNDERLYING,
            expiry=expiry_value,
            instrument_type="option",
            count=request_count,
        )
        diagnostic = summarize_option_chain_response(
            response,
            expiry=expiry_value,
            exchange=NSE_FO,
            underlying=NIFTY_UNDERLYING,
            instrument_type="option",
            count=request_count,
        )
        if self._option_chain_diagnostics:
            logger.warning(
                "KOTAK_OPTION_CHAIN_DIAGNOSTIC %s",
                json.dumps(diagnostic, separators=(",", ":"), sort_keys=False),
            )

        payload = extract_option_chain_payload(response, diagnostic=diagnostic)
        self._cache_nifty_future(payload)
        self._cache_nifty_lot_size(payload)
        call_count = len(payload.get("call") or [])
        put_count = len(payload.get("put") or [])
        if self._option_chain_diagnostics:
            structure = summarize_option_chain_structure(payload)
            logger.warning(
                "KOTAK_OPTION_CHAIN_STRUCTURE %s",
                json.dumps(structure, separators=(",", ":"), sort_keys=False),
            )

        contracts = normalize_option_chain(response, expiry, diagnostic=diagnostic)
        logger.info(
            "OPTION_CHAIN_FETCHED expiry=%s calls=%d puts=%d",
            expiry_value,
            call_count,
            put_count,
        )
        logger.info("OPTION_CHAIN_NORMALIZED contracts=%d", len(contracts))
        quality = option_chain_data_quality(contracts)
        logger.info(
            "OPTION_CHAIN_DATA_QUALITY contracts=%d nonzero_volume=%d "
            "nonzero_oi_change=%d oi_current_prev_different=%d oi_mismatch=%d",
            quality["contracts"],
            quality["nonzero_volume"],
            quality["nonzero_oi_change"],
            quality["oi_current_prev_different"],
            quality["oi_mismatch"],
        )
        return contracts

    def get_nifty_future(self) -> float | None:
        if not self._future_from_chain_resolved:
            logger.warning("NIFTY_FUTURE_UNAVAILABLE reason=option_chain_not_fetched")
        return self._cached_nifty_future

    def enrich_option_quotes(
        self, contracts: list[OptionContractSnapshot]
    ) -> list[OptionContractSnapshot]:
        # 42 filtered ATM +/-10 contracts fit in one broker-supported batch.
        identities = list(dict.fromkeys(key for item in contracts
                                        if (key := quote_identity(item)) is not None))
        started = monotonic()
        rows = self._option_quote_rows(identities, started=started)
        enriched = [item.model_copy(update=quote_updates(rows[key]))
                    if (key := quote_identity(item)) in rows else item for item in contracts]
        logger.info(
            "OPTION_QUOTES_CAPTURED requested=%d matched=%d bid_ask=%d quantities=%d "
            "source_timestamps=%d depth_unit=UNITS tick_size=unavailable elapsed_ms=%d",
            len(identities), len(rows),
            sum(item.bid is not None and item.ask is not None for item in enriched),
            sum(item.bid_quantity is not None and item.ask_quantity is not None for item in enriched),
            sum(item.source_market_timestamp is not None for item in enriched),
            int((monotonic() - started) * 1000),
        )
        return enriched

    def capture_required_option_quotes(
        self, contracts: list[OptionContractSnapshot]
    ) -> list[OptionContractSnapshot]:
        identities = list(dict.fromkeys(key for item in contracts
                                        if (key := quote_identity(item)) is not None))
        rows = self._option_quote_rows(identities)
        # Never return an old book or an identity-only placeholder as a matched quote.
        return [OptionContractSnapshot(
            expiry=item.expiry, strike=item.strike, option_type=item.option_type,
            exchange=item.exchange, instrument_token=item.instrument_token,
            trading_symbol=item.trading_symbol, **required_quote_updates(rows[key]))
            for item in contracts if (key := quote_identity(item)) in rows]

    def _option_quote_rows(self, identities: list[tuple[str, str]], *,
                          started: float | None = None) -> dict:
        rows = {}
        started = monotonic() if started is None else started
        for offset in range(0, len(identities), 50):
            if offset:
                if monotonic() - started >= 10:
                    logger.warning("OPTION_QUOTES_INCOMPLETE reason=batch_budget")
                    break
                sleep(0.04)  # Official ceiling: 25 requests/second.
            batch = identities[offset:offset + 50]
            try:
                response = self._sdk_call(
                    "option depth quotes", self._client.quotes,
                    instrument_tokens=[{"exchange_segment": exchange, "instrument_token": token}
                                       for exchange, token in batch],
                    quote_type="all",
                )
                self._raise_for_error(response, "option depth quotes")
            except MarketDataError:
                # Quote failure must not discard the successfully fetched chain.
                logger.warning("OPTION_QUOTES_UNAVAILABLE reason=request_failed")
                break
            rows.update(index_quotes(response, set(batch)))
        return rows

    def get_nifty_future_identity(self) -> tuple[str | None, date | None]:
        return self._cached_nifty_future_identity

    def get_nifty_lot_size(self) -> int | None:
        return self._cached_nifty_lot_size
