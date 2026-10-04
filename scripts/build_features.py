"""Build deterministic Phase 3 features from persisted raw snapshots only."""

import argparse
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.features.engine import config_from_settings  # noqa: E402
from app.features.repository import FeatureRepository  # noqa: E402
from app.features.service import backfill_features, build_and_store_features  # noqa: E402


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    group = value.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true")
    group.add_argument("--snapshot-id", type=int)
    group.add_argument("--all", action="store_true")
    return value


def print_summary(feature) -> None:
    print("FEATURES_BUILT")
    print(f"snapshot_id={feature.snapshot_id}")
    print(f"feature_version={feature.feature_version}")
    print(f"spot={feature.spot}")
    print(f"local_pcr_oi={feature.pcr_features.local_pcr_oi}")
    print(f"top_put_oi={len(feature.oi_features.top_put_oi)}")
    print(f"top_call_oi={len(feature.oi_features.top_call_oi)}")
    print(f"support_clusters={len(feature.support_resistance.potential_support_clusters)}")
    print(f"resistance_clusters={len(feature.support_resistance.potential_resistance_clusters)}")
    print(f"intraday_oi_usable={str(feature.data_quality.intraday_oi_usable).lower()}")


def main() -> int:
    args = parser().parse_args()
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    repository = FeatureRepository(
        build_session_factory(build_engine(settings.database_url.get_secret_value()))
    )
    config = config_from_settings(settings)
    try:
        if args.all:
            features = backfill_features(repository, config)
            print(f"FEATURE_BACKFILL_COMPLETE snapshots={len(features)}")
            return 0
        snapshot_id = args.snapshot_id
        if args.latest:
            snapshot_id = repository.latest_raw_id()
            if snapshot_id is None:
                raise LookupError("no raw snapshots found")
        feature = build_and_store_features(repository, snapshot_id, config)
    except (LookupError, ValueError) as exc:
        print(f"Feature build failed: {exc}", file=sys.stderr)
        return 1
    print_summary(feature)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
