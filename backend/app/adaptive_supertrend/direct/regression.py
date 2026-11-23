"""Historical regression gate reproducing the frozen AST_FUTURES_V1_22_39 decade audit with the production engine.

Frozen audit semantics: exact 75-bar 5-minute sessions only; for each session, SuperTrend 22/3.9 is computed
freshly over the previous 15 exact sessions followed by the current session; the current session is then run
through the production AST_FUTURES_V1_DIRECT engine with one observation per bar at that bar's open. That gives
completed-close breakouts, next-bar entries, next-bar AST-flip exits and the 15:20 IST forced exit. The legacy
optimizer ``signal_trades`` 15:30 session-end mark is never a reference here.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
from math import isfinite
from pathlib import Path
import sys

from app.adaptive_supertrend.bars import FutureBar
from app.adaptive_supertrend.direct import VERSION
from app.adaptive_supertrend.direct.engine import ACTIVE, Observation, evaluate, step
from app.adaptive_supertrend.direct.policy import DirectPolicy
from app.parallel_paper.bars import Bar
from app.scalper.repository import IST

WARMUP_SESSIONS = 15
SESSION_MINUTES = 375
SESSION_BARS = 75
RESEARCH_INSTRUMENT = "NIFTY50_SPOT_1M_FROZEN_RESEARCH"
RESEARCH_EXPIRY = date(9999, 12, 31)
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
PRICE_TOLERANCE = 0.005
LEDGER_COLUMNS = ("date", "direction", "entry_at", "exit_at", "entry", "exit", "points", "exit_reason")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ist(value: str) -> datetime:
    """Aware timestamps are converted to IST; naive research timestamps are IST wall-clock times."""
    parsed = datetime.fromisoformat(value.strip())
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def _columns(fieldnames) -> dict[str, str]:
    names = {name.strip().lower(): name for name in fieldnames or []}
    missing = [key for key in ("open", "high", "low", "close") if key not in names]
    if missing:
        raise ValueError(f"AST_ANGEL_1M_COLUMNS_MISSING:{','.join(missing)}")
    return names


def _timestamp(row: dict, names: dict[str, str]) -> str:
    for key in ("timestamp", "datetime", "date_time"):
        if key in names:
            return row[names[key]]
    if "date" in names and "time" in names:
        return f"{row[names['date']].strip()} {row[names['time']].strip()}"
    if "date" in names:
        return row[names["date"]]
    raise ValueError("AST_ANGEL_1M_TIMESTAMP_COLUMN_MISSING")


def angel_sessions(path: Path) -> tuple[list[list[Bar]], dict]:
    """Exact full sessions from Angel 1-minute OHLC: 375 consecutive rows 09:15..15:29 IST -> 75 bars each.

    Nothing is synthesized, forward-filled or mixed across sessions; a non-exact session is excluded whole.
    """
    by_day: dict[date, list[tuple[datetime, tuple[float, float, float, float]]]] = {}
    invalid: set[date] = set()
    previous: datetime | None = None
    basis: Counter = Counter()
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        names = _columns(reader.fieldnames)
        for line, row in enumerate(reader, 2):
            raw = _timestamp(row, names)
            basis["aware" if datetime.fromisoformat(raw.strip()).tzinfo else "naive_ist"] += 1
            at = ist(raw)
            if previous is not None and at <= previous:
                raise ValueError(f"AST_ANGEL_1M_STRICT_CHRONOLOGY_REQUIRED line={line}")
            previous = at
            try:
                values = tuple(float(row[names[key]]) for key in ("open", "high", "low", "close"))
            except (TypeError, ValueError):
                values = (float("nan"),) * 4
            o, h, low, c = values
            if not all(isfinite(v) and v > 0 for v in values) or not low <= min(o, c) <= max(o, c) <= h:
                invalid.add(at.date())
            by_day.setdefault(at.date(), []).append((at, values))  # type: ignore[arg-type]
    sessions: list[list[Bar]] = []
    excluded: dict[str, str] = {}
    for day in sorted(by_day):
        rows = by_day[day]
        start = datetime.combine(day, time(9, 15), IST)
        if day in invalid:
            excluded[str(day)] = "INVALID_OHLC"
        elif len(rows) != SESSION_MINUTES:
            excluded[str(day)] = f"ROW_COUNT_{len(rows)}"
        elif any(at != start + timedelta(minutes=index) for index, (at, _) in enumerate(rows)):
            excluded[str(day)] = "MINUTE_SEQUENCE_NOT_09:15_TO_15:29"
        else:
            bars: list[Bar] = []
            for index in range(SESSION_BARS):
                group = rows[index * 5:index * 5 + 5]
                bar_start = group[0][0]
                bars.append(FutureBar(
                    bar_start, bar_start + timedelta(minutes=5), group[0][1][0], max(v[1] for _, v in group),
                    min(v[2] for _, v in group), group[-1][1][3], None, True, None, None,
                    RESEARCH_INSTRUMENT, RESEARCH_EXPIRY,
                ))
            sessions.append(bars)
    if not sessions:
        raise ValueError("AST_ANGEL_1M_NO_EXACT_SESSIONS")
    return sessions, {
        "source": "ANGEL_1M",
        "timestamp_basis": dict(basis),
        "sessions_seen": len(by_day),
        "exact_sessions": len(sessions),
        "excluded_sessions": excluded,
    }


def futures_sessions(bars: list[Bar]) -> tuple[list[list[Bar]], dict]:
    """Exact sessions from an already-completed 5-minute research CSV (same exactness rules)."""
    by_day: dict[date, list[Bar]] = {}
    for bar in bars:
        if bar.open is not None:
            by_day.setdefault(bar.start.astimezone(IST).date(), []).append(bar)
    sessions, excluded = [], {}
    for day in sorted(by_day):
        rows = by_day[day]
        start = datetime.combine(day, time(9, 15), IST)
        identities = {(getattr(b, "future_id", None), getattr(b, "future_expiry", None)) for b in rows}
        if len(rows) != SESSION_BARS or not all(b.valid for b in rows) or len(identities) != 1 or any(
            b.start != start + timedelta(minutes=5 * index) for index, b in enumerate(rows)
        ):
            excluded[str(day)] = "NOT_EXACT_75_BAR_SINGLE_CONTRACT_SESSION"
        else:
            sessions.append(rows)
    return sessions, {"source": "FUTURES_5M", "sessions_seen": len(by_day), "exact_sessions": len(sessions),
                      "excluded_sessions": excluded}


def _drive(policy: DirectPolicy, evaluated, state: dict, active, finished: list, counter: list[int]):
    """Production engine, one observation per bar at that bar's open, using only bars completed before it."""
    for index, current in enumerate(evaluated):
        bar = current.bar
        counter[0] += 1
        obs = Observation(
            snapshot_id=counter[0],
            at=bar.start.astimezone(IST),
            price=Decimal(str(bar.open)),
            future_id=getattr(bar, "future_id", None),
            future_expiry=getattr(bar, "future_expiry", None),
            lot_size=policy.lot_size,
            timing_ok=True,
        )
        result = step(policy, state, active, obs, evaluated[:index])
        active = None
        for doc in (result.active, result.created):
            if doc is None:
                continue
            if doc["state"] in ACTIVE and doc["state"] != "UNRESOLVED_EXPOSURE":
                active = doc
            else:
                finished.append(doc)
    return active


