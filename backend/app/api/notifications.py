from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.notifications.repository import NotificationRepository

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
SessionFactoryDependency = Annotated[sessionmaker[Session], Depends(get_session_factory)]


@router.get("")
def list_notifications(
    sessions: SessionFactoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    status: str | None = None, event_code: str | None = None,
    date_value: Annotated[date | None, Query(alias="date")] = None,
) -> list[dict[str, Any]]:
    return NotificationRepository(sessions).list(
        limit, status=status, event_code=event_code, day=date_value
    )
