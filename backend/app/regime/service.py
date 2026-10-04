from app.regime.engine import RegimeConfig, classify_regime
from app.regime.models import RegimeResult
from app.regime.repository import RegimeRepository


def build_and_store_regime(
    repository: RegimeRepository, snapshot_id: int, config: RegimeConfig
) -> RegimeResult:
    loaded = repository.load_feature(snapshot_id)
    if loaded is None:
        raise LookupError(f"phase3_v1 features for snapshot {snapshot_id} not found")
    feature_id, feature = loaded
    prior = repository.load_prior_feature(feature.timestamp)
    result = classify_regime(feature_id, feature, prior, config)
    repository.upsert(result)
    return result


def backfill_regimes(
    repository: RegimeRepository, config: RegimeConfig
) -> list[RegimeResult]:
    return [
        build_and_store_regime(repository, snapshot_id, config)
        for snapshot_id in repository.feature_snapshot_ids_chronological()
    ]
