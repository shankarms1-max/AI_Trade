"""Replay one frozen AST futures PAPER policy from local files, without a database or broker."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
import gzip
from itertools import groupby
import json
from pathlib import Path
import sys

from app.adaptive_supertrend import VERSION
from app.adaptive_supertrend.bootstrap import file_sha256
from app.adaptive_supertrend.bars import FutureBar, build_bars
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.futures_replay_report import comparison, signal_counterpart, summary, trade_result
from app.adaptive_supertrend.journal import ACTIVE
from app.adaptive_supertrend.lifecycle import advance
from app.adaptive_supertrend.optimizer import optimize, selected_parameters
from app.adaptive_supertrend.research import load_bars
from app.parallel_paper.bars import Bar, MarketPoint
from app.scalper.repository import IST


@dataclass(frozen=True)
class Observation:
    id: int  # Archive line number, independent of any runtime database primary key.
    captured_at: datetime
    request_started_at: datetime
    response_received_at: datetime
    nifty_future: Decimal | None
    future_instrument_id: str | None
    future_expiry: date | None
    lot_size: int | None


def timestamp(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError("AST_REPLAY_AWARE_ORIGINAL_CLOCK_REQUIRED")
    return at.astimezone(IST)


def observations(path):
    previous = None
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for index, line in enumerate(stream, 1):
            value = json.loads(line, parse_float=Decimal)
            if not isinstance(value, dict):
                raise ValueError(f"AST_REPLAY_SNAPSHOT_OBJECT_REQUIRED line={index}")
            context = value.get("context_json") or {}
            if isinstance(context, str):
                context = json.loads(context)
            if not isinstance(context, dict):
                raise ValueError(f"AST_REPLAY_MALFORMED_SOURCE_CONTEXT line={index}")
            for mode in (value.get("mode"), value.get("source_mode"), context.get("mode"), context.get("source_mode")):
                if mode is not None and mode != "ACTS_PAPER":
                    raise ValueError(f"AST_REPLAY_FOREIGN_SOURCE line={index}")
            at = timestamp(value["captured_at"])
            if previous is not None and at <= previous:
                raise ValueError(f"AST_REPLAY_STRICT_OBSERVATION_ORDER_REQUIRED line={index}")
            if previous is not None and int(previous.timestamp()) // 15 == int(at.timestamp()) // 15:
                raise ValueError(f"AST_REPLAY_DUPLICATE_NATIVE_CAPTURE_BUCKET line={index}")
            previous = at
            price = value.get("nifty_future")
            price = Decimal(str(price)) if price is not None else None
            if price is not None and not price.is_finite():
                raise ValueError(f"AST_REPLAY_NONFINITE_PRICE line={index}")
            if value.get("future_instrument_id") is not None and not isinstance(value["future_instrument_id"], str):
                raise ValueError(f"AST_REPLAY_MALFORMED_FUTURE_IDENTITY line={index}")
            yield Observation(
                index,
                at,
                timestamp(value["request_started_at"]),
                timestamp(value["response_received_at"]),
                price,
                value.get("future_instrument_id"),
                date.fromisoformat(value["future_expiry"]) if value.get("future_expiry") else None,
                value.get("lot_size"),
            )


def for_identity(bars, identity):
    return [
        (
            replace(b, valid=False, reason="FUTURES_IDENTITY_OR_PRICE_UNAVAILABLE")
            if b.valid and (b.future_id, b.future_expiry) != identity
            else b
        )
        for b in bars
    ]


def valid_ohlc(bars: list[Bar]) -> dict:
    return {
        b.end: (b.open, b.high, b.low, b.close, b.future_id, b.future_expiry)
        for b in bars if isinstance(b, FutureBar) and b.valid
    }


def run_replay(csv_path: Path, snapshots: Path, policy: AstPolicy, progress=None) -> dict:
    if policy.missing or policy.optimizer is None or policy.optimizer.lookback_sessions != 15:
        raise ValueError("AST_REPLAY_REQUIRES_ONE_COMPLETE_15_SESSION_POLICY")
    cfg = policy.execution
    assert cfg is not None
    csv_days = defaultdict(list)
    for bar in load_bars(csv_path):
        csv_days[bar.start.date()].append(bar)
    if not csv_days:
        raise ValueError("AST_REPLAY_EMPTY_FUTURES_CSV")
    history = dict(csv_days)
    ledger, days, signals = [], [], []
    state: dict = {}
    active = None
    all_opportunities: set[str] = set()
    for day, rows in groupby(observations(snapshots), key=lambda r: r.captured_at.date()):
        csv_session = csv_days.get(day, [])
        previous_days = sorted(d for d in history if d < day)[-15:]
        past = [bar for d in previous_days for bar in history[d]]
        params = None
        freeze_status = "INSUFFICIENT_PAST_SESSIONS"
        if progress:
            progress(f"{day}: {len(previous_days)}/15 preceding sessions")
        if len(previous_days) == 15:
            result = optimize(past, day, datetime.combine(day, time(9), IST), policy)
            params = selected_parameters(result) if result["selected"] else None
            freeze_status = "FROZEN" if params else "NO_ELIGIBLE_CONFIGURATION"
            del result
        points: list[MarketPoint] = []
        blockers: Counter = Counter()
        transitions: Counter = Counter()
        contracts: set[tuple] = set()
        gaps = intrabar = intrabar_only = cutoff_rows = 0
        max_gap = 0.0
        previous = None
        cache_key = None
        bars = []
        for raw in rows:
            at = raw.captured_at
            identity = raw.future_instrument_id, raw.future_expiry
            contracts.add((identity[0], str(identity[1])))
            if previous:
                gap = (at - previous).total_seconds()
                max_gap = max(max_gap, gap)
                gaps += gap > policy.max_sequence_gap_seconds
            previous = at
            cutoff_rows += cfg.session_exit_time <= at.time() < time(15, 30, 15)
            valid_timing = raw.request_started_at <= raw.response_received_at <= at
            points.append(
                MarketPoint(
                    at, float(raw.nifty_future) if valid_timing and raw.nifty_future is not None else None, *identity
                )
            )
            # Completed bars do not change within a five-minute bucket. Only the accumulated prefix is visible.
            key = (at.hour, at.minute // 5, identity)
            if key != cache_key:
                bars = for_identity(past + build_bars(points, at, policy.max_sequence_gap_seconds), identity)
                cache_key = key
            step = advance(policy, raw, at, bars, params, state, active)
            blockers.update(set(step.decision["blockers"]))
            if step.signal_opportunity:
                all_opportunities.add(step.signal_opportunity)
            if step.transition:
                transitions[step.transition] += 1
            if active is not None and active.state not in ACTIVE:
                active = None
            if step.pending is not None:
                active = step.pending
                ledger.append(active)
                intrabar += (at.minute % 5, at.second, at.microsecond) != (0, 0, 0)
                feature = step.decision["features"]
                intrabar_only += (
                    bars[-1].close <= feature["opening_high"]
                    if step.decision["direction"] == "BULL"
                    else bars[-1].close >= feature["opening_low"]
                )
        close = datetime.combine(day, time(15, 30), IST)
        # Completed raw sessions replace the CSV seed prospectively. Keep the scheduled gap boundary.
        history[day] = build_bars([p for p in points if p.at <= close], close, policy.max_sequence_gap_seconds)
        if params and csv_session:
            # Post-session comparison only; these results never feed selection or paper state.
            signals.extend(
                signal_counterpart(
                    [b for d in [*previous_days, day] for b in csv_days.get(d, [])], day, params, policy
                )
            )
        raw_ohlc, csv_ohlc = valid_ohlc(history[day]), valid_ohlc(csv_session)
        days.append(
            {
                "date": str(day),
                "freeze_status": freeze_status,
                "training_sessions": list(map(str, previous_days)),
                "parameters": params,
                "observations": len(points),
                "first_observation": points[0].at.isoformat(),
                "last_observation": points[-1].at.isoformat(),
                "observations_at_or_after_exit_cutoff": cutoff_rows,
                "intrabar_pending_decisions": intrabar,
                "pending_breakouts_absent_at_last_completed_bar_close": intrabar_only,
                "completed_csv_session_end_bar": close in csv_ohlc,
                "csv_session_present": bool(csv_session),
                "valid_bars_only_in_csv": len(csv_ohlc.keys() - raw_ohlc.keys()),
                "valid_bars_only_in_raw": len(raw_ohlc.keys() - csv_ohlc.keys()),
                "disagreeing_valid_bars": sum(raw_ohlc[t] != csv_ohlc[t] for t in raw_ohlc.keys() & csv_ohlc.keys()),
                "gap_count_above_policy_limit": gaps,
                "max_observation_gap_seconds": max_gap,
                "observed_contracts": sorted(contracts, key=str),
                "decision_blockers_by_observation": dict(blockers),
                "transitions": dict(transitions),
                "end_of_data_position_state": active.state if active is not None else None,
            }
        )
    if not days:
        raise ValueError("AST_REPLAY_EMPTY_OBSERVATIONS")
    trades = [trade_result(t) for t in ledger]
    totals = summary(trades, len(all_opportunities))
    replay_dates = {r["date"] for r in days}
    return {
        "strategy_version": VERSION,
        "mode": "OFFLINE_PAPER_REPLAY",
        "policy": policy.model_dump(mode="json"),
        "policy_hash": policy.policy_hash,
        "sessions": days,
        "trades": trades,
        "summary": totals,
        "comparison": comparison(totals, signals, trades),
        "csv_dates_without_observations": sorted(str(d) for d in csv_days if str(d) not in replay_dates),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--futures-csv", required=True, type=Path)
    parser.add_argument("--snapshots", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--session-exit-time", type=time.fromisoformat, help="one explicit, fixed IST cutoff")
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args(argv)
    paths = {"futures_csv": args.futures_csv, "snapshots": args.snapshots, "policy": args.policy}
    try:
        if any(
            args.json.resolve() == p.resolve() or (args.json.exists() and args.json.samefile(p))
            for p in paths.values()
        ):
            parser.error("AST_REPLAY_OUTPUT_ALIASES_INPUT")
        fingerprints = {k: file_sha256(p) for k, p in paths.items()}
        config = json.loads(args.policy.read_text())
        if args.session_exit_time is not None:
            config["execution"]["session_exit_time"] = args.session_exit_time.isoformat()
        policy = AstPolicy.model_validate(config)
        report = run_replay(args.futures_csv, args.snapshots, policy, progress=lambda msg: print(msg, file=sys.stderr))
        if fingerprints != {k: file_sha256(p) for k, p in paths.items()}:
            parser.error("AST_REPLAY_INPUT_CHANGED")
        report["input_sha256"] = fingerprints
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    except (KeyError, TypeError, ValueError, OSError, ArithmeticError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
