#!/usr/bin/env python3
"""Read-only before/after audit of ACTS Alpha history on the Phase 15 database."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.acts_paper.alpha import ACTS_ALPHA_SOURCE, ActsAlphaRepository  # noqa: E402
from app.acts_paper.alpha_history_diagnostic import diagnose_alpha_history  # noqa: E402
from app.alpha.engine import build_alpha_features, config_from_settings  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.models import MarketSnapshotRecord  # noqa: E402
from app.db.session import build_session_factory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="Overrides DATABASE_URL; never printed")
    parser.add_argument("--snapshot-id", type=int, help="Local Alpha raw ID; defaults to latest")
    args = parser.parse_args()
    settings = get_settings()
    url = (args.database_url or os.getenv("DATABASE_URL") or
           (settings.database_url.get_secret_value() if settings.database_url else None))
    if not url:
        parser.error("DATABASE_URL or --database-url is required")
    dialect = make_url(url).get_backend_name()
    if dialect != "postgresql":
        parser.error("this diagnostic requires a PostgreSQL database")
    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={"options": "-c default_transaction_read_only=on"})
    sessions = build_session_factory(engine)
    config = config_from_settings(settings)
    repository = ActsAlphaRepository(sessions, config)
    with sessions() as session:
        query = select(MarketSnapshotRecord.id).where(
            MarketSnapshotRecord.source == ACTS_ALPHA_SOURCE)
        if args.snapshot_id is not None:
            query = query.where(MarketSnapshotRecord.id == args.snapshot_id)
        else:
            query = query.order_by(MarketSnapshotRecord.timestamp_ist.desc(),
                                   MarketSnapshotRecord.id.desc()).limit(1)
        snapshot_id = session.scalar(query)
    if snapshot_id is None:
        parser.error("no matching ACTS local Alpha raw observation")
    current = repository.load_raw(snapshot_id)
    if current is None:
        parser.error("ACTS local Alpha raw observation could not be loaded")
    history = repository.load_history_before(current.timestamp_ist)
    report = dict(repository.last_history_report)
    option_times = repository.last_history_option_aggregate_times
    old_history = [point.model_copy(update={
        "source_market_timestamp": option_times[point.timestamp_ist]})
        if point.timestamp_ist in option_times else point for point in history]
    before = diagnose_alpha_history(current, old_history, config)
    after = diagnose_alpha_history(current, history, config)
    # Original calculation is called in memory only; no Alpha row is written.
    old_alpha = build_alpha_features(snapshot_id, current, old_history, [], config)
    new_alpha = build_alpha_features(snapshot_id, current, history, [], config)
    print(json.dumps({
        "snapshot_id": snapshot_id, "history": report,
        "option_aggregate_timestamps_misused_before": len(option_times),
        "before": before, "after": after,
        "calculated_before": {
            "status": old_alpha.status.value, "validity": old_alpha.validity_state.value,
            "rank_observations_alpha1": old_alpha.rank_observations_alpha1,
            "rank_observations_alpha2": old_alpha.rank_observations_alpha2,
            "warnings": old_alpha.warnings,
        },
        "calculated_after": {
            "status": new_alpha.status.value, "validity": new_alpha.validity_state.value,
            "rank_observations_alpha1": new_alpha.rank_observations_alpha1,
            "rank_observations_alpha2": new_alpha.rank_observations_alpha2,
            "warnings": new_alpha.warnings,
        },
    }, sort_keys=True, indent=2))
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