def replay_sessions(sessions: list[list[Bar]], policy: DirectPolicy) -> dict:
    """Frozen decade audit: 15 prior exact sessions as fresh causal warm-up for each evaluated session."""
    state: dict = {}
    active = None
    finished: list[dict] = []
    counter = [0]
    evaluated_dates = []
    for index in range(WARMUP_SESSIONS, len(sessions)):
        prefix = [bar for session in sessions[index - WARMUP_SESSIONS:index] for bar in session]
        today = sessions[index]
        evaluated = evaluate([*prefix, *today])[len(prefix):]
        evaluated_dates.append(today[0].start.astimezone(IST).date().isoformat())
        active = _drive(policy, evaluated, state, active, finished, counter)
    if active is not None:
        finished.append(active)
    result = summarize(finished)
    result["warmup"] = {
        "sessions": WARMUP_SESSIONS,
        "construction": "FRESH_SUPERTREND_OVER_PREVIOUS_15_EXACT_SESSIONS_THEN_CURRENT",
        "evaluated_sessions": len(evaluated_dates),
        "first_evaluated_date": evaluated_dates[0] if evaluated_dates else None,
        "last_evaluated_date": evaluated_dates[-1] if evaluated_dates else None,
    }
    return result


def replay_continuous(bars: list[Bar], policy: DirectPolicy) -> dict:
    """Diagnostic only: continuous AST history. Not the frozen audit's warm-up construction."""
    evaluated = evaluate(bars)
    state: dict = {}
    active = None
    finished: list[dict] = []
    counter = [0]
    day_rows: list = []
    day = None
    for current in evaluated:
        if current.bar.open is None:
            continue
        local = current.bar.start.astimezone(IST).date()
        if local != day:
            if day_rows:
                active = _drive(policy, day_rows, state, active, finished, counter)
            day, day_rows = local, []
        day_rows.append(current)
    if day_rows:
        active = _drive(policy, day_rows, state, active, finished, counter)
    if active is not None:
        finished.append(active)
    return summarize(finished)


