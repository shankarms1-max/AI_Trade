from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, sessionmaker

from app.alpha.repository import AlphaRepository
from app.db.session import get_session_factory

router = APIRouter(prefix="/api/alpha", tags=["alpha"])
SessionFactoryDependency = Annotated[sessionmaker[Session], Depends(get_session_factory)]


@router.get("/latest")
def latest(sessions: SessionFactoryDependency) -> dict[str, Any]:
    result = AlphaRepository(sessions).latest()
    if result is None:
        raise HTTPException(404, "alpha features not found")
    return result


@router.get("/history")
def history(
    sessions: SessionFactoryDependency,
    date_value: Annotated[date, Query(alias="date")],
) -> list[dict[str, Any]]:
    return AlphaRepository(sessions).history(date_value)


@router.get("")
def list_alpha(
    sessions: SessionFactoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    return AlphaRepository(sessions).list(limit)


@router.get("/{snapshot_id}")
def by_snapshot(snapshot_id: int, sessions: SessionFactoryDependency) -> dict[str, Any]:
    result = AlphaRepository(sessions).get(snapshot_id)
    if result is None:
        raise HTTPException(404, "alpha features not found")
    return result
