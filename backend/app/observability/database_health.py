from time import perf_counter
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import MarketSnapshotRecord, PipelineRunRecord
from app.observability.models import HealthStatus, safe_exception


def check_database(session_factory: sessionmaker[Session]) -> dict[str, Any]:
    started = perf_counter()
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1")).scalar_one()
            session.scalar(select(MarketSnapshotRecord.id).order_by(MarketSnapshotRecord.id.desc()).limit(1))
            session.scalar(select(PipelineRunRecord.id).order_by(PipelineRunRecord.id.desc()).limit(1))
        return {"status": HealthStatus.HEALTHY.value,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "safe_error_type": None, "safe_error_message": None}
    except Exception as exc:
        error_type, message = safe_exception(exc)
        return {"status": HealthStatus.UNHEALTHY.value,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "safe_error_type": error_type, "safe_error_message": message}