def drawdown(values: list[float]) -> float:
    equity = peak = worst = Decimal(0)
    for value in values:
        equity += Decimal(str(value))
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return float(worst)


def daily(points_by_trade: list[tuple[str, float]]) -> list[float]:
    totals: dict[str, Decimal] = {}
    for day, value in points_by_trade:
        totals[day] = totals.get(day, Decimal(0)) + Decimal(str(value))
    return [float(totals[day]) for day in sorted(totals)]


def summarize(docs: list[dict], friction: float = 2.0) -> dict:
    closed = sorted((d for d in docs if d["state"] == "CLOSED"), key=lambda d: d["exit_timestamp"])
    points = [d["gross_points"] for d in closed]
    gross_by_day = [(d["trading_date"], d["gross_points"]) for d in closed]
    net_by_day = [(d["trading_date"], float(Decimal(str(d["gross_points"])) - Decimal(str(friction)))) for d in closed]
    total = float(sum(Decimal(str(p)) for p in points))
    trades = [
        {
            "trading_date": d["trading_date"],
            "side": d["side"],
            "direction": d["direction"],
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
    return {
        "strategy_id": VERSION,
        "summary": {
            "trades": len(closed),
            "gross_points": round(total, 6),
            "average_points": total / len(closed) if closed else None,
            # Frozen audit definition: closed-trade points summed per trading date, equity from zero.
            "drawdown_definition": "DAILY_EQUITY_FROM_ZERO",
            "max_drawdown_points": drawdown(daily(gross_by_day)),
            "friction_points_per_trade": friction,
            "net_points_at_2pt": round(float(sum(Decimal(str(v)) for _, v in net_by_day)), 6),
            "net_max_drawdown_points_at_2pt": drawdown(daily(net_by_day)),
            "trade_sequence_max_drawdown_points": drawdown(points),
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


def _side(value: str) -> str:
    text = value.strip().upper()
    return {"BULL": "LONG", "LONG": "LONG", "BEAR": "SHORT", "SHORT": "SHORT"}.get(text, text)


def load_frozen_ledger(path: Path) -> tuple[list[dict], dict]:
    """``ast-decade-fast-trades.csv``. Only the frozen 22 / 3.9 rows are compared."""
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        missing = [column for column in LEDGER_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"AST_FROZEN_LEDGER_COLUMNS_MISSING:{','.join(missing)}")
        rows, other = [], 0
        for row in reader:
            if ("atr_period" in row and int(float(row["atr_period"])) != 22) or (
                "multiplier" in row and abs(float(row["multiplier"]) - 3.9) > 1e-9
            ):
                other += 1
                continue
            rows.append({
                "trading_date": date.fromisoformat(row["date"].strip()[:10]).isoformat(),
                "side": _side(row["direction"]),
                "entry_at": ist(row["entry_at"]),
                "exit_at": ist(row["exit_at"]),
                "entry_price": float(row["entry"]),
                "exit_price": float(row["exit"]),
                "gross_points": float(row["points"]),
                "exit_reason": row["exit_reason"].strip(),
            })
    rows.sort(key=lambda r: r["entry_at"])
    return rows, {"rows": len(rows), "rows_other_parameters_excluded": other,
                  "exit_reasons": dict(Counter(r["exit_reason"] for r in rows))}


def compare_frozen_ledger(trades: list[dict], ledger: list[dict], *, exit_reason_map: dict | None = None,
                          tolerance: float = PRICE_TOLERANCE) -> dict:
    """Exact dates, sides, timestamps and exit reasons; tolerance only for prices and points."""
    labels = exit_reason_map or {}
    ours = sorted(trades, key=lambda t: datetime.fromisoformat(t["entry_timestamp"]))
    for index, (actual, expected) in enumerate(zip(ours, ledger)):
        view = {
            "trading_date": actual["trading_date"],
            "side": actual["side"],
            "entry_at": datetime.fromisoformat(actual["entry_timestamp"]).astimezone(IST),
            "exit_at": datetime.fromisoformat(actual["exit_timestamp"]).astimezone(IST),
            "entry_price": actual["entry_price"],
            "exit_price": actual["exit_price"],
            "gross_points": actual["gross_points"],
            "exit_reason": labels.get(actual["exit_reason"], actual["exit_reason"]),
        }
        fields = [
            key for key in view
            if (abs(view[key] - expected[key]) > tolerance
                if key in ("entry_price", "exit_price", "gross_points") else view[key] != expected[key])
        ]
        if fields:
            return {
                "status": "MISMATCH",
                "first_difference_index": index,
                "fields": fields,
                "expected": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in expected.items()},
                "actual": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in view.items()},
            }
    if len(ours) != len(ledger):
        return {"status": "MISMATCH", "first_difference_index": min(len(ours), len(ledger)), "fields": ["count"],
                "expected_count": len(ledger), "actual_count": len(ours)}
    return {"status": "MATCH", "trades": len(ours), "price_tolerance": tolerance}


def _reason_map(text: str | None) -> dict:
    if not text:
        return {}
    pairs = [item.split("=", 1) for item in text.split(",") if item.strip()]
    if any(len(pair) != 2 for pair in pairs):
        raise ValueError("AST_EXIT_REASON_MAP_FORMAT:ENGINE=FROZEN,...")
    return {engine.strip(): frozen.strip() for engine, frozen in pairs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--angel-1m-csv", type=Path, help="Angel NIFTY 1-minute full-session OHLC CSV")
    source.add_argument("--futures-csv", type=Path, help="completed 5-minute research CSV")
    parser.add_argument("--lot-size", type=int, default=65, help="INR scaling only; points are unaffected")
    parser.add_argument("--frozen-ledger", type=Path, help="ast-decade-fast-trades.csv")
    parser.add_argument("--frozen-summary", type=Path, help="ast-decade-fast-summary.csv (recorded verbatim)")
    parser.add_argument("--frozen-friction-summary", type=Path, help="ast-futures-friction-summary.csv (verbatim)")
    parser.add_argument("--exit-reason-map", help="engine-to-frozen label vocabulary, e.g. FORCED_EXIT=SESSION_END")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        policy = DirectPolicy(lot_size=args.lot_size)
        if args.angel_1m_csv:
            sessions, construction = angel_sessions(args.angel_1m_csv)
        else:
            from app.adaptive_supertrend.research import load_bars

            sessions, construction = futures_sessions(load_bars(args.futures_csv))
        result = replay_sessions(sessions, policy)
        result["dataset"] = construction
        result["policy"] = policy.model_dump(mode="json")
        result["policy_hash"] = policy.policy_hash
        result["frozen_summary_comparison"] = compare_summary(result["summary"])
        statuses = [result["frozen_summary_comparison"]["status"]]
        keys = ("angel_1m_csv", "futures_csv", "frozen_ledger", "frozen_summary", "frozen_friction_summary")
        inputs = {k: getattr(args, k) for k in keys if getattr(args, k)}
        result["input_sha256"] = {k: sha256(p) for k, p in inputs.items()}
        for key in ("frozen_summary", "frozen_friction_summary"):
            path = getattr(args, key)
            if path:
                with path.open(newline="") as stream:
                    result[f"{key}_rows"] = list(csv.DictReader(stream))
        if args.frozen_ledger:
            ledger, info = load_frozen_ledger(args.frozen_ledger)
            labels = _reason_map(args.exit_reason_map)
            comparison = compare_frozen_ledger(result["trades"], ledger, exit_reason_map=labels)
            result["frozen_ledger_comparison"] = {**comparison, "ledger": info,
                                                  "engine_exit_reasons": result["summary"]["exit_reason_counts"]}
            statuses.append(comparison["status"])
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    args.json.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    report = {"summary": result["summary"], "aggregate": result["frozen_summary_comparison"]["status"],
              "dataset": {k: v for k, v in construction.items() if k != "excluded_sessions"}}
    if args.frozen_ledger:
        report["ledger"] = {k: v for k, v in result["frozen_ledger_comparison"].items() if k != "ledger"}
    print(json.dumps(report, indent=2, sort_keys=True, default=str), file=sys.stderr)
    return 0 if all(status == "MATCH" for status in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
