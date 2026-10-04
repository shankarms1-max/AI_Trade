"""Retry only persisted transient Telegram failures; never recompute research."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.notifications.repository import NotificationRepository  # noqa: E402
from app.notifications.service import NotificationService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.limit <= 500:
        parser.error("--limit must be between 1 and 500")
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    result = NotificationService(
        NotificationRepository(sessions), settings
    ).retry_failed(limit=args.limit, today=args.today)
    print(f"eligible={result['eligible']}")
    print(f"sent={result['sent']}")
    print(f"failed={result['failed']}")
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
