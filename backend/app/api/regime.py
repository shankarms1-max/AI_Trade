from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.session import get_session_factory
from app.regime.repository import RegimeRepository

router = APIRouter(prefix="/api/regime", tags=["regime"])


def get_regime_repository() -> RegimeRepository:
    return RegimeRepository(get_session_factory())


RepositoryDependency = Annotated[RegimeRepository, Depends(get_regime_repository)]


@router.get("/latest")
def latest_regime(repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.latest()
    if result is None:
        raise HTTPException(status_code=404, detail="No regime results found")
    return result


@router.get("")
def list_regimes(
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    return repository.list(limit)


@router.get("/{snapshot_id}")
def regime_detail(snapshot_id: int, repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.get(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Regime result not found")
    return result
