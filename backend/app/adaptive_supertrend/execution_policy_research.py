"""Streaming real-book AST construction research with shared daily optimization."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable, Iterator

from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.decision import decide
from app.adaptive_supertrend.execution_research_config import (
    MAX_CANDIDATES,
    MAX_EVALUATIONS,
    MAX_GRID_FITS,
    MAX_OBSERVATIONS,
    ExecutionSpec,
    SignalOnlyPolicy,
)
from app.adaptive_supertrend.execution_research_engine import ExecutionShadow
from app.adaptive_supertrend.optimizer import GRID, optimize
from app.adaptive_supertrend.research import load_bars
from app.parallel_paper.bars import Bar
from app.research.manifest import digest
from app.scalper.models import ScalperMarketSnapshot
from app.scalper.repository import IST


def lines(path: Path) -> Iterator[bytes]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as stream:
        yield from stream


def inventory(path: Path, count_candidates: int) -> dict:
    sha = hashlib.sha256()
    previous = None
    days: set[date] = set()
    count = quotes = 0
    for line in lines(path):
        sha.update(line)
        row = json.loads(line)
        at = datetime.fromisoformat(row["captured_at"])
        if at.tzinfo is None or previous is not None and at <= previous:
            raise ValueError("AST_RESEARCH_STRICT_AWARE_OBSERVATION_ORDER_REQUIRED")
        previous = at
        days.add(at.astimezone(IST).date())
        count += 1
        quotes += len(row["quotes"])
        if count > MAX_OBSERVATIONS or count * count_candidates > MAX_EVALUATIONS:
            raise ValueError("AST_EXECUTION_RESEARCH_COMPUTE_BUDGET_EXCEEDED")
    if not count:
        raise ValueError("AST_RAW_OPTION_BOOKS_REQUIRED")
    return {
        "uncompressed_jsonl_sha256": sha.hexdigest(),
        "observations": count,
        "option_quotes": quotes,
        "session_dates": sorted(days),
    }


def parameter_document(result: dict, policy: AstPolicy) -> dict | None:
    """Rebind metadata, not mathematics, exactly as a runtime freeze would do.

    Execution fields never enter optimize/rank. Each hypothetical combined policy
    nevertheless has its own optimizer-run hash, parameter document and opportunity IDs.
    """
    if result["selected"] is None:
        return None
    run = {**result, "policy_hash": policy.policy_hash, "policy": policy.model_dump(mode="json")}
    selected = run["selected"]
    return {
        **{
            key: run[key]
            for key in (
                "trading_date",
                "selected_at",
                "data_cutoff",
                "policy_hash",
                "training_data_identity",
                "walk_forward_status",
            )
        },
        "strategy_id": "AST_V1",
        "strategy_version": "AST_V1",
        "optimizer_run_id": digest(run),
        "atr_period": selected["atr_period"],
        "multiplier_tenths": selected["multiplier_tenths"],
        "metrics": selected["metrics"],
        "neighbour_robustness": selected["neighbour_robustness"],
    }


def precompute(
    bars: list[Bar],
    days: list[date],
    signal: AstPolicy,
    shadows: list[ExecutionShadow],
    progress: Callable[[str], None] | None,
) -> dict:
    assert signal.optimizer is not None
    by_day = {day: [bar for bar in bars if bar.start.date() == day] for day in sorted({b.start.date() for b in bars})}
    possible = [d for d in days if sum(p < d for p in by_day) >= signal.optimizer.lookback_sessions]
    if len(possible) > MAX_GRID_FITS:
        raise ValueError("AST_EXECUTION_RESEARCH_GRID_BUDGET_EXCEEDED")
    outputs = {}
    for index, day in enumerate(possible, 1):
        if progress:
            progress(f"grid {index}/{len(possible)} day={day} configurations=1886 shared_across={len(shadows)}")
        past = sorted(p for p in by_day if p < day)[-signal.optimizer.lookback_sessions:]
        result = optimize(
            [b for p in past for b in by_day[p]],
            day,
            datetime.combine(day, time(9), IST),
            signal,
            signal_research_only=True,
        )
        if len(result["ranking"]) != 1886 or {
            (r["atr_period"], r["multiplier_tenths"]) for r in result["ranking"]
        } != set(GRID):
            raise ValueError("AST_EXACT_1886_GRID_REQUIRED")
        outputs[day] = {
            "signal": parameter_document(result, signal),
            "combined": {s.name: parameter_document(result, s.policy) for s in shadows},
            "data_cutoff": result["data_cutoff"],
            "training_data_identity": result["training_data_identity"],
            "grid_ranking_hash": digest(result["ranking"]),
            "reason": None if result["selected"] else "AST_NO_ELIGIBLE_OPTIMIZER_CONFIGURATION",
        }
    return outputs


def _selection_key(record: dict) -> tuple:
    value = record["validation"]["entry_cohort_closed_metrics"]
    return (
        not record["eligible"],
        -value["consistency"],
        -(value["expectancy"] if value["expectancy"] is not None else float("-inf")),
        value["max_drawdown"],
        -value["total_trades"],
        record["candidate_id"],
    )


def run_research(
    bars: list[Bar],
    observations: Path,
    signal: AstPolicy,
    spec: ExecutionSpec,
    validation_sessions: int,
    oos_sessions: int,
    progress: Callable[[str], None] | None = None,
) -> dict:
    if (
        signal.optimizer is None
        or signal.regime is None
        or signal.regime.mode != "NONE"
        or signal.execution is not None
    ):
        raise ValueError("AST_FROZEN_NONE_SIGNAL_POLICY_WITHOUT_EXECUTION_REQUIRED")
    if not 1 <= validation_sessions <= 100 or not 1 <= oos_sessions <= 100:
        raise ValueError("AST_EXPLICIT_BOUNDED_VALIDATION_OOS_SESSIONS_REQUIRED")
    if not bars or any(a.end > b.start for a, b in zip(bars, bars[1:])):
        raise ValueError("AST_CHRONOLOGICAL_COMPLETED_BARS_REQUIRED")
    if progress:
        progress("inventory streaming plain/gzip raw option books")
    data = inventory(observations, len(spec.candidates))
    bar_days = sorted({b.start.date() for b in bars})
    eligible = [d for d in data["session_dates"] if sum(p < d for p in bar_days) >= signal.optimizer.lookback_sessions]
    folds: list[dict] = []
    for index in range(validation_sessions, len(eligible) - oos_sessions + 1, oos_sessions):
        folds.append(
            {
                "validation_dates": list(map(str, eligible[index - validation_sessions: index])),
                "oos_dates": list(map(str, eligible[index: index + oos_sessions])),
            }
        )
    if not folds:
        raise ValueError("AST_EXECUTION_RESEARCH_HISTORY_INSUFFICIENT_FOR_CONTIGUOUS_FOLDS")
    shadows = [ExecutionShadow(signal, spec, c) for c in sorted(spec.candidates, key=lambda c: c.name)]
    last_day = date.fromisoformat(folds[-1]["oos_dates"][-1])
    cache = precompute(bars, [d for d in data["session_dates"] if d <= last_day], signal, shadows, progress)
    signal_view = SignalOnlyPolicy.model_validate(signal.model_dump(mode="json"))
    by_day = {d: [b for b in bars if b.start.date() == d] for d in bar_days}
    contexts: dict[date, tuple[list[Bar], list[datetime]]] = {}
    for day in cache:
        prior = [d for d in bar_days if d < day][-signal.optimizer.lookback_sessions:]
        context = [b for d in [*prior, day] for b in by_day.get(d, [])]
        contexts[day] = context, [b.end for b in context]
    opportunities: dict[str, dict] = {}
    blockers: Counter = Counter()
    signal_state: dict = {}
    previous_day = None
    oos_dates = sorted({d for fold in folds for d in fold["oos_dates"]})
    evaluated = 0

    def boundaries(day: date) -> None:
        cutoff = datetime.combine(day, time(9, 15), IST)
        for i, fold in enumerate(folds, 1):
            if "oos_results" not in fold and "selection" in fold and fold["oos_dates"][-1] < str(day):
                fold["oos_results"] = [
                    {"candidate_id": s.candidate_id, "name": s.name, "summary": s.summary(fold["oos_dates"], cutoff)}
                    for s in shadows
                ]
            if fold["oos_dates"][0] != str(day) or "selection" in fold:
                continue
            ranking = []
            for shadow in shadows:
                value = shadow.summary(fold["validation_dates"], cutoff)
                ranking.append(
                    {
                        "candidate_id": shadow.candidate_id,
                        "name": shadow.name,
                        "validation": value,
                        "eligible": value["entry_cohort_closed_metrics"]["total_trades"] > 0
                        and value["active_exposure_at_cutoff"] == 0,
                    }
                )
            ranking.sort(key=_selection_key)
            fold["selection"] = {
                "as_of": cutoff.isoformat(),
                "uses_oos": False,
                "candidate_id": ranking[0]["candidate_id"] if ranking[0]["eligible"] else None,
                "validation_ranking": ranking,
            }
            if progress:
                progress(f"fold {i}/{len(folds)} validation-only selection before {day}")

    # Single quote-stream pass for all candidates. No per-candidate grid fits or
    # copies of the historical quote database; raw book memory is one observation.
    replay_sha = hashlib.sha256()
    for line_number, line in enumerate(lines(observations), 1):
        replay_sha.update(line)
        raw = ScalperMarketSnapshot.model_validate_json(line)
        at = raw.captured_at.astimezone(IST)
        raw = raw.model_copy(update={"captured_at": at})
        if previous_day != at.date():
            boundaries(at.date())
            previous_day = at.date()
            if progress:
                progress(f"capture day={at.date()} candidate_count={len(shadows)}")
        if at.date() not in cache or at.date() > last_day:
            continue
        evaluated += 1
        if progress and evaluated % 1000 == 0:
            progress(f"observations={evaluated} line={line_number}/{data['observations']}")
        context, ends = contexts[at.date()]
        identity = raw.future_instrument_id, raw.future_expiry
        causal = [
            (
                b
                if not b.valid or (getattr(b, "future_id", None), getattr(b, "future_expiry", None)) == identity
                else replace(b, valid=False, reason="AST_RESEARCH_FUTURES_IDENTITY_MISMATCH")
            )
            for b in context[: bisect_right(ends, at)]
        ]
        parameters = cache[at.date()]["signal"]
        decision = decide(
            causal,
            at,
            raw.nifty_future,
            parameters,
            signal_state,
            signal_view,
            future_identity=(raw.future_instrument_id, str(raw.future_expiry)),
        )
        if not raw.request_started_at <= raw.response_received_at <= at:
            decision["blockers"].append("AST_OBSERVATION_TIMING_INVALID")
            decision["status"] = "NO_SETUP"
        decision["features"].update(
            futures_source_timestamp=None,
            futures_source_timestamp_status="UNAVAILABLE_IN_SHARED_CAPTURE_SCHEMA",
            futures_observation_time=at.isoformat(),
            futures_observation_time_basis="BROKER_RESPONSE_CAPTURE_TIME",
            snapshot_request_started_at=raw.request_started_at.isoformat(),
            snapshot_response_received_at=raw.response_received_at.isoformat(),
            future_instrument_id=raw.future_instrument_id,
            future_expiry=str(raw.future_expiry),
        )
        blockers.update(decision["blockers"])
        if decision["status"] == "ENTER":
            key = decision["opportunity_id"]
            if key not in opportunities:
                opportunities[key] = {
                    "signal_opportunity_id": key,
                    "first_seen": at.isoformat(),
                    "direction": decision["direction"],
                    "features": decision["features"],
                    "last_seen": at.isoformat(),
                    "observations": 0,
                }
            opportunities[key]["last_seen"] = at.isoformat()
            opportunities[key]["observations"] += 1
        orb = signal_state.get("opening_range", {})
        for shadow in shadows:
            shadow.observe(raw, line_number, decision, cache[at.date()]["combined"][shadow.name], orb)
    if replay_sha.hexdigest() != data["uncompressed_jsonl_sha256"]:
        raise ValueError("AST_EXECUTION_RESEARCH_SOURCE_CHANGED_DURING_RUN")
    final_cutoff = datetime.combine(last_day + timedelta(days=1), time(9, 15), IST)
    boundaries(final_cutoff.date())
    for fold in folds:
        selected = fold["selection"]["candidate_id"]
        fold["selected_candidate_oos"] = next((r for r in fold["oos_results"] if r["candidate_id"] == selected), None)
    candidates = []
    evaluated_dates = [str(d) for d in eligible if d <= last_day]
    for shadow in shadows:
        candidates.append(
            {
                "name": shadow.name,
                "execution_candidate_id": shadow.candidate_id,
                "signal_policy_id": signal.policy_hash,
                "combined_hypothetical_policy_id": shadow.policy.policy_hash,
                "combined_policy": shadow.policy.model_dump(mode="json"),
                "all_research_sessions": shadow.summary(evaluated_dates, final_cutoff),
                "oos": shadow.summary(oos_dates, final_cutoff),
                "trades": [json.loads(r.document) for r in shadow.rows],
                "trade_states": {r.id: r.state for r in shadow.rows},
                "research_trace": {
                    "events_including_marks": shadow.trace_count,
                    "head": shadow.trace_head,
                    "non_mark_events": shadow.trace,
                    "runtime_journal": False,
                },
            }
        )
        if progress:
            progress(f"candidate={shadow.name} completed trades={len(shadow.rows)}")
    return {
        "schema_version": "AST_EXECUTION_POLICY_RESEARCH_V1",
        "purpose": "OFFLINE_ACTUAL_OPTION_BOOKS_ONLY",
        "signal_policy_id": signal.policy_hash,
        "signal_policy": signal.model_dump(mode="json"),
        "execution_spec": spec.model_dump(mode="json"),
        "dataset": {
            **data,
            "session_dates": list(map(str, data["session_dates"])),
            "observations_evaluated": evaluated,
            "oos_dates": oos_dates,
            "unused_tail_dates": [str(d) for d in eligible if d > last_day],
        },
        "daily_parameters": {str(day): value for day, value in sorted(cache.items())},
        "futures_signal_opportunities_before_execution": list(opportunities.values()),
        "signal_blocker_observations": dict(sorted(blockers.items())),
        "candidates": candidates,
        "folds": folds,
        "compute": {
            "grid_size": 1886,
            "grid_fits": len(cache),
            "candidates": len(shadows),
            "candidate_observations": evaluated * len(shadows),
            "maximum_candidates": MAX_CANDIDATES,
            "maximum_grid_fits": MAX_GRID_FITS,
            "maximum_candidate_observations": MAX_EVALUATIONS,
        },
        "methodology": {
            "signal_parameters": "ONCE_PER_SIGNAL_POLICY_DAY_REBIND_COMBINED_HASHES_WITHOUT_RERANKING",
            "signals": "EXISTING_DECIDE_ON_COMPLETED_CAUSAL_CSV_BARS_AND_CURRENT_CAPTURED_FUTURE",
            "execution": "EXISTING_BUILD_CANDIDATES_RISK_AND_TRANSITION_NEXT_OBSERVATION_EXACT_BOOKS",
            "controls": "EXPLICIT_IDENTICAL_RESEARCH_CONTROL_VALUES_NOT_OPTIMIZED",
            "availability": "FIRST_AND_EVER_EXECUTABLE_PER_SIGNAL_OPPORTUNITY_WITHOUT_PORTFOLIO_CONTROL_FILTERS",
            "availability_sampling": "STOP_STATIC_CHECKS_AFTER_SUCCESS_UNLESS_REAL_CONSTRUCTION_NEEDED",
            "reject_counts": "ENTRY_REASONS_PER_ATTEMPT_CONSTRUCTION_COUNTS_PER_LEG_PAIR_NOT_UNIQUE_TRADES",
            "selection": (
                "VALIDATION_CLOSED_ENTRY_COHORT_TRADES_AND_ZERO_ACTIVE_THEN_CONSISTENCY_EXPECTANCY_DD_COUNT_ID"
            ),
            "oos": "NONOVERLAPPING_BLOCKS_VALIDATION_MAY_OVERLAP_NO_OOS_SELECTION",
            "shadow_state": "CONTINUOUS_INDEPENDENT_CANDIDATES_UNRESOLVED_EXPOSURE_NEVER_RESET",
            "selected_oos": "PER_FOLD_SHADOW_COMPARISON_NOT_A_SWITCHED_POLICY_PORTFOLIO",
            "pnl": "CLOSED_REALIZED_RUPEES_UNPRICED_OPEN_OR_UNRESOLVED_EXCLUDED_NOT_ASSUMED_FLAT",
            "net": "ONLY_EXPLICIT_COMPLETE_DATED_SUPPORTED_COST_SCHEDULE_ELSE_NULL",
            "source_unchanged": "READ_ONLY_FILES_NO_DATABASE_OR_BROKER_CONNECTION",
        },
    }


def _checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("futures-csv", "snapshots", "signal-policy", "candidates", "json"):
        parser.add_argument(f"--{flag}", required=True, type=Path)
    parser.add_argument("--validation-sessions", required=True, type=int)
    parser.add_argument("--oos-sessions", required=True, type=int)
    args = parser.parse_args(argv)
    sources = [args.futures_csv, args.snapshots, args.signal_policy, args.candidates]
    paths = [*sources, args.json]
    if len({p.resolve() for p in paths}) != len(paths) or any(
        a.exists() and b.exists() and a.samefile(b) for i, a in enumerate(paths) for b in paths[i + 1:]
    ):
        parser.error("AST_EXECUTION_RESEARCH_DISTINCT_INPUT_OUTPUT_PATHS_REQUIRED")
    checksums = {str(path): _checksum(path) for path in sources}
    try:
        signal = AstPolicy.model_validate_json(args.signal_policy.read_text())
        spec = ExecutionSpec.model_validate_json(args.candidates.read_text())
        result = run_research(
            load_bars(args.futures_csv),
            args.snapshots,
            signal,
            spec,
            args.validation_sessions,
            args.oos_sessions,
            lambda message: print(message, file=sys.stderr, flush=True),
        )
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    if any(_checksum(path) != checksums[str(path)] for path in sources):
        parser.error("AST_EXECUTION_RESEARCH_SOURCE_CHANGED_DURING_RUN")
    result["source_file_sha256"] = checksums
    args.json.write_text(json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
