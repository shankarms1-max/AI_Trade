"""Fetch and atomically persist one real, read-only NIFTY snapshot."""

from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.collector.factory import make_live_snapshot_builder  # noqa: E402
from app.collector.service import CollectorService, TradingCalendar  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.db.repositories import SnapshotRepository  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.pipeline.service import make_after_snapshot_callback  # noqa: E402
from app.paper.continuity import required_paper_contracts  # noqa: E402
from pydantic import ValidationError  # noqa: E402


def main() -> int:
    try:
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is required")
    except (ValidationError, RuntimeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level)
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    repository = SnapshotRepository(sessions)
    service = CollectorService(
        make_live_snapshot_builder(
            settings,
            (lambda: required_paper_contracts(sessions)) if settings.forward_paper_enabled else None,
        ),
        repository,
        TradingCalendar(
            settings.collector_start_time,
            settings.collector_end_time,
            settings.configured_holidays,
        ),
        settings.collector_interval_minutes,
        make_after_snapshot_callback(sessions, settings)
        if settings.pipeline_after_snapshot else None,
    )
    try:
        result = service.collect_once()
        snapshot = repository.get(result.snapshot_id) if result else None
    except Exception as exc:
        print(f"Snapshot persistence failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    if result is None or snapshot is None:
        print("Snapshot persistence failed.", file=sys.stderr)
        return 1
    print("SNAPSHOT_STORED")
    print(f"snapshot_id={result.snapshot_id}")
    print(f"timestamp={snapshot['timestamp_ist'].isoformat()}")
    print(f"spot={snapshot['nifty_spot']}")
    print(f"expiry={snapshot['expiry'].isoformat()}")
    print(f"contracts={result.contracts}")
    print(f"duplicate={str(result.duplicate).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
