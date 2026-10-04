from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.session import get_session_factory
from app.features.repository import FeatureRepository

router = APIRouter(prefix="/api/features", tags=["features"])


def get_feature_repository() -> FeatureRepository:
    return FeatureRepository(get_session_factory())


RepositoryDependency = Annotated[FeatureRepository, Depends(get_feature_repository)]


@router.get("/latest")
def latest_features(repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.latest()
    if result is None:
        raise HTTPException(status_code=404, detail="No features found")
    return result


@router.get("")
def list_features(
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    return repository.list(limit)


@router.get("/{snapshot_id}")
def feature_detail(snapshot_id: int, repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.get(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Features not found")
    return result
