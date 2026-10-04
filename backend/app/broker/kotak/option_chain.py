from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any, NoReturn

from app.broker.base import MarketDataError
from app.data.models import OptionContractSnapshot, OptionType

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "consumerkey",
    "accesstoken",
    "sessiontoken",
    "neofinkey",
    "token",
    "secret",
    "password",
    "mobile",
    "totp",
    "mpin",
    "ucc",
    "sid",
    "rid",
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)\b(authorization|consumer[_ -]?key|access[_ -]?token|session[_ -]?token|"
    r"neo[_ -]?fin[_ -]?key|token|secret|password|mobile(?:[_ -]?number)?|totp|mpin|"
    r"ucc|sid|rid)\b\s*[:=]\s*([^\s,;\]}]+)"
)
_JWT_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\b")
_ALLOWED_CONTRACT_VALUE_KEYS = {
    "strike",
    "strikeprice",
    "optiontype",
    "type",
    "tradingsymbol",
    "tsym",
    "instrumenttoken",
    "token",
    "neosymbol",
    "scriptoken",
    "ltp",
    "lasttradedprice",
    "oi",
    "openinterest",
    "previousoi",
    "oichange",
    "volume",
    "expiry",
}


def _is_sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            ("[REDACTED_FIELD]" if _is_sensitive_key(key) else str(key)): (
                "[REDACTED]" if _is_sensitive_key(key) else _redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str):
        redacted = _SENSITIVE_VALUE_PATTERN.sub(r"\1=[REDACTED]", value)
        return _JWT_PATTERN.sub("[REDACTED_TOKEN]", redacted)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def _safe_keys(value: Any) -> list[str] | None:
    if not isinstance(value, dict):
        return None
    return ["[REDACTED_FIELD]" if _is_sensitive_key(key) else str(key) for key in value]


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _is_allowed_contract_value(key: Any) -> bool:
    return _normalized_key(key) in _ALLOWED_CONTRACT_VALUE_KEYS


def _safe_contract_key(key: Any) -> str:
    if _is_allowed_contract_value(key):
        return str(key)
    return "[REDACTED_FIELD]" if _is_sensitive_key(key) else str(key)


def _safe_contract_sample_value(key: Any, value: Any) -> Any:
    if _is_allowed_contract_value(key) and (
        value is None or isinstance(value, (str, bool, int, float))
    ):
        return _redact_sensitive(value)
    if _is_sensitive_key(key):
        return "[REDACTED]"
    return f"<{type(value).__name__}>"


def _dict_structure(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"type": type(value).__name__, "keys": None, "value_types": None}
    return {
        "type": "dict",
        "keys": [_safe_contract_key(key) for key in value],
        "value_types": {
            _safe_contract_key(key): type(item).__name__ for key, item in value.items()
        },
    }


def summarize_contract_structure(item: Any) -> dict[str, Any]:
    """Summarize one contract without dumping unknown or sensitive values."""

    if not isinstance(item, dict):
        return {
            "type": type(item).__name__,
            "keys": None,
            "value_types": None,
            "nested_dicts": None,
            "safe_sample": None,
            "nested_safe_samples": None,
        }

    nested_dicts: dict[str, Any] = {}
    nested_safe_samples: dict[str, Any] = {}
    for key, value in item.items():
        if not isinstance(value, dict):
            continue
        safe_key = _safe_contract_key(key)
        nested_dicts[safe_key] = _dict_structure(value)
        nested_safe_samples[safe_key] = {
            _safe_contract_key(nested_key): _safe_contract_sample_value(
                nested_key, nested_value
            )
            for nested_key, nested_value in value.items()
        }

    return {
        "type": "dict",
        "keys": [_safe_contract_key(key) for key in item],
        "value_types": {
            _safe_contract_key(key): type(value).__name__ for key, value in item.items()
        },
        "nested_dicts": nested_dicts,
        "safe_sample": {
            _safe_contract_key(key): _safe_contract_sample_value(key, value)
            for key, value in item.items()
        },
        "nested_safe_samples": nested_safe_samples,
    }


