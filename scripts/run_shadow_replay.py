"""Chronologically replay persisted snapshots through the shadow lifecycle."""
import argparse
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.shadow.repository import ShadowRepository  # noqa: E402
from app.shadow.service import config_from_settings, replay_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", required=True)
    args = parser.parse_args()
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    repository = ShadowRepository(build_session_factory(build_engine(settings.database_url.get_secret_value())), regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version)
    result = replay_all(repository, config_from_settings(settings))
    print("SHADOW_REPLAY_COMPLETE")
    print(f"snapshots={result['snapshots']}")
    print(f"entries={result['entries']}")
    print(f"updates={result['updates']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
