"""Print a safe JSON operations summary for one IST date."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.observability.service import get_daily_operations_summary  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat)
    args = parser.parse_args()
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    day = args.date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    print(json.dumps(get_daily_operations_summary(sessions, settings, day), default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
