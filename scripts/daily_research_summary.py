"""Print deterministic daily research analytics."""

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.pipeline.service import get_daily_research_summary  # noqa: E402


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--date", type=date.fromisoformat)
    return value


def main() -> int:
    args = parser().parse_args()
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    trading_date = args.date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    print(json.dumps(get_daily_research_summary(sessions, trading_date), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
