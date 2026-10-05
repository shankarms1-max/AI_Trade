"""Build deterministic hypothetical credit-spread candidates from persisted data."""

import argparse
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.strategy.repository import StrategyRepository  # noqa: E402
from app.strategy.service import (  # noqa: E402
    backfill_candidates,
    build_and_store_candidates,
    config_from_settings,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    group = value.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true")
    group.add_argument("--snapshot-id", type=int)
    group.add_argument("--all", action="store_true")
    return value


def print_summary(result) -> None:
    print("STRATEGY_CANDIDATES_BUILT")
    print(f"snapshot_id={result.snapshot_id}")
    print(f"strategy_version={result.strategy_version}")
    print(f"regime={result.regime}")
    print(f"strategy_type={result.strategy_type.value}")
    print(f"eligible={str(result.eligible).lower()}")
    print(f"candidate_count={result.candidate_count}")
    print(f"reason_codes={','.join(result.reason_codes) or 'none'}")
    print(f"warnings={','.join(result.warnings) or 'none'}")


def main() -> int:
    args = parser().parse_args()
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    repository = StrategyRepository(
        build_session_factory(build_engine(settings.database_url.get_secret_value())),
        regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version
    )
    try:
        config = config_from_settings(settings)
        if args.all:
            results = backfill_candidates(repository, config)
            print(f"STRATEGY_BACKFILL_COMPLETE snapshots={len(results)}")
            return 0
        snapshot_id = args.snapshot_id
        enforce_freshness = bool(args.latest)
        if args.latest:
            snapshot_id = repository.latest_context_snapshot_id()
            if snapshot_id is None:
                raise LookupError("no phase4_v1 result found")
        result = build_and_store_candidates(
            repository, snapshot_id, config, enforce_freshness=enforce_freshness
        )
    except (LookupError, ValueError) as exc:
        print(f"Strategy candidate build failed: {exc}", file=sys.stderr)
        return 1
    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
