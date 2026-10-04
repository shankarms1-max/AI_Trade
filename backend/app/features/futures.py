from app.features.models import FuturesFeatures


def calculate_futures_features(spot: float, future: float | None) -> FuturesFeatures:
    if future is None:
        return FuturesFeatures(
            future_available=False, futures_basis=None, futures_basis_pct=None
        )
    basis = future - spot
    return FuturesFeatures(
        future_available=True,
        futures_basis=basis,
        futures_basis_pct=basis / spot * 100 if spot else None,
    )
