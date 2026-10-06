"""Read-only verification of a sealed replay run and its exact dataset.

No broker, database, feature recomputation, registration or ledger writes.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from uuid import UUID

from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_integrity_replay import configurations, load_dataset  # noqa: E402
from app.research.analytics import path_coverage  # noqa: E402
from app.research.costs import CostSchedule  # noqa: E402
from app.research.ledger import ResearchLedger  # noqa: E402
from app.research.manifest import FrozenManifest, implementation_hash, registered_rows  # noqa: E402
from app.research.quotes import executable_quote, information_time, validate_book  # noqa: E402


def audit_run(namespace, run_id, dataset_json, split="TRAIN"):
    UUID(run_id)
    if split not in {"TRAIN", "VALIDATION", "FINAL_TEST"}:
        raise ValueError("invalid split")
    root = Path(namespace)
    directory = root / run_id
    manifest = FrozenManifest.from_payload(json.loads((directory / "manifest.json").read_text(encoding="utf-8")))
    ledger = ResearchLedger(root, manifest, split=split)
    events = ledger.events()  # Verifies every hash, sequence and the final seal.
    summary = json.loads((directory / split / "summary.json").read_text(encoding="utf-8"))
    registered = manifest.payload()
    rows = registered_rows(manifest, load_dataset(dataset_json))
    sessions = set(registered["sessions"][split])
    rows = [row for row in rows if row.snapshot.timestamp_ist.date().isoformat() in sessions]
    integrity = configurations(registered["config"])[-1]
    schedule = TypeAdapter(CostSchedule).validate_python(registered["cost_schedule"])
    coverage = path_coverage(rows, sessions, integrity)
    if any(summary.get(key) != value for key, value in coverage.items()):
        raise ValueError("SUMMARY_COVERAGE_MISMATCH")
    decisions = [event["decision"] for event in events if event["event_type"] == "DECISION_ATTEMPT"]
    final = [event["outcome"] for event in events if event["event_type"] == "FINAL_OUTCOME"]
    if len(decisions) != summary["denominators"]["total_decisions"]:
        raise ValueError("SUMMARY_DECISION_COUNT_MISMATCH")
    books, execution, units = Counter(), Counter(), Counter()
    options = missing_bid = missing_ask = 0
    for row in rows:
        snapshot = row.snapshot
        for option in snapshot.options:
            options += 1
            missing_bid += option.bid is None
            missing_ask += option.ask is None
            units[option.depth_unit] += 1
            books[validate_book(option, snapshot.timestamp_ist, integrity.quote_policy,
                                evaluated_at=information_time(snapshot)).reason] += 1
            # A diagnostic size check, not an entry attempt or a policy bypass.
            for side in ("bid", "ask"):
                execution[executable_quote(snapshot, option, side,
                    registered["config"]["requested_lots"], integrity.quote_policy,
                    evaluated_at=information_time(snapshot)).reason] += 1
    return {"run_id": run_id, "ledger_verified": True, "dataset_verified": True,
            "current_source_matches_manifest": implementation_hash() == registered["working_tree_source_hash"],
            "recorded_code_sha": registered["git_commit_sha"],
            "event_counts": dict(Counter(event["event_type"] for event in events)),
            "decision_reasons": dict(Counter(item.get("missing_reason") or "NONE" for item in decisions)),
            "final_reasons": dict(Counter(item.get("missing_reason") or "NONE" for item in final)),
            "coverage": coverage, "denominators": summary["denominators"],
            "status": summary["status"], "profitability_claim": False,
            "alpha_rows": sum(row.alpha is not None for row in rows),
            "expiry_dates": sorted({row.snapshot.expiry.isoformat() for row in rows}),
            "option_rows": options, "missing_bid": missing_bid, "missing_ask": missing_ask,
            "depth_units": dict(units), "book_quality_counts": dict(books),
            "side_execution_quality_counts": dict(execution),
            "recorded_cost_schedule_complete": bool(rows) and all(
                schedule.complete_at(row.snapshot.timestamp_ist.date()) for row in rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dataset-json", required=True)
    parser.add_argument("--split", choices=("TRAIN", "VALIDATION", "FINAL_TEST"), default="TRAIN")
    args = parser.parse_args(argv)
    try:
        result = audit_run(args.namespace, args.run_id, args.dataset_json, args.split)
    except Exception as exc:
        print(f"Audit failed ({type(exc).__name__}); no files were changed.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