def _summarize_contract_side(value: Any) -> dict[str, Any]:
    first_item = value[0] if isinstance(value, list) and value else None
    return {
        "type": type(value).__name__,
        "length": len(value) if isinstance(value, list) else None,
        "first_item": summarize_contract_structure(first_item),
    }


def summarize_option_chain_structure(payload: Any) -> dict[str, Any]:
    """Return only structural metadata and allowlisted first-contract values."""

    payload_dict = payload if isinstance(payload, dict) else {}
    return {
        "payload_type": type(payload).__name__,
        "common_data": _dict_structure(payload_dict.get("common_data")),
        "spot": _dict_structure(payload_dict.get("spot")),
        "future": _dict_structure(payload_dict.get("future")),
        "call": _summarize_contract_side(payload_dict.get("call")),
        "put": _summarize_contract_side(payload_dict.get("put")),
    }


def summarize_option_chain_response(
    response: Any,
    *,
    expiry: str | None = None,
    exchange: str | None = None,
    underlying: str | None = None,
    instrument_type: str | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    """Return only approved diagnostics, recursively redacting sensitive fields."""

    response_dict = response if isinstance(response, dict) else {}
    data = response_dict.get("data")
    return {
        "response_type": type(response).__name__,
        "top_level_keys": _safe_keys(response),
        "stat": _redact_sensitive(response_dict.get("stat")),
        "stCode": _redact_sensitive(response_dict.get("stCode")),
        "errMsg": _redact_sensitive(response_dict.get("errMsg")),
        "desc": _redact_sensitive(response_dict.get("desc")),
        "error": _redact_sensitive(response_dict.get("error")),
        "data_present": "data" in response_dict,
        "data_keys": _safe_keys(data),
        "expiry": expiry,
        "exchange": exchange,
        "underlying": underlying,
        "instrument_type": instrument_type,
        "count": count,
    }


class KotakOptionChainError(MarketDataError):
    """A preserved Kotak no-data/error response with a safe string form."""

    def __init__(
        self,
        *,
        stat: Any = None,
        stCode: Any = None,
        errMsg: Any = None,
        desc: Any = None,
        error: Any = None,
        response: Any = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        self.stat = _redact_sensitive(stat)
        self.stCode = _redact_sensitive(stCode)
        self.errMsg = _redact_sensitive(errMsg)
        self.desc = _redact_sensitive(desc)
        self.error = _redact_sensitive(error)
        # Preserve the original object for in-process diagnosis. It is never
        # included in repr, str, logs, or command output.
        self.response = response
        self.diagnostic = diagnostic or {}
        super().__init__(
            "KotakOptionChainError("
            f"stat={self.stat!r}, stCode={self.stCode!r}, "
            f"errMsg={self.errMsg!r}, desc={self.desc!r})"
        )


def _raise_option_chain_error(
    response: Any,
    description: str,
    diagnostic: dict[str, Any] | None = None,
) -> NoReturn:
    response_dict = response if isinstance(response, dict) else {}
    raise KotakOptionChainError(
        stat=response_dict.get("stat"),
        stCode=response_dict.get("stCode"),
        errMsg=response_dict.get("errMsg"),
        desc=response_dict.get("desc") or description,
        error=response_dict.get("error"),
        response=response,
        diagnostic=diagnostic or summarize_option_chain_response(response),
    )


def extract_option_chain_payload(
    response: Any,
    *,
    diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract and strictly validate documented or observed Kotak chain payloads."""

    if not isinstance(response, dict):
        _raise_option_chain_error(response, "option-chain response is not a dictionary", diagnostic)

    failed_status = str(response.get("stat", "")).lower() in {
        "not_ok",
        "not ok",
        "error",
    }
    try:
        failed_code = int(response.get("stCode")) >= 400
    except (TypeError, ValueError):
        failed_code = False
    if failed_status or failed_code or response.get("error") or response.get("Error"):
        _raise_option_chain_error(response, "Kotak returned an option-chain error", diagnostic)

    if isinstance(response.get("data"), dict):
        payload = response["data"]
    elif "common_data" in response and ("call" in response or "put" in response):
        payload = response
    else:
        _raise_option_chain_error(
            response,
            "response is neither a documented wrapped nor observed top-level option chain",
            diagnostic,
        )

    if not isinstance(payload.get("common_data"), (dict, list)):
        _raise_option_chain_error(response, "option-chain common_data is invalid", diagnostic)

    call = payload.get("call", [])
    put = payload.get("put", [])
    if "call" in payload and not isinstance(call, list):
        _raise_option_chain_error(response, "option-chain call field is not a list", diagnostic)
    if "put" in payload and not isinstance(put, list):
        _raise_option_chain_error(response, "option-chain put field is not a list", diagnostic)
    if not call and not put:
        _raise_option_chain_error(response, "option-chain contains no call or put contracts", diagnostic)
    if any(not isinstance(contract, dict) for contract in [*call, *put]):
        _raise_option_chain_error(response, "option-chain contains a malformed contract", diagnostic)

    return payload


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and value.strip() in {"", "-"}):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _float_or_none(value: Any) -> float | None:
    number = _decimal_or_none(value)
    return float(number) if number is not None else None


def _int_or_none(value: Any) -> int | None:
    number = _decimal_or_none(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _instrument_token(neo_symbol: Any) -> str | None:
    if not isinstance(neo_symbol, str) or not neo_symbol:
        return None
    return neo_symbol.split("|", maxsplit=1)[-1]


def _normalized_option_type(value: Any) -> OptionType | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if normalized in {"CE", "CALL", "C"}:
        return OptionType.CE
    if normalized in {"PE", "PUT", "P"}:
        return OptionType.PE
    return None


def _iso_expiry_or_none(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or value.strip() in {"", "-"}:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _dict_field(
    item: dict[str, Any],
    field: str,
    *,
    response: dict[str, Any],
    location: str,
    required: bool = False,
    diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = item.get(field)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        _raise_option_chain_error(
            response, f"{location}.{field} must be a dictionary", diagnostic
        )
    return value


def _normalize_contract(
    item: dict[str, Any],
    *,
    container_type: OptionType,
    requested_expiry: date,
    response: dict[str, Any],
    location: str,
    diagnostic: dict[str, Any] | None,
) -> OptionContractSnapshot:
    if "inst" in item:
        instrument = _dict_field(
            item,
            "inst",
            response=response,
            location=location,
            required=True,
            diagnostic=diagnostic,
        )
        quote = _dict_field(
            item, "quote", response=response, location=location, diagnostic=diagnostic
        )
        oi = _dict_field(
            item, "oi", response=response, location=location, diagnostic=diagnostic
        )
        broker_type = _normalized_option_type(instrument.get("optType"))
        strike = _float_or_none(instrument.get("strkPrc"))
        contract_expiry = _iso_expiry_or_none(instrument.get("exp"))
        symbol = instrument.get("symbol")
        neo_symbol = instrument.get("neoSymbol")
        ltp = _float_or_none(quote.get("ltp"))
        current_oi = _int_or_none(oi.get("cur"))
        previous_oi = _int_or_none(oi.get("prev"))
        oi_change = _int_or_none(oi.get("chg"))
        volume = _int_or_none(quote.get("vol"))
        instrument_token = str(neo_symbol) if neo_symbol not in (None, "") else None
    elif "instrument" in item:
        instrument = _dict_field(
            item,
            "instrument",
            response=response,
            location=location,
            required=True,
            diagnostic=diagnostic,
        )
        quote = _dict_field(
            item, "quote", response=response, location=location, diagnostic=diagnostic
        )
        oi = _dict_field(
            item,
            "openInterest",
            response=response,
            location=location,
            diagnostic=diagnostic,
        )
        raw_type = instrument.get("optionType")
        broker_type = (
            container_type if raw_type is None else _normalized_option_type(raw_type)
        )
        strike = _float_or_none(instrument.get("strikePrice"))
        contract_expiry = requested_expiry
        symbol = instrument.get("symbol")
        neo_symbol = instrument.get("neoSymbol")
        ltp = _float_or_none(quote.get("ltp"))
        current_oi = _int_or_none(oi.get("current"))
        previous_oi = _int_or_none(oi.get("previous"))
        oi_change = _int_or_none(oi.get("change"))
        volume = _int_or_none(quote.get("volume"))
        instrument_token = _instrument_token(neo_symbol)
    else:
        _raise_option_chain_error(
            response, f"{location} has no recognized contract structure", diagnostic
        )

    if broker_type is None:
        _raise_option_chain_error(
            response, f"{location} has an invalid option type", diagnostic
        )
    if broker_type != container_type:
        _raise_option_chain_error(
            response, f"{location} option type contradicts its container", diagnostic
        )
    if strike is None or strike <= 0:
        _raise_option_chain_error(response, f"{location} has an invalid strike", diagnostic)
    if not isinstance(symbol, str) or not symbol.strip():
        _raise_option_chain_error(
            response, f"{location} has an invalid trading symbol", diagnostic
        )
    if contract_expiry is None:
        _raise_option_chain_error(response, f"{location} has an invalid expiry", diagnostic)
    if contract_expiry != requested_expiry:
        _raise_option_chain_error(
            response, f"{location} expiry does not match requested expiry", diagnostic
        )

    contract = OptionContractSnapshot(
        strike=strike,
        option_type=broker_type,
        expiry=contract_expiry,
        trading_symbol=symbol,
        instrument_token=instrument_token,
        ltp=ltp,
        open_interest=current_oi,
        previous_open_interest=previous_oi,
        change_in_open_interest=oi_change,
        volume=volume,
    )
    return contract


def option_chain_data_quality(
    contracts: list[OptionContractSnapshot],
) -> dict[str, int]:
    """Compute aggregate quality counters without changing broker values."""

    return {
        "contracts": len(contracts),
        "nonzero_volume": sum(
            contract.volume is not None and contract.volume != 0 for contract in contracts
        ),
        "nonzero_oi_change": sum(
            contract.change_in_open_interest is not None
            and contract.change_in_open_interest != 0
            for contract in contracts
        ),
        "oi_current_prev_different": sum(
            contract.open_interest is not None
            and contract.previous_open_interest is not None
            and contract.open_interest != contract.previous_open_interest
            for contract in contracts
        ),
        "oi_mismatch": sum(
            contract.open_interest is not None
            and contract.previous_open_interest is not None
            and contract.change_in_open_interest is not None
            and contract.open_interest - contract.previous_open_interest
            != contract.change_in_open_interest
            for contract in contracts
        ),
    }


def normalize_option_chain(
    response: dict[str, Any],
    expiry: date,
    *,
    diagnostic: dict[str, Any] | None = None,
) -> list[OptionContractSnapshot]:
    payload = extract_option_chain_payload(response, diagnostic=diagnostic)

    normalized: list[OptionContractSnapshot] = []
    for response_key, option_type in (("call", OptionType.CE), ("put", OptionType.PE)):
        contracts = payload.get(response_key) or []

        for index, item in enumerate(contracts):
            normalized.append(
                _normalize_contract(
                    item,
                    container_type=option_type,
                    requested_expiry=expiry,
                    response=response,
                    location=f"{response_key}[{index}]",
                    diagnostic=diagnostic,
                )
            )
    if not normalized:
        _raise_option_chain_error(
            response, "option-chain contains no normalizable contracts", diagnostic
        )
    return sorted(normalized, key=lambda contract: (contract.strike, contract.option_type.value))

