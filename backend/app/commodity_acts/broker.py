"""MCX scrip-master + batch quotes only. No execution methods or NIFTY adapter.

Wire schema verified against installed kotakneoapi 3.0.7 ScripSearch/quotes and
official Kotak-Neo/kotak-neo-python market_data docs. Search returns pExpiryDate
as DDMMMYYYY; MCX lExpiryDate is Unix seconds (not NSE's shifted epoch). The
SDK's strike filters compare dStrikePrice; to requested strike * 100.
"""

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from time import sleep
from pathlib import Path
import stat

from pydantic import SecretStr
from typing import Any, Callable, Protocol, cast
from app.data.models import OptionType

from app.alpha.clock import IST
from app.broker.kotak.depth import _touch, parse_quote_timestamp
from app.broker.kotak.option_chain import _redact_sensitive
from app.commodity_acts.config import InstrumentAlias, InstrumentSpec
from app.commodity_acts.domain import CommodityOption, CommoditySnapshot

SEGMENT = "mcx_fo"


class CommodityDataError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)  # Never concatenate broker response/exception text.


class MarketReadClient(Protocol):
    def search_scrip(self, **kwargs) -> Any: ...
    def quotes(self, **kwargs) -> Any: ...


def assert_market_only(value: object) -> None:
    exposed = [name for name in dir(value) if not name.startswith("_")]
    # Refuse the entire capability namespace, including noncallable handles.
    # The dedicated read facade has no such attributes at all.
    if any(
        "order" in name.lower()
        or name.lower() in {"buy", "sell", "execute", "amend", "cancel_all"}
        or name.lower().startswith("square")
        for name in exposed
    ):
        raise CommodityDataError("COMMODITY_ORDER_CAPABILITY_FORBIDDEN")


class MarketOnlySDK:
    """Explicit two-method capability facade; SDK trading capabilities not exposed."""

    __slots__ = ("__search", "__quote")

    def __init__(self, search: Callable, quote: Callable):
        self.__search, self.__quote = search, quote

    def search_scrip(self, **kwargs):
        return self.__search(**kwargs)

    def quotes(self, **kwargs):
        return self.__quote(**kwargs)


def make_adapter(settings, *, key_file=None, **kwargs):
    if key_file:
        # Deployment secret, mounted read-only only into the commodity worker.
        # Keep the actual key out of Docker environment/argv and all messages.
        path = Path(key_file)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("unsafe secret permissions")
            with path.open("rb") as stream:
                value = stream.read(16385)
            key = value.decode("utf-8").strip()
            if not key or len(value) > 16384 or any(c.isspace() for c in key) or key.startswith("COMMODITY_DISABLED"):
                raise ValueError("invalid secret")
            settings = settings.model_copy(update={"kotak_consumer_key": SecretStr(key)})
            del value, key
        except (OSError, ValueError, UnicodeError):
            raise CommodityDataError("COMMODITY_MARKET_DATA_SECRET_FILE_REFUSED") from None

    # Existing consumer-key-only read authentication; no trading adapter import.
    from app.broker.kotak.auth import create_market_data_client

    sdk = create_market_data_client(settings)
    return KotakCommodityMarketData(MarketOnlySDK(sdk.search_scrip, sdk.quotes), **kwargs)


def number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return (
        float(result)
        if result.is_finite() and 0 <= result < Decimal("1e100") and (not positive or result > 0)
        else None
    )


def integer(value: Any, *, positive: bool = False) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return (
        int(result)
        if result.is_finite()
        and 0 <= result <= 2**63 - 1
        and result == result.to_integral_value()
        and (not positive or result > 0)
        else None
    )


def raw_timestamp(value: Any) -> str | int | None:
    # Retain only a small scalar from the known timestamp field, not its parent
    # response. Malformed/missing timestamps never become quote source times.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return _redact_sensitive(value) if isinstance(value, str) and len(value) <= 128 else None


def master_expiry(row: dict) -> date:
    readable = row.get("pExpiryDate")
    try:
        parsed = datetime.strptime(str(readable), "%d%b%Y").date()
        epoch = number(row.get("lExpiryDate"), positive=True)
        if epoch is not None and datetime.fromtimestamp(epoch, timezone.utc).date() != parsed:
            raise CommodityDataError("COMMODITY_EXPIRY_METADATA_DISAGREEMENT")
        return parsed
    except (ValueError, OverflowError, OSError):
        raise CommodityDataError("COMMODITY_EXPIRY_ENCODING_UNSUPPORTED") from None


