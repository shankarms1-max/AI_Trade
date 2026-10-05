"""Build deterministic Phase 4 regimes from persisted Phase 3 features."""

import argparse
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.regime.engine import config_from_settings  # noqa: E402
from app.regime.repository import RegimeRepository  # noqa: E402
from app.regime.service import backfill_regimes, build_and_store_regime  # noqa: E402


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    group = value.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true")
    group.add_argument("--snapshot-id", type=int)
    group.add_argument("--all", action="store_true")
    return value


def print_summary(result) -> None:
    print("REGIME_BUILT")
    print(f"snapshot_id={result.snapshot_id}")
    print(f"regime_version={result.regime_version}")
    print(f"regime={result.regime.value}")
    print(f"confidence={result.confidence}")
    print(f"evidence_quality={result.evidence_quality.value}")
    print(f"bull_score={result.bull_score}")
    print(f"bear_score={result.bear_score}")
    print(f"range_score={result.range_score}")
    print(f"warnings={','.join(result.warnings) or 'none'}")


def main() -> int:
    args = parser().parse_args()
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    repository = RegimeRepository(
        build_session_factory(build_engine(settings.database_url.get_secret_value())),
        regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version
    )
    config = config_from_settings(settings)
    try:
        if args.all:
            results = backfill_regimes(repository, config)
            print(f"REGIME_BACKFILL_COMPLETE snapshots={len(results)}")
            return 0
        snapshot_id = args.snapshot_id
        if args.latest:
            snapshot_id = repository.latest_feature_snapshot_id()
            if snapshot_id is None:
                raise LookupError("no phase3_v1 features found")
        result = build_and_store_regime(repository, snapshot_id, config)
    except (LookupError, ValueError) as exc:
        print(f"Regime build failed: {exc}", file=sys.stderr)
        return 1
    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
