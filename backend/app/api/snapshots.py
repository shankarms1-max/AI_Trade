from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.repositories import SnapshotRepository
from app.db.session import get_session_factory

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])


def get_snapshot_repository() -> SnapshotRepository:
    return SnapshotRepository(get_session_factory())


RepositoryDependency = Annotated[SnapshotRepository, Depends(get_snapshot_repository)]


@router.get("/latest")
def latest_snapshot(repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.latest()
    if result is None:
        raise HTTPException(status_code=404, detail="No snapshots found")
    return result


@router.get("")
def list_snapshots(
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    return repository.list(limit)


@router.get("/{snapshot_id}")
def snapshot_detail(snapshot_id: int, repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.get(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return result