def resolve_master(
    response: Any, spec: InstrumentAlias, today: date, *, renewal_policy=None, at=None, required_options=()
) -> tuple[dict, list[dict]]:
    if not isinstance(response, list):
        raise CommodityDataError("FUTURE_UNRESOLVED")
    records = []
    for row in response:
        if (
            not isinstance(row, dict)
            or str(row.get("pSymbolName", "")).strip().upper() != spec.underlying.strip().upper()
        ):
            continue  # SDK search is substring-based; never trust it as exact identity.
        if row.get("pExchSeg") != SEGMENT:
            raise CommodityDataError("COMMODITY_EXCHANGE_IDENTITY_MISMATCH")
        kind, side = str(row.get("pInstType", "")).strip().upper(), str(row.get("pOptionType", "")).strip().upper()
        if not (kind.startswith("FUT") and side == "XX" or kind.startswith("OPT") and side in ("CE", "PE")):
            # Observed live: CRUDEOILMCOM / COM has no derivative expiry. It is
            # not a future/option and must not become a cash-spot dependency.
            continue
        expiry = master_expiry(row)
        if expiry < today:
            continue
        token, symbol = row.get("pSymbol"), row.get("pTrdSymbol")
        lot = integer(row.get("lLotSize"), positive=True)
        if (
            token is None
            or isinstance(token, bool)
            or not str(token).strip()
            or not isinstance(symbol, str)
            or not symbol
            or lot is None
        ):
            raise CommodityDataError("COMMODITY_MASTER_IDENTITY_INCOMPLETE")
        records.append({**row, "resolved_expiry": expiry, "resolved_token": str(token), "resolved_lot": lot})
    futures = [
        row
        for row in records
        if str(row.get("pInstType", "")).upper().startswith("FUT") and str(row.get("pOptionType", "")).upper() == "XX"
    ]
    if not futures:
        raise CommodityDataError("FUTURE_UNRESOLVED")
    if renewal_policy is not None:
        from app.commodity_acts.contracts import choose, RenewalError

        try:
            future, options = choose(records, spec, renewal_policy, at, required_options)
        except RenewalError as exc:
            raise CommodityDataError(str(exc)) from None
        futures = [future]
    else:
        first = min(row["resolved_expiry"] for row in futures)
        futures = [row for row in futures if row["resolved_expiry"] == first]
        if len(futures) != 1:
            raise CommodityDataError("AMBIGUOUS_COMMODITY_FUTURE")
        options = [
            row
            for row in records
            if str(row.get("pInstType", "")).upper().startswith("OPT")
            and str(row.get("pOptionType", "")).upper() in ("CE", "PE")
        ]
        if not options:
            raise CommodityDataError("OPTION_EXPIRY_UNRESOLVED")
        expiry = min(row["resolved_expiry"] for row in options)
        options = [row for row in options if row["resolved_expiry"] == expiry]
    seen, tokens = set(), {futures[0]["resolved_token"]}
    for row in options:
        raw = number(row.get("dStrikePrice;"), positive=True)
        if raw is None:
            raise CommodityDataError("COMMODITY_STRIKE_METADATA_UNAVAILABLE")
        row["resolved_strike"] = float(Decimal(str(raw)) / 100)
        identity = row["resolved_strike"], str(row["pOptionType"]).upper()
        if identity in seen or row["resolved_token"] in tokens:
            raise CommodityDataError("AMBIGUOUS_COMMODITY_OPTION")
        seen.add(identity)
        tokens.add(row["resolved_token"])
    if len({row["resolved_lot"] for row in options}) != 1:
        raise CommodityDataError("COMMODITY_OPTION_LOT_SIZE_AMBIGUOUS")
    return futures[0], options


