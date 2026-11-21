"""Deterministic CSV futures signal research. Completed bars and pre-session optimization."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, time
import json
from pathlib import Path
import resource
from time import perf_counter

from app.adaptive_supertrend.bars import FutureBar
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.optimizer import optimize, walk_forward
from app.parallel_paper.bars import Bar
from app.scalper.repository import IST

SESSION_GAP = "AST_INCOMPLETE_SESSION_GAP"


class SessionGap(Bar):
    """Non-price missing-session interval; existing segmentation excludes invalid rows.

    Only this immutable invalid marker has None OHLC, rather than fake zero/filled prices.
    Its times describe the missing interval, not a broker observation or executable bar.
    """

    def __init__(self, start: datetime, end: datetime) -> None:
        super().__init__(start, end, None, None, None, None, None, False, SESSION_GAP)  # type: ignore[arg-type]


def _append_session_gap(result: list[Bar]) -> bool:
    if result:
        previous = result[-1]
        close = datetime.combine(previous.start.date(), time(15, 30), IST)
        if previous.end < close:
            result.append(SessionGap(previous.end, close))
            return True
    return False


def load_bars(path: Path) -> list[Bar]:
    result: list[Bar] = []
    previous_identity = None
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            start, end = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
            if start.tzinfo is None or end.tzinfo is None or (end - start).total_seconds() != 300:
                raise ValueError("AST_COMPLETED_AWARE_FIVE_MINUTE_BARS_REQUIRED")
            start, end = start.astimezone(IST), end.astimezone(IST)
            if (start.minute - 15) % 5 or start.second or start.microsecond:
                raise ValueError("AST_SESSION_ANCHORED_BARS_REQUIRED")
            if not time(9, 15) <= start.time().replace(tzinfo=None) < time(15, 30):
                raise ValueError("AST_BAR_OUTSIDE_SESSION")
            identity = (row["future_instrument_id"], date.fromisoformat(row["future_expiry"]))
            if not identity[0] or identity[1] < start.date():
                raise ValueError("AST_FUTURES_IDENTITY_REQUIRED")
            values = [float(row[key]) for key in ("open", "high", "low", "close")]
            from math import isfinite

            if (
                not all(isfinite(v) and v > 0 for v in values)
                or not values[2] <= min(values[0], values[3]) <= max(values[0], values[3]) <= values[1]
            ):
                raise ValueError("AST_MALFORMED_OHLC")
            if result and result[-1].end.date() < start.date() and _append_session_gap(result):
                # The boundary already breaks continuity; do not sacrifice the new opening bar.
                previous_identity = None
            contiguous = (
                not result
                or result[-1].end == start
                or (
                    result[-1].end.time().replace(tzinfo=None) == time(15, 30)
                    and start.time().replace(tzinfo=None) == time(9, 15)
                    and result[-1].end.date() < start.date()
                )
            )
            valid = previous_identity in (None, identity) and contiguous
            result.append(
                FutureBar(
                    start,
                    end,
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    None,
                    valid,
                    None if valid else "AST_CONTRACT_OR_SEQUENCE_BOUNDARY",
                    None,
                    identity[0],
                    identity[1],
                )
            )
            previous_identity = identity
    # Also represent the final truncated session without depending on a future CSV row.
    # A causal prefix before the scheduled close excludes this non-price marker by end time.
    _append_session_gap(result)
    if any(left.end > right.start for left, right in zip(result, result[1:])):
        raise ValueError("AST_CHRONOLOGICAL_BARS_REQUIRED")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True, help="out-of-sample session date")
    parser.add_argument("--grid", action="store_true")
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--benchmark", action="store_true")
    args = parser.parse_args(argv)
    if args.top < 1:
        parser.error("--top must be positive")
    policy = AstPolicy.model_validate_json(args.policy.read_text())
    bars = load_bars(args.csv)
    cutoff = datetime.combine(args.date, time(9, 15), IST)
    training = [bar for bar in bars if bar.start.date() < args.date and bar.end < cutoff]
    started = perf_counter()
    # Historical simulation records a deterministic pre-session selection clock, not wall-clock now.
    result = optimize(training, args.date, cutoff.replace(hour=9, minute=0), policy, signal_research_only=True)
    result["ranking"] = result["ranking"][: args.top]
    result.pop("training_bars")
    if args.walk_forward:
        result["walk_forward"] = walk_forward(bars, policy)
    if args.benchmark:
        result["benchmark"] = {
            "elapsed_seconds": perf_counter() - started,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "processes": 1,
            "grid_count": 1886,
        }
    output = json.dumps(result, indent=2, sort_keys=True, allow_nan=False)
    if args.json:
        args.json.write_text(output + "\n")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
