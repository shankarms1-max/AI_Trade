from app.alpha.engine import AlphaConfig, build_alpha_features
from app.alpha.repository import AlphaRepository
from app.alpha.models import CalculationMode


def build_and_store_alpha(repository: AlphaRepository, snapshot_id: int, config: AlphaConfig,
                          mode: CalculationMode = CalculationMode.HISTORICAL_REPLAY):
    snapshot = repository.load_raw(snapshot_id)
    if snapshot is None:
        raise LookupError(f"raw snapshot {snapshot_id} not found")
    history = repository.load_history_before(snapshot.timestamp_ist)
    prior_alpha = repository.load_alpha_before(snapshot.timestamp_ist, mode=mode)
    result = build_alpha_features(snapshot_id, snapshot, history, prior_alpha, config, mode)
    repository.upsert(result)
    return result


def backfill_alpha(repository: AlphaRepository, config: AlphaConfig):
    return [build_and_store_alpha(repository, snapshot_id, config)
            for snapshot_id in repository.raw_ids_chronological()]