class KotakCommodityMarketData:
    def __init__(
        self,
        client: MarketReadClient,
        *,
        strikes_each_side: int = 10,
        clock: Callable[[], datetime] = lambda: datetime.now(IST),
        max_quote_age_seconds: int = 30,
        renewal_policy=None,
        master_refresh_seconds: int = 1800,
        request_budget=None,
    ):
        assert_market_only(client)
        if not 5 <= strikes_each_side <= 10:
            raise CommodityDataError("COMMODITY_CAPTURE_RANGE_INVALID")
        self._client, self._clock = client, clock
        self._range, self._max_age = strikes_each_side, max_quote_age_seconds
        self._master_cache: dict[tuple, tuple] = {}
        self._renewal_policy = renewal_policy
        self._daily_rows: dict[tuple, Any] = {}
        self._master_verified_at: datetime | None = None
        if not 60 <= master_refresh_seconds <= 86400:
            raise CommodityDataError("COMMODITY_MASTER_REFRESH_INVALID")
        self._master_refresh_seconds, self._request_budget = master_refresh_seconds, request_budget

    def _call(self, method: Callable, **kwargs):
        try:
            return (
                self._request_budget.call(method, **kwargs) if self._request_budget is not None else method(**kwargs)
            )
        except Exception as exc:
            from app.commodity_acts.request_budget import BudgetUnavailable

            if isinstance(exc, BudgetUnavailable):
                raise CommodityDataError(str(exc)) from None
            raise CommodityDataError("COMMODITY_MARKET_REQUEST_FAILED") from None

    def _quotes(self, records: list[dict]) -> dict:
        result = {}
        for offset in range(0, len(records), 50):
            if offset:
                sleep(0.04)
            batch = records[offset:offset + 50]
            requested = {row["resolved_token"] for row in batch}
            symbols = {row["resolved_token"]: row["pTrdSymbol"] for row in batch}
            response = self._call(
                self._client.quotes,
                instrument_tokens=[
                    {"exchange_segment": SEGMENT, "instrument_token": token} for token in sorted(requested)
                ],
                quote_type="all",
            )
            rows = response.get("data") if isinstance(response, dict) else response
            if not isinstance(rows, list):
                raise CommodityDataError("COMMODITY_QUOTES_UNAVAILABLE")
            for row in rows:
                if not isinstance(row, dict) or row.get("exchange") != SEGMENT:
                    raise CommodityDataError("COMMODITY_QUOTE_IDENTITY_MISMATCH")
                token = str(row.get("exchange_token", ""))
                if token not in requested or token in result:
                    raise CommodityDataError("COMMODITY_DUPLICATE_OR_UNREQUESTED_QUOTE")
                if row.get("display_symbol") is not None and row["display_symbol"] != symbols[token]:
                    raise CommodityDataError("COMMODITY_QUOTE_IDENTITY_MISMATCH")
                result[token] = row
        return result

    def capture(self, spec: InstrumentSpec, *, required_options=()) -> CommoditySnapshot:
        started = self._clock()
        key = spec.instrument_id, spec.underlying, started.date()
        if self._renewal_policy is not None:
            if (
                key not in self._daily_rows
                or self._master_verified_at is None
                or not 0 <= (started - self._master_verified_at).total_seconds() < self._master_refresh_seconds
            ):
                response = self._call(
                    self._client.search_scrip, exchange_segment=SEGMENT, symbol=spec.underlying, ignore_50multiple=True
                )
                self._daily_rows = {key: response}
                self._master_verified_at = self._clock()
            self._master_cache = {
                key: resolve_master(
                    self._daily_rows[key],
                    spec,
                    started.date(),
                    renewal_policy=self._renewal_policy,
                    at=started,
                    required_options=required_options,
                )
            }
        elif key not in self._master_cache:
            response = self._call(
                self._client.search_scrip, exchange_segment=SEGMENT, symbol=spec.underlying, ignore_50multiple=True
            )
            self._master_cache = {key: resolve_master(response, spec, started.date())}
        future, options = self._master_cache[key]
        renewal = None
        if self._renewal_policy is not None:
            from app.commodity_acts.contracts import evidence

            renewal = evidence(future, options, spec, self._master_verified_at)
        quote = self._quotes([future]).get(future["resolved_token"], {})
        future_price = number(quote.get("ltp"), positive=True)
        if future_price is None:
            raise CommodityDataError("FUTURE_UNRESOLVED")
        strikes = sorted({row["resolved_strike"] for row in options})
        nearest = min(range(len(strikes)), key=lambda i: (abs(strikes[i] - future_price), strikes[i]))
        selected = set(strikes[max(0, nearest - self._range):nearest + self._range + 1])
        required_tokens = {
            row["instrument_token"]
            for row in required_options
            if row.get("exchange") == SEGMENT and str(row.get("expiry")) == options[0]["resolved_expiry"].isoformat()
        }
        options = [
            row for row in options if row["resolved_strike"] in selected or row["resolved_token"] in required_tokens
        ]
        quotes = self._quotes(options)
        if not quotes:
            raise CommodityDataError("OPTION_CHAIN_UNAVAILABLE")
        received = self._clock()
        source = parse_quote_timestamp(quote.get("lstup_time"))
        if source is not None and not 0 <= (received - source).total_seconds() <= self._max_age:
            raise CommodityDataError("COMMODITY_REFERENCE_TIMESTAMP_INVALID")
        contracts = []
        for row in options:
            data = quotes.get(row["resolved_token"])
            if data is None:
                continue  # Do not manufacture a quote for an unreturned contract.
            depth = data.get("depth") if isinstance(data.get("depth"), dict) else {}
            bid, bid_qty = _touch(depth, "buy")
            ask, ask_qty = _touch(depth, "sell")
            contracts.append(
                CommodityOption(
                    strike=row["resolved_strike"],
                    raw_master_strike=float(row["dStrikePrice;"]),
                    raw_master_tick_size=number(row.get("dTickSize")),
                    tick_size=None,
                    option_type=cast(OptionType, str(row["pOptionType"]).upper()),
                    expiry=row["resolved_expiry"],
                    trading_symbol=row["pTrdSymbol"],
                    instrument_token=row["resolved_token"],
                    lot_size=row["resolved_lot"],
                    ltp=number(data.get("ltp"), positive=True),
                    open_interest=integer(data.get("open_int")),
                    volume=integer(data.get("last_volume")),
                    bid=bid,
                    ask=ask,
                    bid_quantity=bid_qty,
                    ask_quantity=ask_qty,
                    raw_source_timestamp=raw_timestamp(data.get("lstup_time")),
                    source_market_timestamp=parse_quote_timestamp(data.get("lstup_time")),
                    depth_unit="UNITS",
                )
            )
        if not contracts:
            raise CommodityDataError("OPTION_CHAIN_UNAVAILABLE")
        return CommoditySnapshot(
            instrument_id=spec.instrument_id,
            session_id=f"MCX_{spec.instrument_id}_{received.date().isoformat()}",
            timestamp_ist=received,
            request_started_at=started,
            response_received_at=received,
            reference_price=future_price,
            future_price=future_price,
            future_instrument_id=future["pTrdSymbol"],
            future_token=future["resolved_token"],
            future_expiry=future["resolved_expiry"],
            future_lot_size=future["resolved_lot"],
            source_market_timestamp=source,
            raw_future_source_timestamp=raw_timestamp(quote.get("lstup_time")),
            expiry=contracts[0].expiry,
            lot_size=contracts[0].lot_size,
            atm_strike=strikes[nearest],
            options=sorted(contracts, key=lambda item: (item.strike, item.option_type.value)),
            provenance={
                "identity": "KOTAK_MCX_SCRIP_SEARCH_EXACT_MASTER_MATCH",
                "quotes": "KOTAK_REST_ALL",
                "configured_alias": spec.underlying,
                "resolved_underlying": future["pSymbolName"],
                "expiry": "SDK_DDMMMYYYY_CHECKED_AGAINST_MCX_UNIX_EXPIRY",
                "strike": "SDK_3_0_7_SCRIP_SEARCH_DSTRIKEPRICE_DIV100",
                "depth_unit": "OPERATOR_CONFIRMED_KOTAK_MCX_UNDERLYING_UNITS_2026_10_22_NO_CONVERSION",
                "tick_size": "UNCONFIRMED_RAW_MASTER_RETAINED",
                "option_future_link": "SAME_MASTER_UNDERLYING_NEAREST_EXPIRIES_LINK_NOT_CONFIRMED",
                **(
                    {
                        "option_future_link": "KOTAK_PASSETCODE_EXACT_FUTURE_TOKEN",
                        "tick_size": "MASTER_PRECISION_VERIFIED",
                        "contract_evidence": renewal.model_dump_json(),
                    }
                    if renewal is not None
                    else {}
                ),
            },
        )
