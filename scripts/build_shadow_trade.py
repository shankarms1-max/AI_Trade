"""Create one observed-price shadow entry from persisted approved risk research."""
import argparse
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.shadow.repository import ShadowRepository  # noqa: E402
from app.shadow.service import build_shadow_entry, config_from_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true")
    group.add_argument("--snapshot-id", type=int)
    args = parser.parse_args()
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    repository = ShadowRepository(build_session_factory(build_engine(settings.database_url.get_secret_value())))
    snapshot_id = args.snapshot_id
    if args.latest:
        snapshot_id = repository.latest_risk_snapshot_id()
        if snapshot_id is None:
            print("Shadow entry failed: no risk decision found", file=sys.stderr)
            return 1
    result = build_shadow_entry(repository, snapshot_id, config_from_settings(settings))
    print("SHADOW_ENTRY_CREATED" if result.created else "SHADOW_ENTRY_NOT_CREATED")
    print(f"snapshot_id={snapshot_id}")
    print(f"reason_codes={','.join(result.reason_codes)}")
    if result.trade:
        print(f"shadow_trade_id={result.trade.id}")
        print(f"strategy_type={result.trade.strategy_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
