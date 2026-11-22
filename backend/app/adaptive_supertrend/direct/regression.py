"""Historical regression gate: the production AST_FUTURES_V1_DIRECT engine over completed 5-minute bars.

Each bar open is the next executable observation after the previous bar completed. The same pure engine used by
the PAPER worker makes every decision. Nothing here selects parameters or reads future bars.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys

from app.adaptive_supertrend.direct import VERSION
from app.adaptive_supertrend.direct.engine import ACTIVE, Observation, evaluate, step
from app.adaptive_supertrend.direct.policy import DirectPolicy
from app.adaptive_supertrend.research import load_bars
from app.parallel_paper.bars import Bar
from app.scalper.repository import IST

FROZEN_EXPECTATION = {
    "trades": 3461,
    "gross_points": 18031.89,
    "average_points": 5.21,
    "max_drawdown_points": 1549.20,
    "net_points_at_2pt": 11109.89,
    "net_max_drawdown_points_at_2pt": 1701.20,
}
TOLERANCE = {"trades": 0, "gross_points": 0.05, "average_points": 0.005, "max_drawdown_points": 0.05,
             "net_points_at_2pt": 0.05, "net_max_drawdown_points_at_2pt": 0.05}


def drawdown(values: list[float]) -> float:
    equity = peak = worst = Decimal(0)
    for value in values:
        equity += Decimal(str(value))
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return float(worst)


def replay(bars: list[Bar], policy: DirectPolicy) -> dict:
    evaluated = evaluate(bars)
    state: dict = {}
    active: dict | None = None
    finished: list[dict] = []
    day_start, day = 0, None
    for index, current in enumerate(evaluated):
        bar = current.bar
        if bar.open is None:  # Non-price session-gap marker; not an observation.
            continue
        local = bar.start.astimezone(IST)
        if local.date() != day:
            day, day_start = local.date(), index
        obs = Observation(
            snapshot_id=index,
            at=local,
            price=Decimal(str(bar.open)),
            future_id=getattr(bar, "future_id", None),
            future_expiry=getattr(bar, "future_expiry", None),
            lot_size=policy.lot_size,
            timing_ok=True,
        )
        result = step(policy, state, active, obs, evaluated[day_start:index])
        active = None
        for doc in (result.active, result.created):
            if doc is None:
                continue
            if doc["state"] in ACTIVE and doc["state"] != "UNRESOLVED_EXPOSURE":
                active = doc
            else:
                # Historical replay records unpriced exposure and continues; the worker requires an operator.
                finished.append(doc)
    if active is not None:
        finished.append(active)
    return summarize(finished)


def summarize(docs: list[dict], friction: float = 2.0) -> dict:
    closed = sorted((d for d in docs if d["state"] == "CLOSED"), key=lambda d: d["exit_timestamp"])
    points = [d["gross_points"] for d in closed]
    net = [p - friction for p in points]
    trades = [
        {
            "trading_date": d["trading_date"],
            "side": d["side"],
            "signal_timestamp": d["signal_timestamp"],
            "entry_timestamp": d["entry_timestamp"],
            "entry_price": d["entry_price"],
            "exit_timestamp": d["exit_timestamp"],
            "exit_price": d["exit_price"],
            "exit_reason": d["exit_reason"],
            "gross_points": d["gross_points"],
            "ast_transition": d["ast"]["transition"],
        }
        for d in closed
    ]
    total = float(sum(Decimal(str(p)) for p in points))
    return {
        "strategy_id": VERSION,
        "summary": {
            "trades": len(closed),
            "gross_points": round(total, 6),
            "average_points": total / len(closed) if closed else None,
            "max_drawdown_points": drawdown(points),
            "friction_points_per_trade": friction,
            "net_points_at_2pt": round(float(sum(Decimal(str(p)) for p in net)), 6),
            "net_max_drawdown_points_at_2pt": drawdown(net),
            "wins": sum(p > 0 for p in points),
            "losses": sum(p < 0 for p in points),
            "exit_reason_counts": dict(Counter(d["exit_reason"] for d in closed)),
            "rejected_entries": sum(d["state"] == "ENTRY_REJECTED" for d in docs),
            "rejection_reasons": dict(Counter(d["rejection_reason"] for d in docs if d["state"] == "ENTRY_REJECTED")),
            "unresolved_exposures": sum(d["state"] == "UNRESOLVED_EXPOSURE" for d in docs),
        },
        "trades": trades,
    }


def compare_summary(summary: dict, expected: dict = FROZEN_EXPECTATION) -> dict:
    deltas = {key: summary[key] - value if summary[key] is not None else None for key, value in expected.items()}
    failures = [key for key, delta in deltas.items() if delta is None or abs(delta) > TOLERANCE[key]]
    return {"expected": expected, "tolerance": TOLERANCE, "delta": deltas, "failures": failures,
            "status": "MATCH" if not failures else "MISMATCH"}


def load_ledger(path: Path) -> list[dict]:
    """Frozen research ledger in the repository's signal_trades vocabulary (entry_at, exit_at, signal_points)."""
    if path.suffix == ".json":
        value = json.loads(path.read_text())
        rows = value["trades"] if isinstance(value, dict) else value
    else:
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
    return [{"entry_at": r["entry_at"], "exit_at": r["exit_at"], "points": float(r["signal_points"])} for r in rows]


def compare_ledger(trades: list[dict], ledger: list[dict], tolerance: float = 0.005) -> dict:
    for index, (ours, theirs) in enumerate(zip(trades, ledger)):
        if (
            ours["entry_timestamp"] != theirs["entry_at"]
            or ours["exit_timestamp"] != theirs["exit_at"]
            or abs(ours["gross_points"] - theirs["points"]) > tolerance
        ):
            return {"status": "MISMATCH", "first_difference_index": index, "engine": ours, "frozen": theirs}
    if len(trades) != len(ledger):
        return {"status": "MISMATCH", "first_difference_index": min(len(trades), len(ledger)),
                "engine_count": len(trades), "frozen_count": len(ledger)}
    return {"status": "MATCH", "trades": len(trades)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--futures-csv", type=Path, required=True, help="completed 5-minute research CSV")
    parser.add_argument("--lot-size", type=int, required=True)
    parser.add_argument("--frozen-ledger", type=Path, help="optional frozen trade ledger to compare in sequence")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args(argv)
    policy = DirectPolicy(lot_size=args.lot_size)
    result = replay(load_bars(args.futures_csv), policy)
    result["policy"] = policy.model_dump(mode="json")
    result["policy_hash"] = policy.policy_hash
    result["frozen_summary_comparison"] = compare_summary(result["summary"])
    if args.frozen_ledger:
        result["frozen_ledger_comparison"] = compare_ledger(result["trades"], load_ledger(args.frozen_ledger))
    args.json.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"summary": result["summary"], "comparison": result["frozen_summary_comparison"]},
                     indent=2, sort_keys=True), file=sys.stderr)
    statuses = [result["frozen_summary_comparison"]["status"]]
    if args.frozen_ledger:
        statuses.append(result["frozen_ledger_comparison"]["status"])
    return 0 if all(status == "MATCH" for status in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
