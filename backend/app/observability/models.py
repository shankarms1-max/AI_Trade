from enum import Enum
import re
from typing import Any


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"
    IDLE = "IDLE"


class MarketSessionState(str, Enum):
    PRE_MARKET = "PRE_MARKET"
    OPEN = "OPEN"
    POST_MARKET = "POST_MARKET"
    CLOSED = "CLOSED"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"


class AIHealthStatus(str, Enum):
    DISABLED = "DISABLED"
    CONFIGURED_NOT_TESTED = "CONFIGURED_NOT_TESTED"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class EventSeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+"),
    re.compile(r"(?i)\b(api[_ -]?key|authorization|consumer[_ -]?key|token|secret|chat[_ -]?id|mpin|totp|password)\b\s*[:=]\s*[^\s,;]+"),
)


def safe_exception(error: BaseException) -> tuple[str, str]:
    """Return a bounded, redacted error suitable for persistence and APIs."""
    message = str(error).replace("\r", " ").replace("\n", " ")
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("[REDACTED]", message)
    if not message:
        message = "Operation failed"
    return type(error).__name__[:120], message[:500]


def public_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value
