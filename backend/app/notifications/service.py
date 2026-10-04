import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.notifications.models import (
    NotificationRequest, TelegramHealthStatus, TelegramSendResult,
)
from app.notifications.preferences import enabled_for
from app.notifications.repository import NotificationRepository
from app.notifications.telegram import TelegramClient

logger = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class NotificationService:
    def __init__(
        self, repository: NotificationRepository, settings: Any,
        client: TelegramClient | None = None, *, throttle_seconds: float = 0.25,
        sleeper=time.sleep,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self._client = client
        self._throttle_seconds = throttle_seconds
        self._sleeper = sleeper
        self._last_send = 0.0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    def _telegram(self) -> TelegramClient | None:
        if self._client is not None:
            return self._client
        if not self.configured:
            return None
        self._client = TelegramClient(
            self.settings.telegram_bot_token.get_secret_value(),
            self.settings.telegram_chat_id.get_secret_value(),
            timeout_seconds=self.settings.telegram_timeout_seconds,
            max_retries=self.settings.telegram_max_retries,
        )
        return self._client

    def notify(self, request: NotificationRequest) -> bool:
        if not self.settings.telegram_enabled or not enabled_for(self.settings, request.event_code):
            return False
        try:
            delivery, created = self.repository.reserve(request)
        except Exception as exc:
            logger.error("NOTIFICATION_RESERVATION_FAILED error_type=%s", type(exc).__name__)
            return False
        if not created:
            return False
        client = self._telegram()
        if client is None:
            try:
                self.repository.skip(delivery["id"], "Telegram is enabled but credentials are incomplete")
            except Exception as exc:
                logger.error("NOTIFICATION_SKIP_RECORD_FAILED error_type=%s", type(exc).__name__)
            return False
        try:
            with self._lock:
                wait = self._throttle_seconds - (time.monotonic() - self._last_send)
                if wait > 0:
                    self._sleeper(wait)
                result = client.send(request.message)
                self._last_send = time.monotonic()
            self.repository.finish(delivery["id"], result)
            return result.success
        except Exception as exc:
            logger.error("TELEGRAM_DELIVERY_FAILED error_type=%s", type(exc).__name__)
            from app.observability.models import safe_exception
            error_type, message = safe_exception(exc)
            try:
                self.repository.finish(delivery["id"], TelegramSendResult(
                    success=False, attempts=1, transient_failure=False,
                    safe_error_type=error_type, safe_error_message=message,
                ))
            except Exception as persist_exc:
                logger.error("NOTIFICATION_FAILURE_RECORD_FAILED error_type=%s",
                             type(persist_exc).__name__)
            return False

    def retry_failed(self, *, limit: int = 20, today: bool = False) -> dict[str, int]:
        rows = self.repository.retryable_failed(limit, today=today)
        sent = failed = 0
        client = self._telegram()
        if not self.settings.telegram_enabled or client is None:
            return {"eligible": len(rows), "sent": 0, "failed": len(rows)}
        for row in rows:
            try:
                result = client.send(row["rendered_message"])
                self.repository.finish(row["id"], result)
                sent += int(result.success)
                failed += int(not result.success)
            except Exception as exc:
                failed += 1
                logger.error("TELEGRAM_RETRY_FAILED error_type=%s", type(exc).__name__)
        return {"eligible": len(rows), "sent": sent, "failed": failed}

    def health(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(IST)).astimezone(IST)
        metrics = self.repository.health_metrics(current.date())
        if not self.settings.telegram_enabled:
            status = TelegramHealthStatus.DISABLED
        elif not self.configured:
            status = TelegramHealthStatus.DEGRADED
        elif metrics["deliveries_today"] == 0:
            status = TelegramHealthStatus.CONFIGURED_NOT_TESTED
        elif metrics["consecutive_failures"] >= 3:
            status = TelegramHealthStatus.FAILED
        elif metrics["consecutive_failures"]:
            status = TelegramHealthStatus.DEGRADED
        else:
            status = TelegramHealthStatus.HEALTHY
        return {"status": status.value, "enabled": self.settings.telegram_enabled,
                "configured": self.configured, **metrics}
