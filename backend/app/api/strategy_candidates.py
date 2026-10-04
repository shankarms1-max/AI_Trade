from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.session import get_session_factory
from app.strategy.repository import StrategyRepository

router = APIRouter(prefix="/api/strategy-candidates", tags=["strategy-candidates"])


def get_strategy_repository() -> StrategyRepository:
    return StrategyRepository(get_session_factory())


RepositoryDependency = Annotated[StrategyRepository, Depends(get_strategy_repository)]


@router.get("/latest")
def latest_candidates(repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.latest()
    if result is None:
        raise HTTPException(status_code=404, detail="No strategy candidate sets found")
    return result


@router.get("")
def list_candidates(
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    return repository.list(limit)


@router.get("/{snapshot_id}")
def candidate_detail(
    snapshot_id: int, repository: RepositoryDependency
) -> dict[str, Any]:
    result = repository.get(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Strategy candidate set not found")
    return result
