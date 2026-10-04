from decimal import Decimal, InvalidOperation
import re
from typing import Any

INDIA_VIX_IDENTIFIER = "INDIA VIX"
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(authorization|consumer[_ -]?key|access[_ -]?token|session[_ -]?token|"
    r"token|secret|password|mobile(?:[_ -]?number)?|totp|mpin|ucc|sid|rid)\b"
    r"\s*[:=]\s*([^\s,;\]}]+)"
)
_JWT = re.compile(r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\b")


def safe_vix_error(value: Any) -> str | None:
    """Return a bounded broker error string with credential patterns removed."""

    if value is None:
        return None
    if not isinstance(value, (str, int, float, bool)):
        return f"<{type(value).__name__}>"
    text = _SENSITIVE_VALUE.sub(r"\1=[REDACTED]", str(value))
    return _JWT.sub("[REDACTED_TOKEN]", text)[:300]


def quote_records(response: Any) -> list[dict[str, Any]]:
    records = response.get("data") if isinstance(response, dict) else response
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def parse_vix_ltp(response: Any) -> float | None:
    records = quote_records(response)
    if not records:
        return None
    value = records[0].get("ltp")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return float(number)


def summarize_vix_quote(response: Any, *, method: str) -> dict[str, Any]:
    """Summarize only safe index quote fields; never return a broker payload."""

    records = quote_records(response)
    first = records[0] if records else {}
    ltp = parse_vix_ltp(response)
    error_value = None
    if isinstance(response, dict):
        for key in ("errMsg", "desc", "message", "error", "Error"):
            if response.get(key) not in (None, ""):
                error_value = response[key]
                break
    return {
        "method": method,
        "success": ltp is not None,
        "response_type": type(response).__name__,
        "returned_symbol": next(
            (
                first[key]
                for key in ("trading_symbol", "tradingSymbol", "symbol", "tsym")
                if isinstance(first.get(key), (str, int))
            ),
            None,
        ),
        "returned_token": next(
            (
                first[key]
                for key in ("instrument_token", "token", "neoSymbol")
                if isinstance(first.get(key), (str, int))
            ),
            None,
        ),
        "ltp": ltp,
        "safe_error_message": safe_vix_error(error_value),
    }
