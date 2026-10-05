from app.core.config import get_settings
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.session import get_session_factory
from app.shadow.analytics import breakdown, performance
from app.shadow.repository import ShadowRepository

router = APIRouter(prefix="/api/shadow", tags=["shadow"])


def get_shadow_repository() -> ShadowRepository:
    return ShadowRepository(get_session_factory(), regime_version=get_settings().active_regime_version, strategy_version=get_settings().active_strategy_version)


RepositoryDependency = Annotated[ShadowRepository, Depends(get_shadow_repository)]


@router.get("/latest")
def latest_shadow(repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.latest()
    if result is None:
        raise HTTPException(status_code=404, detail="No shadow trades found")
    return result


@router.get("/trades")
def list_shadow_trades(
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    return repository.list_trades(limit)


@router.get("/trades/{trade_id}")
def shadow_trade(trade_id: int, repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.get_trade(trade_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Shadow trade not found")
    return result


@router.get("/trades/{trade_id}/marks")
def shadow_marks(trade_id: int, repository: RepositoryDependency) -> list[dict[str, Any]]:
    if repository.get_trade(trade_id) is None:
        raise HTTPException(status_code=404, detail="Shadow trade not found")
    return repository.marks(trade_id)


@router.get("/performance")
def shadow_performance(repository: RepositoryDependency) -> dict[str, Any]:
    return performance(repository.all_trades())


@router.get("/performance/breakdown")
def shadow_breakdown(repository: RepositoryDependency) -> dict[str, Any]:
    return breakdown(repository.all_trades())
