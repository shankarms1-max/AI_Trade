"""AST-only indexed scalar history. Load option chains only for the current observation."""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.adaptive_supertrend import CAPTURE_MODE
from app.adaptive_supertrend.bars import build_bars
from app.parallel_paper.bars import Bar, MarketPoint
from app.scalper.models import ScalperMarketSnapshotRecord as Snapshot
from app.scalper.repository import IST, _aware


def mode_clause():
    return Snapshot.context_json.like(f'%"mode":"{CAPTURE_MODE}"%')


def history(
    sessions: sessionmaker,
    at: datetime,
    session_count: int,
    gap: int,
    future_identity: tuple | None = None,
    *,
    current_only: bool = False,
) -> list[Bar]:
    result = []
    with sessions() as session:
        dates = (
            [at.date()]
            if current_only
            else session.scalars(
                select(func.date(Snapshot.captured_at))
                .where(mode_clause(), Snapshot.captured_at <= at)
                .distinct()
                .order_by(func.date(Snapshot.captured_at).desc())
                .limit(session_count)
            ).all()
        )
        reference = future_identity
        for day_value in sorted(dates, key=str):
            day = date.fromisoformat(str(day_value))
            start = datetime.combine(day, time(9, 15), IST)
            end = min(at, start.replace(hour=15, minute=30))
            rows = session.execute(
                select(
                    Snapshot.captured_at,
                    Snapshot.nifty_future,
                    Snapshot.future_instrument_id,
                    Snapshot.future_expiry,
                    Snapshot.request_started_at,
                    Snapshot.response_received_at,
                )
                .where(mode_clause(), Snapshot.captured_at >= start, Snapshot.captured_at <= end)
                .order_by(Snapshot.captured_at, Snapshot.id)
            ).all()
            points = [
                MarketPoint(
                    _aware(row.captured_at),
                    (
                        float(row.nifty_future)
                        if row.nifty_future is not None
                        and _aware(row.request_started_at)
                        <= _aware(row.response_received_at)
                        <= _aware(row.captured_at)
                        else None
                    ),
                    row.future_instrument_id,
                    row.future_expiry,
                )
                for row in rows
            ]
            if reference is not None:
                points = [
                    (
                        point
                        if (point.future_id, point.future_expiry) == reference
                        else MarketPoint(point.at, None, point.future_id, point.future_expiry)
                    )
                    for point in points
                ]
            result.extend(build_bars(points, end, gap))
    return result


def pending_ids(sessions: sessionmaker, after: datetime | None, limit: int = 200) -> list[int]:
    with sessions() as session:
        query = select(Snapshot.id).where(mode_clause())
        if after is not None:
            query = query.where(Snapshot.captured_at > after)
        return list(session.scalars(query.order_by(Snapshot.captured_at, Snapshot.id).limit(limit)))
