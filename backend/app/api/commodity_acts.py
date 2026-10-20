"""Read-only, separately scoped MCX commodity ACTS research endpoints."""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from app.alpha.clock import IST
from app.commodity_acts.config import CommoditySettings, InstrumentId
from app.commodity_acts.read import decisions, summary
from app.db.session import get_session_factory

router = APIRouter(prefix="/api/commodity-acts", tags=["commodity-acts-research"])


def get_commodity_settings() -> CommoditySettings:
    return CommoditySettings()


@router.get("/status")
@router.get("/summary")
def commodity_summary(sessions: Annotated[sessionmaker[Session], Depends(get_session_factory)],
                      config: Annotated[CommoditySettings, Depends(get_commodity_settings)]):
    try:
        return summary(sessions, config, datetime.now(IST))
    except (OperationalError, ProgrammingError, ValueError, TypeError):
        raise HTTPException(status_code=503, detail="Commodity research unavailable") from None


@router.get("/decisions")
def commodity_decisions(sessions: Annotated[sessionmaker[Session], Depends(get_session_factory)],
                        instrument: InstrumentId, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                        offset: Annotated[int, Query(ge=0, le=100000)] = 0):
    try:
        return decisions(sessions, instrument, limit, offset)
    except (OperationalError, ProgrammingError, ValueError, TypeError):
        raise HTTPException(status_code=503, detail="Commodity decisions unavailable") from None
