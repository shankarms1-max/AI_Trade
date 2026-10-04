"""Offline market-session audit for Phase 14.1; never calls a broker."""
import argparse
from datetime import date
import json
from pathlib import Path
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.alpha.engine import config_from_settings  # noqa: E402
from app.alpha.models import ALPHA_VERSION, AlphaFeatureSnapshot  # noqa: E402
from app.alpha.validation import prefix_replay_equivalent, validate_session  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.models import AlphaFeatureSnapshotRecord, MarketSnapshotRecord  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.features.repository import raw_record_to_model  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="NSE session date YYYY-MM-DD")
    parser.add_argument("--prefix-snapshot-id", type=int)
    args = parser.parse_args()
    day = date.fromisoformat(args.date)
    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required")
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    with sessions() as session:
        records = session.scalars(select(MarketSnapshotRecord).options(
            selectinload(MarketSnapshotRecord.options)).order_by(
                MarketSnapshotRecord.timestamp_ist, MarketSnapshotRecord.id)).all()
        all_snapshots = [raw_record_to_model(item) for item in records]
        session_rows = [(record.id, raw_record_to_model(record)) for record in records
                        if record.timestamp_ist.date() == day]
        alpha_records = session.scalars(select(AlphaFeatureSnapshotRecord).where(
            AlphaFeatureSnapshotRecord.alpha_version == ALPHA_VERSION,
            func.date(AlphaFeatureSnapshotRecord.timestamp) == day,
            AlphaFeatureSnapshotRecord.calculation_mode != "RESEARCH_RECOMPUTE"
        ).order_by(AlphaFeatureSnapshotRecord.timestamp)).all()
        alphas = [AlphaFeatureSnapshot.model_validate(item.result_json) for item in alpha_records]
    report = validate_session([snapshot for _, snapshot in session_rows], alphas,
                              settings.collector_interval_minutes * 60,
                              settings.collector_start_time, settings.collector_end_time)
    if args.prefix_snapshot_id is not None:
        chosen = next((snapshot for identifier, snapshot in session_rows
                       if identifier == args.prefix_snapshot_id), None)
        if chosen is None:
            raise SystemExit("prefix snapshot ID is not in the selected session")
        report["prefix_replay"] = {
            "snapshot_id": args.prefix_snapshot_id,
            "equivalent": prefix_replay_equivalent(args.prefix_snapshot_id, chosen,
                                                   all_snapshots, config_from_settings(settings)),
        }
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
