"""Provenance-aware AST scalar history; only native observations can be processed."""

from __future__ import annotations

from datetime import date, datetime, time
import json

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import sessionmaker

from app.adaptive_supertrend import BOOTSTRAP_MODE, CAPTURE_MODE, VERSION
from app.adaptive_supertrend.bars import build_bars
from app.adaptive_supertrend.bootstrap import validate_provenance
from app.parallel_paper.bars import Bar, MarketPoint
from app.scalper.models import ScalperMarketSnapshotRecord as Snapshot
from app.scalper.repository import IST, _aware


def mode_clause():
    # Deliberately native-only: pending IDs and paper processing must not broaden.
    return Snapshot.context_json.like(f'%"mode":"{CAPTURE_MODE}"%')


def paper_clause():
    return and_(mode_clause(), Snapshot.context_json.like(f'%"producer":"{VERSION}"%'))


def history_clause(at: datetime):
    return or_(
        mode_clause(),
        and_(
            Snapshot.context_json.like(f'%"mode":"{BOOTSTRAP_MODE}"%'),
            Snapshot.captured_at < datetime.combine(at.astimezone(IST).date(), time.min, IST),
        ),
    )


class HistoryBars(list[Bar]):
    """Ordinary bar list with separate input evidence, not indicator parameters."""

    def __init__(self) -> None:
        super().__init__()
        self.history_provenance: dict = {}


def history(
    sessions: sessionmaker,
    at: datetime,
    session_count: int,
    gap: int,
    future_identity: tuple | None = None,
    *,
    current_only: bool = False,
) -> list[Bar]:
    if at.tzinfo is None or at.utcoffset() is None or session_count < 1:
        raise ValueError("AST_AWARE_HISTORY_CLOCK_AND_POSITIVE_SESSION_COUNT_REQUIRED")
    at = at.astimezone(IST)
    result = HistoryBars()
    provenance: list[dict] = []
    with sessions() as session:
        assert session.bind is not None
        # PostgreSQL's session timezone must not decide the IST trading date.
        local_date = (
            func.date(func.timezone("Asia/Kolkata", Snapshot.captured_at))
            if (session.bind.dialect.name == "postgresql")
            else func.date(Snapshot.captured_at)
        )
        dates = (
            [(at.date(), 1)]
            if current_only
            else session.execute(
                select(local_date, func.max(case((mode_clause(), 1), else_=0)))
                .where(history_clause(at), Snapshot.captured_at <= at)
                .group_by(local_date)
                .order_by(local_date.desc())
                .limit(session_count)
            ).all()
        )
        reference = future_identity
        for day_value, native in sorted(dates, key=lambda row: str(row[0])):
            day = date.fromisoformat(str(day_value))
            mode = CAPTURE_MODE if native else BOOTSTRAP_MODE
            selected_mode = mode_clause() if native else Snapshot.context_json.like(f'%"mode":"{BOOTSTRAP_MODE}"%')
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
                    Snapshot.context_json,
                )
                .where(selected_mode, Snapshot.captured_at >= start, Snapshot.captured_at <= end)
                .order_by(Snapshot.captured_at, Snapshot.id)
            ).all()
            source_hashes: set[str] = set()
            if mode == BOOTSTRAP_MODE:
                for row in rows:
                    context = json.loads(row.context_json or "{}")
                    validate_provenance(context)
                    for key in ("captured_at", "request_started_at", "response_received_at"):
                        original = datetime.fromisoformat(context.get("original_timestamps", {}).get(key, ""))
                        if original.tzinfo is None or _aware(original) != _aware(getattr(row, key)):
                            raise ValueError("AST_BOOTSTRAP_ORIGINAL_TIMESTAMPS_CONFLICT")
                    source_hashes.add(context["source_file_sha256"])
            provenance.append(
                {
                    "trading_date": str(day),
                    "mode": mode,
                    "observation_count": len(rows),
                    "source_file_sha256s": sorted(source_hashes),
                    "first_observation": _aware(rows[0].captured_at).isoformat() if rows else None,
                    "last_observation": _aware(rows[-1].captured_at).isoformat() if rows else None,
                }
            )
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
    result.history_provenance = {
        "schema_version": "AST_HISTORY_PROVENANCE_V1",
        "as_of": at.isoformat(),
        "preference": "NATIVE_AST_PAPER_PER_DATE_NO_STREAM_MIXING",
        "current_session_native_only": True,
        "selected_sessions": provenance,
        "observation_counts": {
            mode: sum(p["observation_count"] for p in provenance if p["mode"] == mode)
            for mode in (CAPTURE_MODE, BOOTSTRAP_MODE)
        },
    }
    return result


def pending_ids(sessions: sessionmaker, after: datetime | None, limit: int = 200) -> list[int]:
    with sessions() as session:
        query = select(Snapshot.id).where(paper_clause())
        if after is not None:
            query = query.where(Snapshot.captured_at > after)
        return list(session.scalars(query.order_by(Snapshot.captured_at, Snapshot.id).limit(limit)))
