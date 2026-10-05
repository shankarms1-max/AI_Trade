from app.core.config import get_settings
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.session import get_session_factory
from app.risk.repository import RiskRepository

router = APIRouter(prefix="/api/risk", tags=["risk"])


def get_risk_repository() -> RiskRepository:
    return RiskRepository(get_session_factory(), regime_version=get_settings().active_regime_version, strategy_version=get_settings().active_strategy_version)


RepositoryDependency = Annotated[RiskRepository, Depends(get_risk_repository)]


@router.get("/latest")
def latest_risk(repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.latest()
    if result is None:
        raise HTTPException(status_code=404, detail="No risk evaluations found")
    return result


@router.get("")
def list_risk(
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    return repository.list(limit)


@router.get("/{snapshot_id}/approved")
def approved_risk(snapshot_id: int, repository: RepositoryDependency) -> list[dict[str, Any]]:
    result = repository.get(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Risk evaluation not found")
    return [item for item in result["decisions"] if item["decision"] == "APPROVED"]


@router.get("/{snapshot_id}")
def risk_detail(snapshot_id: int, repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.get(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Risk evaluation not found")
    return result
