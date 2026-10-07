#!/usr/bin/env python3
"""Run deterministic, read-only Phase 15 replay and P&L research."""
from __future__ import annotations

import argparse
from datetime import date
from itertools import product
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.research.manifest import implementation_hash  # noqa: E402
from app.scalper.config import ScalperConfig  # noqa: E402
from app.scalper.paper import cost_schedule  # noqa: E402
from app.scalper.replay import (  # noqa: E402
    ScalperReplayEngine,
    load_replay_rows,
    write_replay_outputs,
)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _float_grid(value: str) -> tuple[float, ...]:
    try:
        result = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated numbers") from exc
    if not result:
        raise argparse.ArgumentTypeError("grid cannot be empty")
    return result


def _int_grid(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from exc
    if not result:
        raise argparse.ArgumentTypeError("grid cannot be empty")
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--start", type=_date)
    value.add_argument("--end", type=_date)
    value.add_argument("--research-start", type=_date)
    value.add_argument("--research-end", type=_date)
    value.add_argument("--validation-start", type=_date)
    value.add_argument("--validation-end", type=_date)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument(
        "--database-url",
        help="Read-only source database URL; defaults to DATABASE_URL",
    )
    value.add_argument("--json", action="store_true", help="Print result JSON")

    value.add_argument("--signal-min-score", type=float)
    value.add_argument("--min-confirmations", type=int)
    value.add_argument("--allowed-widths")
    value.add_argument("--profit-capture-pct", type=float)
    value.add_argument("--stop-credit-multiple", type=float)
    value.add_argument("--trailing-activation-pct", type=float)
    value.add_argument("--trailing-giveback-pct", type=float)
    value.add_argument("--time-stop-minutes", type=int)
    value.add_argument("--score-grid", type=_float_grid)
    value.add_argument("--confirmation-grid", type=_int_grid)
    return value


def _periods(args: argparse.Namespace) -> list[tuple[str, date, date]]:
    direct = args.start is not None or args.end is not None
    partitioned = any(value is not None for value in (
        args.research_start, args.research_end,
        args.validation_start, args.validation_end,
    ))
    if direct and partitioned:
        raise ValueError("direct and partitioned date arguments are mutually exclusive")
    if direct:
        if args.start is None or args.end is None:
            raise ValueError("--start and --end are required together")
        return [("UNPARTITIONED", args.start, args.end)]
    if not partitioned:
        raise ValueError("a direct or research/validation date range is required")
    if (args.research_start is None) != (args.research_end is None):
        raise ValueError("--research-start and --research-end are required together")
    if (args.validation_start is None) != (args.validation_end is None):
        raise ValueError("--validation-start and --validation-end are required together")
    periods = []
    if args.research_start is not None:
        periods.append(("IN_SAMPLE", args.research_start, args.research_end))
    if args.validation_start is not None:
        periods.append(("OUT_OF_SAMPLE", args.validation_start, args.validation_end))
    if len(periods) == 2 and periods[0][2] >= periods[1][1]:
        raise ValueError("research and validation periods must be chronological and disjoint")
    return periods


def _base_overrides(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        "SCALPER_SIGNAL_MIN_SCORE": args.signal_min_score,
        "SCALPER_MIN_CONFIRMATIONS": args.min_confirmations,
        "SCALPER_ALLOWED_WIDTHS": args.allowed_widths,
        "SCALPER_PROFIT_CAPTURE_PCT": args.profit_capture_pct,
        "SCALPER_STOP_CREDIT_MULTIPLE": args.stop_credit_multiple,
        "SCALPER_TRAILING_ACTIVATION_PCT": args.trailing_activation_pct,
        "SCALPER_TRAILING_GIVEBACK_PCT": args.trailing_giveback_pct,
        "SCALPER_TIME_STOP_MINUTES": args.time_stop_minutes,
    }
    return {key: value for key, value in values.items() if value is not None}


def _experiment_grid(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.score_grid is not None and args.signal_min_score is not None:
        raise ValueError("--score-grid conflicts with --signal-min-score")
    if args.confirmation_grid is not None and args.min_confirmations is not None:
        raise ValueError("--confirmation-grid conflicts with --min-confirmations")
    base = _base_overrides(args)
    scores: tuple[float | None, ...] = args.score_grid or (None,)
    confirmations: tuple[int | None, ...] = args.confirmation_grid or (None,)
    grid = []
    for score, confirmation in product(scores, confirmations):
        override = dict(base)
        if score is not None:
            override["SCALPER_SIGNAL_MIN_SCORE"] = score
        if confirmation is not None:
            override["SCALPER_MIN_CONFIRMATIONS"] = confirmation
        grid.append(override)
    if len(grid) > 48:
        raise ValueError("SCALPER_REPLAY_GRID_TOO_LARGE")
    return grid


def _git_sha() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        periods = _periods(args)
        grid = _experiment_grid(args)
        if any(label == "OUT_OF_SAMPLE" for label, _, _ in periods) and len(grid) != 1:
            raise ValueError("VALIDATION_GRID_FORBIDDEN")

        settings = get_settings()
        database_url = args.database_url or os.getenv("DATABASE_URL")
        if database_url is None and settings.database_url:
            database_url = settings.database_url.get_secret_value()
        if not database_url:
            raise ValueError("DATABASE_URL is required")

        database = build_engine(database_url)
        sessions = build_session_factory(database)
        engine = ScalperReplayEngine(
            ScalperConfig.from_settings(settings), cost_schedule(settings))
        source_sha = _git_sha()
        tree_hash = implementation_hash()
        outputs = []
        try:
            for period_index, (label, start, end) in enumerate(periods, start=1):
                rows = load_replay_rows(sessions, start, end)
                for policy_index, overrides in enumerate(grid, start=1):
                    result = engine.run(
                        rows,
                        overrides=overrides,
                        split_label=label,
                        source_commit_sha=source_sha,
                        source_tree_hash=tree_hash,
                    )
                    single = len(periods) == 1 and len(grid) == 1
                    destination = args.output if single else args.output / (
                        f"{label.lower()}-policy-{policy_index:03d}")
                    files = write_replay_outputs(result, destination)
                    outputs.append({
                        "period_index": period_index,
                        "split_label": label,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "mode": result.manifest["mode"],
                        "policy_hash": result.manifest["policy_hash"],
                        "dataset_hash": result.manifest["dataset_hash"],
                        "summary": result.summary,
                        "files": files,
                    })
        finally:
            database.dispose()
    except Exception as exc:
        print(f"Scalper replay failed: {exc}", file=sys.stderr)
        return 1

    payload = {"runs": outputs}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in outputs:
            summary = item["summary"]
            print(
                "SCALPER_REPLAY_COMPLETE "
                f"split={item['split_label']} mode={item['mode']} "
                f"observations={summary['observations']} "
                f"closed_trades={summary['closed_trades']} "
                f"output={item['files']['manifest']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
