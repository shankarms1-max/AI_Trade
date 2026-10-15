"""Read-only Phase 15 strategy monitor API."""
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.parallel_paper.monitor import summary
from app.scalper.repository import IST

router = APIRouter(prefix="/api/phase15", tags=["phase15"])


@router.get("/summary")
def phase15_summary(
    sessions: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> dict[str, Any]:
    try:
        return summary(sessions, datetime.now(IST))
    except (OperationalError, ProgrammingError) as exc:
        raise HTTPException(status_code=503, detail="Phase 15 monitor unavailable") from exc
