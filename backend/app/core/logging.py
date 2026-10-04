import logging
import re
from typing import Final

SENSITIVE_NAMES: Final = (
    "consumer_key",
    "access_token",
    "authorization",
    "mpin",
    "totp",
    "secret",
    "token",
    "chat_id",
    "telegram_bot",
)


class SecretRedactionFilter(logging.Filter):
    """A final guard against logging mapping fields with sensitive names."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in tuple(vars(record)):
            if any(name in key.lower() for name in SENSITIVE_NAMES):
                setattr(record, key, "[REDACTED]")
        if isinstance(record.msg, str):
            rendered = record.getMessage()
            record.msg = re.sub(
                r"(?i)\b(api[_ -]?key|authorization|consumer[_ -]?key|token|secret|chat[_ -]?id|mpin|totp|password)\b\s*[:=]\s*[^\s,;]+",
                "[REDACTED]", rendered,
            )
            record.msg = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+", "[REDACTED]", record.msg)
            record.args = ()
        return True


def configure_logging(
    level: str = "INFO", log_dir: str = "logs", max_bytes: int = 10_485_760,
    backup_count: int = 7,
) -> None:
    from app.observability.logging_config import configure_rotating_logging
    configure_rotating_logging(level, log_dir, max_bytes, backup_count)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

