"""Read-only Phase 15 strategy monitor API."""
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_session_factory
from app.parallel_paper.monitor import summary
from app.parallel_paper.trade_monitor import trades
from app.scalper.repository import IST

router = APIRouter(prefix="/api/phase15", tags=["phase15"])
Strategy = Literal["ACTS_V2", "ORB_15", "ORB_30", "ORB_60", "EMA_PULLBACK", "SUPERTREND_RSI_VOLUME"]
TradeState = Literal["PENDING_PAPER_ENTRY", "OPEN", "CLOSED", "ENTRY_REJECTED", "UNRESOLVED_EXPOSURE"]


@router.get("/summary")
def phase15_summary(
    sessions: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> dict[str, Any]:
    try:
        return summary(sessions, datetime.now(IST))
    except (OperationalError, ProgrammingError) as exc:
        raise HTTPException(status_code=503, detail="Phase 15 monitor unavailable") from exc


@router.get("/positions")
def phase15_positions(
    sessions: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
) -> dict[str, Any]:
    try:
        return trades(sessions, datetime.now(IST), positions=True, limit=limit, offset=offset)
    except (OperationalError, ProgrammingError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="Phase 15 positions unavailable") from exc


@router.get("/journal")
def phase15_journal(
    sessions: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    day: Annotated[date | None, Query(alias="date")] = None,
    strategy: Strategy | None = None,
    state: TradeState | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
) -> dict[str, Any]:
    try:
        return trades(sessions, datetime.now(IST), day=day, strategy=strategy,
                      state=state, limit=limit, offset=offset)
    except (OperationalError, ProgrammingError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="Phase 15 journal unavailable") from exc
