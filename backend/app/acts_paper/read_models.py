"""Public ACTS-only views; full parallel research projections remain available."""
from datetime import date, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.parallel_paper.monitor import summary as full_summary
from app.parallel_paper.trade_monitor import trades as full_trades


def summary(sessions: sessionmaker[Session], at: datetime) -> dict:
    return full_summary(sessions, at, acts_only=True)


def trades(sessions: sessionmaker[Session], at: datetime, *, positions: bool = False,
           day: date | None = None, strategy: str | None = None, state: str | None = None,
           limit: int = 100, offset: int = 0) -> dict:
    return full_trades(sessions, at, positions=positions, day=day, strategy=strategy,
                       state=state, limit=limit, offset=offset, acts_only=True)
