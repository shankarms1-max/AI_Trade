"""Check provenance alignment before reconstructing causal features from raw history."""
from app.features.engine import build_market_features
from app.research.quotes import finite


def feature_row_aligned(row):
    feature = row.feature
    return bool(feature is not None and row.feature_id is not None
                and feature.snapshot_id == row.snapshot_id
                and feature.timestamp == row.snapshot.timestamp_ist
                and feature.expiry == row.snapshot.expiry
                and finite(feature.spot) and feature.spot == row.snapshot.nifty_spot)


def reconstruct_feature(row, available_history, config):
    # Missing/misaligned stored provenance remains a missing feature observation.
    if not feature_row_aligned(row):
        return None
    return build_market_features(row.snapshot_id, row.snapshot, available_history, config)
