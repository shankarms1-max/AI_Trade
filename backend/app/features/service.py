from app.features.engine import FeatureEngineConfig, build_market_features
from app.features.models import MarketFeatureSnapshot
from app.features.repository import FeatureRepository


def build_and_store_features(
    repository: FeatureRepository,
    snapshot_id: int,
    config: FeatureEngineConfig,
) -> MarketFeatureSnapshot:
    snapshot = repository.load_raw(snapshot_id)
    if snapshot is None:
        raise LookupError(f"raw snapshot {snapshot_id} not found")
    history = repository.load_history_before(snapshot.timestamp_ist)
    feature = build_market_features(snapshot_id, snapshot, history, config)
    repository.upsert(feature)
    return feature


def backfill_features(
    repository: FeatureRepository, config: FeatureEngineConfig
) -> list[MarketFeatureSnapshot]:
    return [
        build_and_store_features(repository, snapshot_id, config)
        for snapshot_id in repository.raw_ids_chronological()
    ]
