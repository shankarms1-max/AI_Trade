from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.session import get_session_factory
from app.pipeline.repository import PipelineRepository

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


def get_pipeline_repository() -> PipelineRepository:
    return PipelineRepository(get_session_factory())


RepositoryDependency = Annotated[PipelineRepository, Depends(get_pipeline_repository)]


@router.get("/latest")
def latest_pipeline(repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.latest()
    if result is None:
        raise HTTPException(status_code=404, detail="No pipeline runs found")
    return result


@router.get("/{snapshot_id}")
def pipeline_by_snapshot(snapshot_id: int, repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.get(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    return result


@router.get("")
def list_pipeline_runs(
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    return repository.list(limit)
