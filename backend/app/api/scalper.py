"""Read-only isolated scalper status."""
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.scalper.repository import scalper_summary

router = APIRouter(prefix="/api/scalper", tags=["scalper"])
IST = ZoneInfo("Asia/Kolkata")


@router.get("/status")
def status(
    sessions: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> dict[str, Any]:
    settings = get_settings()
    return scalper_summary(sessions, settings, datetime.now(IST).date())
