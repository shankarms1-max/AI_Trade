"""Send one safe outbound Telegram test message when explicitly enabled."""
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.notifications.models import (  # noqa: E402
    NotificationEventCode, NotificationPriority, NotificationRequest,
)
from app.notifications.repository import NotificationRepository  # noqa: E402
from app.notifications.service import NotificationService  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not settings.telegram_enabled:
        print("TELEGRAM_DISABLED")
        return 0
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    service = NotificationService(NotificationRepository(sessions), settings)
    if not service.configured:
        print("TELEGRAM_MISCONFIGURED")
        return 2
    stamp = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
    success = service.notify(NotificationRequest(
        event_code=NotificationEventCode.TELEGRAM_TEST,
        dedupe_key=f"TELEGRAM_TEST:{stamp}", priority=NotificationPriority.INFO,
        message="Nifty AI Research — Telegram test successful.",
        subject_ref_type="manual_test", subject_ref_id=stamp,
    ))
    print("TELEGRAM_TEST_SENT" if success else "TELEGRAM_TEST_FAILED")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
