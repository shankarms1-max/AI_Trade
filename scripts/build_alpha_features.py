"""Build deterministic Phase 14 alpha from persisted snapshots only."""
import argparse
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.alpha.engine import config_from_settings
from app.alpha.repository import AlphaRepository
from app.alpha.service import backfill_alpha, build_and_store_alpha
from app.core.config import get_settings
from app.db.session import build_engine, build_session_factory


def main() -> int:
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--latest", action="store_true")
    target.add_argument("--snapshot-id", type=int)
    target.add_argument("--all", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required")
    repository = AlphaRepository(build_session_factory(
        build_engine(settings.database_url.get_secret_value())
    ))
    config = config_from_settings(settings)
    if args.all:
        results = backfill_alpha(repository, config)
        print(f"ALPHA_BACKFILL_COMPLETE count={len(results)}")
        return 0
    snapshot_id = args.snapshot_id if args.snapshot_id is not None else repository.latest_raw_id()
    if snapshot_id is None:
        raise SystemExit("No persisted market snapshot found")
    result = build_and_store_alpha(repository, snapshot_id, config)
    print(
        "ALPHA_FEATURE_COMPLETE "
        f"snapshot_id={snapshot_id} status={result.status.value} "
        f"alpha1={result.alpha_1} alpha2={result.alpha_2}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
