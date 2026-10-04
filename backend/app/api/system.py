from datetime import date, datetime, time, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.observability.repository import ObservabilityRepository
from app.observability.service import ObservabilityService

router = APIRouter(tags=["system"])
IST = ZoneInfo("Asia/Kolkata")
SessionFactoryDependency = Annotated[sessionmaker[Session], Depends(get_session_factory)]


def _service(sessions: sessionmaker[Session]) -> ObservabilityService:
    return ObservabilityService(sessions, get_settings())


@router.get("/api/health")
def health(sessions: SessionFactoryDependency) -> dict[str, Any]:
    return _service(sessions).compact_health()


@router.get("/api/system/health")
def detailed_health(sessions: SessionFactoryDependency) -> dict[str, Any]:
    return _service(sessions).detailed_health()


@router.get("/api/system/metrics")
def system_metrics(sessions: SessionFactoryDependency) -> dict[str, Any]:
    return _service(sessions).metrics()


@router.get("/api/system/events")
def operational_events(
    sessions: SessionFactoryDependency,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    component: str | None = None, severity: str | None = None,
    event_code: str | None = None, date_value: Annotated[date | None, Query(alias="date")] = None,
) -> list[dict[str, Any]]:
    start = None if date_value is None else datetime.combine(date_value, time.min, IST)
    end = None if start is None else start + timedelta(days=1)
    return ObservabilityRepository(sessions).events(
        limit, component=component, severity=severity, event_code=event_code,
        start=start, end=end,
    )
