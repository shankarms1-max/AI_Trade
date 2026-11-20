"""Daily, chronological optimizer-policy experiments; no execution or runtime defaults."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
from itertools import product
import json
from pathlib import Path
import sys
from typing import Callable

from app.adaptive_supertrend.config import AstPolicy, NoRegimePolicy, OptimizerPolicy, Weights
from app.adaptive_supertrend.indicators import supertrend
from app.adaptive_supertrend.optimizer import GRID, _segments, metrics, optimize, rank, signal_trades
from app.adaptive_supertrend.research import SessionGap, load_bars
from app.parallel_paper.bars import Bar
from app.research.manifest import digest
from app.scalper.repository import IST

BENCHMARKS = ((7, 11), (7, 12), (26, 12))
MAX_CANDIDATES = 256
MAX_GRID_FITS = 512


@dataclass(frozen=True)
class Candidate:
    weight_profile: str
    optimizer: OptimizerPolicy

    @property
    def policy(self) -> AstPolicy:
        return AstPolicy(optimizer=self.optimizer, regime=NoRegimePolicy(mode="NONE"))

    @property
    def key(self) -> str:
        return digest({"weight_profile": self.weight_profile, "optimizer": self.optimizer.model_dump(mode="json")})

    @property
    def base_key(self) -> tuple:
        cfg = self.optimizer
        return (
            cfg.lookback_sessions,
            cfg.subperiods,
            cfg.minimum_trades,
            self.weight_profile,
            digest(cfg.weights.model_dump()),
        )

    @property
    def layout(self) -> tuple[int, int]:
        return self.optimizer.validation_sessions, self.optimizer.out_of_sample_sessions

    def payload(self) -> dict:
        return {
            "candidate_id": self.key,
            "weight_profile": self.weight_profile,
            "policy_hash": self.policy.policy_hash,
            "signal_policy": self.policy.model_dump(mode="json"),
        }


def candidates(
    lookbacks: tuple[int, ...],
    subperiods: tuple[int, ...],
    minimum_trades: tuple[int, ...],
    profiles: dict[str, Weights],
    validation: tuple[int, ...],
    oos: tuple[int, ...],
) -> list[Candidate]:
    if not profiles or any(not name.strip() for name in profiles):
        raise ValueError("AST_EXPLICIT_NAMED_WEIGHT_PROFILES_REQUIRED")
    axes = (lookbacks, subperiods, minimum_trades, tuple(sorted(profiles)), validation, oos)
    size = 1
    for axis in axes:
        if not axis or len(set(axis)) != len(axis):
            raise ValueError("AST_NONEMPTY_UNIQUE_CANDIDATE_AXES_REQUIRED")
        size *= len(axis)
    if size > MAX_CANDIDATES:
        raise ValueError("AST_RESEARCH_CANDIDATE_BUDGET_EXCEEDED")
    return [
        Candidate(
            profile,
            OptimizerPolicy(
                lookback_sessions=lookback,
                subperiods=subperiod,
                minimum_trades=minimum,
                weights=profiles[profile],
                validation_sessions=val,
                out_of_sample_sessions=out,
            ),
        )
        for lookback, subperiod, minimum, profile, val, out in product(*axes)
    ]


def _measure(trades: list[dict], days: list[str]) -> dict:
    # One subperiod per session, including no-trade sessions; never count missing
    # selections as a valid flat strategy. Coverage is reported separately.
    return metrics(trades, len(days), max(1, len(days)), days)


def _summary(rows: list[dict]) -> dict:
    days = [row["date"] for row in rows]
    valid = [row for row in rows if row["selected"] is not None]
    pairs = [(a["selected"], b["selected"]) for a, b in zip(rows, rows[1:]) if a["selected"] and b["selected"]]
    changes = sum(a != b for a, b in pairs)
    fast = sum(5 <= r["selected"]["atr_period"] <= 7 and 10 <= r["selected"]["multiplier_tenths"] <= 13 for r in valid)
    slow = sum(
        20 <= r["selected"]["atr_period"] <= 26 and 11 <= r["selected"]["multiplier_tenths"] <= 13 for r in valid
    )
    return {
        "planned_oos_sessions": len(rows),
        "valid_selected_oos_sessions": len(valid),
        "complete_selection_coverage": len(valid) == len(rows),
        "closed_signal_metrics": _measure([trade for row in rows for trade in row["trades"]], days),
        "parameter_changes": changes,
        "comparable_adjacent_session_pairs": len(pairs),
        "turnover": changes / len(pairs) if pairs else None,
        "stability": 1 - changes / len(pairs) if pairs else None,
        "distinct_selected_parameters": sorted(
            {(r["selected"]["atr_period"], r["selected"]["multiplier_tenths"]) for r in valid}
        ),
        "fast_region_sessions": fast,
        "fast_region_fraction": fast / len(valid) if valid else None,
        "slow_region_sessions": slow,
        "slow_region_fraction": slow / len(valid) if valid else None,
    }


def _selection_key(record: dict) -> tuple:
    value = record["validation_metrics"]
    # Research-only meta selection, fixed before OOS and explicitly documented.
    # This does not change the runtime grid ranking or supply a runtime policy.
    return (
        not record["eligible"],
        -value["consistency"],
        -(value["expectancy"] if value["expectancy"] is not None else float("-inf")),
        value["max_drawdown"],
        -value["total_trades"],
        record["candidate_id"],
    )


def run_research(bars: list[Bar], choices: list[Candidate], progress: Callable[[str], None] | None = None) -> dict:
    if not choices or len(choices) > MAX_CANDIDATES or len({c.key for c in choices}) != len(choices):
        raise ValueError("AST_UNIQUE_BOUNDED_CANDIDATES_REQUIRED")
    # Public Python callers must use the same completed causal CSV loader contract.
    if any(b.start.tzinfo is None or b.end.tzinfo is None or b.end <= b.start for b in bars):
        raise ValueError("AST_COMPLETED_AWARE_BARS_REQUIRED")
    if any(a.end > b.start for a, b in zip(bars, bars[1:])):
        raise ValueError("AST_RESEARCH_ORDER_REQUIRED")
    days = sorted({b.start.astimezone(IST).date() for b in bars})
    by_day = {day: [b for b in bars if b.start.astimezone(IST).date() == day] for day in days}
    max_lookback = max(c.optimizer.lookback_sessions for c in choices)
    layouts: dict[tuple[int, int], list[dict]] = {}
    needed: set[date] = set()
    for layout in sorted({c.layout for c in choices}):
        val, out = layout
        folds = []
        for cursor in range(max_lookback + val, len(days) - out + 1, out):
            validation_days, test_days = days[cursor - val: cursor], days[cursor: cursor + out]
            folds.append({"validation_dates": list(map(str, validation_days)), "oos_dates": list(map(str, test_days))})
            needed.update(validation_days + test_days)
        layouts[layout] = folds
    if not needed:
        raise ValueError("AST_OPTIMIZER_POLICY_RESEARCH_HISTORY_INSUFFICIENT")
    bases = {c.base_key: c for c in choices}
    groups: dict[tuple[int, int], list[Candidate]] = {}
    for candidate in bases.values():
        cfg = candidate.optimizer
        groups.setdefault((cfg.lookback_sessions, cfg.subperiods), []).append(candidate)
    fit_count = len(needed) * len(groups)
    if fit_count > MAX_GRID_FITS:
        raise ValueError("AST_RESEARCH_GRID_FIT_BUDGET_EXCEEDED")
    daily: dict[tuple, dict[str, dict]] = {key: {} for key in bases}
    benchmark_days: dict[tuple, dict[str, list[dict]]] = {}
    fitted = 0
    for day in sorted(needed):
        index = days.index(day)
        # Only this date's trades are scored. Warm-up starts at the same rolling
        # training prefix as optimize(), rather than an unrelated full-data seed.
        trade_cache: dict[tuple, list[dict]] = {}

        def trades_for(lookback: int, parameters: tuple[int, int]) -> list[dict]:
            cache_key = lookback, parameters
            if cache_key not in trade_cache:
                context = [b for d in days[index - lookback: index + 1] for b in by_day[d]]
                trades: list[dict] = []
                for segment in _segments(context):
                    sequence = supertrend(segment, *parameters)
                    trades.extend(
                        t for t in signal_trades(segment, sequence, [True] * len(segment)) if t["date"] == str(day)
                    )
                trade_cache[cache_key] = sorted(trades, key=lambda t: (t["entry_at"], t["exit_at"]))
            return trade_cache[cache_key]

        for (lookback, subperiod), group in sorted(groups.items()):
            fitted += 1
            if progress:
                progress(f"grid {fitted}/{fit_count} day={day} lookback={lookback} subperiods={subperiod} grid=1886")
            training = [b for d in days[index - lookback: index] for b in by_day[d]]
            result = optimize(
                training,
                day,
                datetime.combine(day, time(9), IST),
                group[0].policy,
                signal_research_only=True,
            )
            if {(r["atr_period"], r["multiplier_tenths"]) for r in result["ranking"]} != set(GRID):
                raise ValueError("AST_EXACT_1886_GRID_REQUIRED")
            # Minimum trades/weights only affect rank(), not the grid's raw
            # signal metrics. Re-rank fresh rows; never reuse another selection.
            raw = [{k: row[k] for k in ("atr_period", "multiplier_tenths", "metrics")} for row in result["ranking"]]
            for candidate in sorted(group, key=lambda c: c.key):
                if progress:
                    progress(f"candidate={candidate.key[:12]} day={day} rerank")
                rows = rank([dict(row) for row in raw], candidate.policy)
                selected = (
                    {k: rows[0][k] for k in ("atr_period", "multiplier_tenths")} if rows[0]["eligible"] else None
                )
                trades = (
                    trades_for(lookback, (selected["atr_period"], selected["multiplier_tenths"])) if selected else []
                )
                daily[candidate.base_key][str(day)] = {
                    "date": str(day),
                    "selected": selected,
                    "reason": None if selected else "AST_NO_ELIGIBLE_GRID_CONFIGURATION",
                    "selection_clock": result["selected_at"],
                    "selection_clock_basis": "HISTORICAL_PRESESSION_SIMULATION_NOT_EXCHANGE_TIMESTAMP",
                    "training_dates": list(map(str, days[index - lookback: index])),
                    "training_data_identity": result["training_data_identity"],
                    "data_cutoff": result["data_cutoff"],
                    "trades": trades,
                }
            benchmark_days[(lookback, str(day))] = {
                f"{p}/{m / 10:.1f}": trades_for(lookback, (p, m)) for p, m in BENCHMARKS
            }
    outputs = []
    layout_outputs = []
    for layout, folds in sorted(layouts.items()):
        group = sorted((c for c in choices if c.layout == layout), key=lambda c: c.key)
        oos_dates = [day for fold in folds for day in fold["oos_dates"]]
        selected_path = []
        fold_outputs = []
        for i, fold in enumerate(folds, 1):
            if progress:
                progress(f"fold={i}/{len(folds)} validation={layout[0]} oos={layout[1]}")
            scores = []
            for candidate in group:
                validation_rows = [daily[candidate.base_key][day] for day in fold["validation_dates"]]
                value = _measure([t for row in validation_rows for t in row["trades"]], fold["validation_dates"])
                valid = sum(r["selected"] is not None for r in validation_rows)
                scores.append(
                    {
                        "candidate_id": candidate.key,
                        "valid_validation_selections": valid,
                        "eligible": valid == len(validation_rows) and value["total_trades"] > 0,
                        "validation_metrics": value,
                    }
                )
            scores.sort(key=_selection_key)
            selected_id = scores[0]["candidate_id"] if scores[0]["eligible"] else None
            selected_candidate = next((c for c in group if c.key == selected_id), None)
            fold_outputs.append({**fold, "validation_ranking": scores, "selected_candidate_id": selected_id})
            for oos_day in fold["oos_dates"]:
                if selected_candidate:
                    selected_path.append(
                        {
                            **daily[selected_candidate.base_key][oos_day],
                            "candidate_id": selected_id,
                            "policy_hash": selected_candidate.policy.policy_hash,
                        }
                    )
                else:
                    selected_path.append(
                        {
                            "date": oos_day,
                            "selected": None,
                            "trades": [],
                            "candidate_id": None,
                            "reason": "NO_VALIDATION_ELIGIBLE_POLICY",
                        }
                    )
        layout_outputs.append(
            {
                "validation_sessions": layout[0],
                "out_of_sample_sessions": layout[1],
                "status": "EVALUATED" if folds else "NO_COMPLETE_FOLDS",
                "folds": fold_outputs,
                "selected_policy_daily_oos": selected_path,
                "selected_policy_summary": _summary(selected_path),
                "uses_oos_for_policy_selection": False,
                "unused_tail_dates": [str(d) for d in days if oos_dates and str(d) > oos_dates[-1]],
            }
        )
        for candidate in group:
            rows = [{**daily[candidate.base_key][d], "policy_hash": candidate.policy.policy_hash} for d in oos_dates]
            benchmarks = []
            matched_dates = [r["date"] for r in rows if r["selected"]]
            for p, m in BENCHMARKS:
                name = f"{p}/{m / 10:.1f}"
                all_trades = [
                    t for d in oos_dates for t in benchmark_days[(candidate.optimizer.lookback_sessions, d)][name]
                ]
                matched = [t for t in all_trades if t["date"] in matched_dates]
                benchmarks.append(
                    {
                        "label": name,
                        "atr_period": p,
                        "multiplier_tenths": m,
                        "role": "STATIC_RESEARCH_BENCHMARK_ONLY_NEVER_FALLBACK",
                        "all_oos": _measure(all_trades, oos_dates),
                        "matched_selected_dates": _measure(matched, matched_dates),
                        "optimizer_minus_matched_benchmark_points": sum(
                            t["signal_points"] for r in rows for t in r["trades"]
                        )
                        - sum(t["signal_points"] for t in matched),
                    }
                )
            outputs.append(
                {**candidate.payload(), "daily_oos": rows, "summary": _summary(rows), "benchmarks": benchmarks}
            )
    return {
        "schema_version": "AST_OPTIMIZER_POLICY_RESEARCH_V1",
        "purpose": "FUTURES_SIGNAL_RESEARCH_ONLY_NO_RUNTIME_POLICY",
        "regime": {"mode": "NONE"},
        "dataset": {
            "session_dates": list(map(str, days)),
            "valid_completed_bars": sum(b.valid for b in bars),
            "session_gap_boundaries": sum(isinstance(b, SessionGap) for b in bars),
        },
        "candidates": outputs,
        "walk_forward_layouts": layout_outputs,
        "computation": {
            "grid_size": len(GRID),
            "grid_fits": fitted,
            "candidate_count": len(choices),
            "maximum_candidates": MAX_CANDIDATES,
            "maximum_grid_fits": MAX_GRID_FITS,
        },
        "methodology": {
            "daily_selection": "EXACT_PRIOR_LOOKBACK_SESSIONS_OPTIMIZE_THEN_FREEZE_FOR_ONE_SESSION",
            "meta_selection": (
                "FULL_VALIDATION_SELECTION_COVERAGE_AND_CLOSED_TRADE_THEN_CONSISTENCY_EXPECTANCY_DD_COUNT_ID"
            ),
            "validation_consistency": "FRACTION_OF_SESSIONS_WITH_POSITIVE_CLOSED_SIGNAL_POINTS",
            "layout_comparison": "DESCRIPTIVE_ONLY_NO_OOS_BASED_LAYOUT_WINNER_OR_RUNTIME_DEFAULT",
            "history_reuse": "PAST_OOS_MAY_ENTER_LATER_TRAINING_ONLY_AFTER_SESSION_HAS_ENDED",
            "indicator_context": "SAME_ROLLING_TRAINING_PREFIX_AS_OPTIMIZER_PLUS_CURRENT_SESSION",
            "grid_cache": "METRICS_SHARED_ONLY_FOR_SAME_DATE_LOOKBACK_SUBPERIODS_RERANK_EACH_WEIGHT_AND_MINIMUM",
            "unpriced_positions": "EXCLUDED_BY_EXISTING_SIGNAL_TRADES_NO_SYNTHETIC_SESSION_CLOSE",
            "missing_selection": "NO_STATIC_FALLBACK_COVERAGE_REPORTED_SEPARATELY_FROM_CLOSED_TRADE_METRICS",
            "option_pnl": "UNAVAILABLE_NO_OPTION_FILLS_OR_COSTS_INFERRED",
        },
    }


def write_csv(result: dict, path: Path) -> None:
    fields = [
        "candidate_id",
        "weight_profile",
        "policy_hash",
        "date",
        "atr_period",
        "multiplier_tenths",
        "reason",
        "total_trades",
        "gross_signal_points",
        "expectancy",
        "profit_factor",
        "max_drawdown",
        "sharpe_equivalent",
        "win_rate",
        "consistency",
    ]
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for candidate in result["candidates"]:
            for day in candidate["daily_oos"]:
                value = _measure(day["trades"], [day["date"]])
                writer.writerow(
                    {
                        **{k: candidate[k] for k in fields[:3]},
                        "date": day["date"],
                        "reason": day["reason"],
                        **(day["selected"] or {}),
                        **{k: value[k] for k in fields[7:]},
                    }
                )


def _integers(value: str) -> tuple[int, ...]:
    try:
        rows = tuple(int(item) for item in value.split(","))
        if not rows or len(rows) != len(set(rows)) or any(item < 1 for item in rows):
            raise ValueError()
        return rows
    except ValueError as exc:
        raise argparse.ArgumentTypeError("positive unique comma-separated integers required") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--weight-profiles", type=Path, required=True)
    for name in ("lookback-sessions", "subperiods", "minimum-trades", "validation-sessions", "oos-sessions"):
        parser.add_argument(f"--{name}", type=_integers, required=True)
    args = parser.parse_args(argv)
    inputs, outputs = [args.csv, args.weight_profiles], [p for p in (args.json, args.csv_output) if p]
    paths = inputs + outputs
    if len({p.resolve() for p in paths}) != len(paths) or any(
        a.exists() and b.exists() and a.samefile(b) for i, a in enumerate(paths) for b in paths[i + 1:]
    ):
        parser.error("AST_RESEARCH_DISTINCT_INPUT_AND_OUTPUT_PATHS_REQUIRED")
    checksums = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    try:
        profiles = {
            name: Weights.model_validate(value) for name, value in json.loads(args.weight_profiles.read_text()).items()
        }
        choices = candidates(
            args.lookback_sessions,
            args.subperiods,
            args.minimum_trades,
            profiles,
            args.validation_sessions,
            args.oos_sessions,
        )
        result = run_research(
            load_bars(args.csv), choices, lambda message: print(message, file=sys.stderr, flush=True)
        )
    except (ValueError, AttributeError) as exc:
        parser.error(str(exc))
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != checksums[str(p)] for p in inputs):
        parser.error("AST_RESEARCH_INPUT_CHANGED_DURING_RUN")
    result["source_checksums"] = checksums
    args.json.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    if args.csv_output:
        write_csv(result, args.csv_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
