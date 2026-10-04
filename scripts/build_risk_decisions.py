"""Build hard deterministic risk decisions from persisted Phase 6 candidates."""

import argparse
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.risk.event_checks import ConfiguredMarketEventProvider  # noqa: E402
from app.risk.models import EvaluationContext  # noqa: E402
from app.risk.repository import RiskRepository  # noqa: E402
from app.risk.service import (  # noqa: E402
    backfill_risk,
    build_and_store_risk,
    config_from_settings,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    group = value.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true")
    group.add_argument("--snapshot-id", type=int)
    group.add_argument("--all", action="store_true")
    return value


def print_summary(result) -> None:
    print("RISK_EVALUATION_BUILT")
    print(f"snapshot_id={result.snapshot_id}")
    print(f"candidate_count={result.candidate_count}")
    print(f"approved={result.approved_count}")
    print(f"rejected={result.rejected_count}")
    print(f"best_approved_candidate={result.best_approved_candidate}")
    if result.not_applicable:
        print("decision=NOT_APPLICABLE")
        print("reason=NO_STRATEGY_CANDIDATE")


def main() -> int:
    args = parser().parse_args()
    settings = get_settings()
    if not settings.database_url:
        print("Configuration error: DATABASE_URL is required", file=sys.stderr)
        return 2
    repository = RiskRepository(
        build_session_factory(build_engine(settings.database_url.get_secret_value()))
    )
    try:
        config = config_from_settings(settings)
        events = ConfiguredMarketEventProvider.from_json(settings.risk_market_events_json)
        if args.all:
            results = backfill_risk(repository, config, event_provider=events)
            print(f"RISK_BACKFILL_COMPLETE snapshots={len(results)}")
            return 0
        snapshot_id = args.snapshot_id
        context = EvaluationContext.HISTORICAL
        if args.latest:
            snapshot_id = repository.latest_context_snapshot_id()
            if snapshot_id is None:
                raise LookupError("no phase6_v1 candidate set found")
            context = EvaluationContext.LIVE
        result = build_and_store_risk(
            repository, snapshot_id, config, context, event_provider=events
        )
    except (LookupError, ValueError) as exc:
        print(f"Risk evaluation failed: {exc}", file=sys.stderr)
        return 1
    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
