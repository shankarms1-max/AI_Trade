from collections.abc import Callable
import time

import httpx

from app.notifications.models import TelegramSendResult
class TelegramClient:
    """Minimal Bot API client. The token-bearing endpoint is never logged."""

    def __init__(
        self, bot_token: str, chat_id: str, *, timeout_seconds: float = 10,
        max_retries: int = 2, client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client or httpx.Client()
        self._sleeper = sleeper

    def send(self, text: str) -> TelegramSendResult:
        attempts = 0
        for attempts in range(1, self._max_retries + 2):
            transient = False
            try:
                response = self._client.post(
                    self._endpoint,
                    json={"chat_id": self._chat_id, "text": text[:4096],
                          "disable_web_page_preview": True},
                    timeout=self._timeout,
                )
                if response.is_success and response.json().get("ok", False):
                    return TelegramSendResult(success=True, attempts=attempts)
                transient = response.status_code == 429 or response.status_code >= 500
                error = RuntimeError(f"Telegram HTTP {response.status_code}")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                transient = True
                error = exc
            except Exception as exc:
                error = exc
            if not transient or attempts > self._max_retries:
                error_type = type(error).__name__[:120]
                if isinstance(error, httpx.TimeoutException):
                    message = "Telegram request timed out"
                elif isinstance(error, httpx.TransportError):
                    message = "Telegram transport failed"
                elif isinstance(error, RuntimeError) and str(error).startswith("Telegram HTTP"):
                    message = str(error)
                else:
                    message = "Telegram request failed"
                return TelegramSendResult(
                    success=False, attempts=attempts, transient_failure=transient,
                    safe_error_type=error_type, safe_error_message=message,
                )
            self._sleeper(min(2 ** (attempts - 1), 5))
        raise AssertionError("unreachable")
