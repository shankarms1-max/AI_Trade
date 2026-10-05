from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.ai.repository import AIResearchRepository
from app.core.config import get_settings
from app.db.session import get_session_factory

router = APIRouter(prefix="/api/ai-research", tags=["ai-research"])


def get_ai_research_repository() -> AIResearchRepository:
    return AIResearchRepository(get_session_factory(), regime_version=get_settings().active_regime_version, strategy_version=get_settings().active_strategy_version)


RepositoryDependency = Annotated[
    AIResearchRepository, Depends(get_ai_research_repository)
]


@router.get("/latest")
def latest_ai_research(repository: RepositoryDependency) -> dict[str, Any]:
    result = repository.latest()
    if result is None:
        raise HTTPException(status_code=404, detail="No AI research found")
    return result


@router.get("")
def list_ai_research(
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    return repository.list(limit)


@router.get("/{snapshot_id}")
def ai_research_detail(
    snapshot_id: int, repository: RepositoryDependency
) -> dict[str, Any]:
    result = repository.get(snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail="AI research not found")
    return result
