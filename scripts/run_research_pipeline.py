"""Run the persisted research pipeline without broker calls."""

import argparse
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.pipeline.models import PipelineStatus  # noqa: E402
from app.pipeline.repository import PipelineRepository  # noqa: E402
from app.pipeline.service import build_pipeline_orchestrator  # noqa: E402


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    group = value.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true")
    group.add_argument("--snapshot-id", type=int)
    group.add_argument("--all", action="store_true")
    value.add_argument("--with-ai", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    repository = PipelineRepository(sessions)
    if args.all:
        snapshot_ids = repository.raw_ids_chronological()
    elif args.latest:
        latest = repository.latest_raw_id()
        snapshot_ids = [] if latest is None else [latest]
    else:
        snapshot_ids = [args.snapshot_id]
    if not snapshot_ids:
        print("Pipeline failed: no persisted snapshots found", file=sys.stderr)
        return 1

    run_ai = bool(args.with_ai)
    orchestrator = build_pipeline_orchestrator(sessions, settings, enable_ai=run_ai)
    failed = False
    for snapshot_id in snapshot_ids:
        result = orchestrator.run(snapshot_id, run_ai=run_ai)
        print("PIPELINE_RUN")
        print(f"snapshot_id={snapshot_id}")
        print(f"status={result.status.value}")
        print(f"approved_candidates={result.approved_candidate_count}")
        print(f"shadow_trade_id={result.shadow_trade_id}")
        failed |= result.status in {PipelineStatus.FAILED, PipelineStatus.PARTIAL}
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
