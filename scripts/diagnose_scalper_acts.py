#!/usr/bin/env python3
"""Read an existing strategy-lab replay and explain ACTS regime gates per capture."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, time
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def _rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def _relation(price: float | None, midpoint: float | None) -> str:
    if price is None or midpoint is None:
        return "UNAVAILABLE"
    return "ABOVE" if price > midpoint else "BELOW" if price < midpoint else "AT"


def diagnose(contexts: list[dict[str, Any]],
             decisions: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Report existing evidence; never recalculate or override a strategy decision."""
    by_id = {(row["snapshot_id"], row["strategy_id"]): row for row in decisions}
    by_time = {datetime.fromisoformat(row["timestamp"]): row for row in contexts}
    counts: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    trend_split: Counter[str] = Counter()
    acts_split: Counter[str] = Counter()
    decision_split: Counter[str] = Counter()
    alpha_split: Counter[str] = Counter()
    details = []
    opening_times = []
    for ctx in contexts:
        structure = ctx.get("structure") or {}
        returns = ctx.get("futures_returns_bps") or {}
        five, fifteen = returns.get("5m"), returns.get("15m")
        position = structure.get("range_position")
        high, low = ctx.get("opening_high"), ctx.get("opening_low")
        midpoint = (high + low) / 2 if high is not None and low is not None else None
        spot, future = ctx.get("spot"), ctx.get("future")
        complete = structure.get("opening_range_complete") is True
        actual = (ctx.get("acts") or {}).get("direction")
        acts_input = (ctx.get("acts") or {}).get("regime") or {}
        alpha = ((ctx.get("acts") or {}).get("alpha") or {}).get("status", "MISSING")
        acts_decision = by_id.get((ctx["snapshot_id"], "ACTS_V1"), {})
        trend = by_id.get((ctx["snapshot_id"], "TREND_PULLBACK"), {})
        trend_direction = trend.get("direction")
        trend_split[trend_direction or "RANGE"] += 1
        acts_split[actual or "RANGE"] += 1
        decision_split[acts_decision.get("regime", "MISSING")] += 1
        alpha_split[alpha] += 1
        counts["observations"] += 1
        if "ACTS_DIRECTIONAL_REGIME_REQUIRED" in acts_decision.get("blockers", []):
            counts["acts_directional_regime_blocker"] += 1
        for name, value in (("futures_5m_bps", five),
                            ("futures_15m_bps", fifteen),
                            ("range_position", position)):
            if acts_input.get(name) != value:
                counts[f"acts_input_mismatch_{name}"] += 1
        for field, value in (("nifty_future", future),
                             ("future_instrument_id", ctx.get("future_instrument_id")),
                             ("future_expiry", ctx.get("future_expiry")),
                             ("futures_5m", five), ("futures_15m", fifteen),
                             ("range_position", position), ("opening_high", high),
                             ("opening_low", low), ("opening_midpoint", midpoint),
                             ("acts_direction", actual)):
            if value is None:
                missing[field] += 1
        if not complete:
            missing["opening_range_incomplete"] += 1
        if "opening_range_complete" not in structure:
            missing["opening_range_complete"] += 1
        if five is not None:
            counts["valid_futures_5m"] += 1
        if fifteen is not None:
            counts["valid_futures_15m"] += 1
        if future is not None:
            counts["valid_nifty_future"] += 1
        if ctx.get("future_instrument_id") and ctx.get("future_expiry"):
            counts["valid_future_identity"] += 1
        if position is not None:
            counts["valid_range_position"] += 1
        if midpoint is not None:
            counts["valid_opening_midpoint"] += 1
        if complete:
            counts["opening_range_complete"] += 1
        spot_relation = _relation(spot, midpoint)
        future_relation = _relation(future, midpoint)
        if spot_relation in {"ABOVE", "BELOW"}:
            counts[f"spot_{spot_relation.lower()}_opening_midpoint"] += 1
        if future_relation in {"ABOVE", "BELOW"}:
            counts[f"future_{future_relation.lower()}_opening_midpoint"] += 1
        for direction, sign, boundary in (("BULL", 1, .55), ("BEAR", -1, .45)):
            if five is not None and sign * five >= 4:
                counts[f"futures_5m_{direction.lower()}_pass"] += 1
            if fifteen is not None and sign * fifteen >= 8:
                counts[f"futures_15m_{direction.lower()}_pass"] += 1
            passes_range = (position is not None and
                            (position >= boundary if sign > 0 else position <= boundary))
            if passes_range:
                counts[f"range_{direction.lower()}_pass"] += 1
            base = (five is not None and fifteen is not None and position is not None
                    and complete and midpoint is not None and spot is not None
                    and sign * five >= 4 and sign * fifteen >= 8
                    and (position >= boundary if sign > 0 else position <= boundary)
                    and sign * (spot - midpoint) > 0)
            if base:
                counts[f"pre_vwap_{direction.lower()}_pass"] += 1
        if actual in {"BULL", "BEAR"}:
            counts[f"full_{actual.lower()}_regime_before_alpha"] += 1
        if trend_direction in {"BULL", "BEAR"} and actual != trend_direction:
            counts[f"trend_{trend_direction.lower()}_acts_other"] += 1
        references = structure.get("horizon_reference_at") or {}
        continuity = {}
        for horizon in ("5m", "15m"):
            reference_time = references.get(horizon)
            reference = (by_time.get(datetime.fromisoformat(reference_time))
                         if reference_time else None)
            if reference is None:
                continuity[horizon] = "NO_REFERENCE"
                missing[f"future_reference_{horizon}"] += 1
            elif not ctx.get("future_instrument_id") or not ctx.get("future_expiry"):
                continuity[horizon] = "MISSING_CURRENT_IDENTITY"
            elif (ctx.get("future_instrument_id"), ctx.get("future_expiry")) != (
                    reference.get("future_instrument_id"), reference.get("future_expiry")):
                continuity[horizon] = "CHANGED_CONTRACT"
                counts[f"future_contract_change_{horizon}"] += 1
            else:
                continuity[horizon] = "SAME_CONTRACT"
                counts[f"same_future_contract_{horizon}"] += 1
        timestamp = datetime.fromisoformat(ctx["timestamp"])
        if timestamp.tzinfo is None:
            missing["timestamp_timezone"] += 1
        elif time(9, 15) <= timestamp.astimezone(IST).time() < time(9, 30):
            opening_times.append(timestamp.isoformat())
        detail = {
            "snapshot_id": ctx["snapshot_id"], "timestamp": ctx["timestamp"],
            "timezone_offset": (timestamp.utcoffset().total_seconds() if timestamp.tzinfo
                                else None),
            "nifty_future": future, "future_instrument_id": ctx.get("future_instrument_id"),
            "future_expiry": ctx.get("future_expiry"),
            "futures_5m_bps": five, "futures_15m_bps": fifteen,
            "futures_reference_at": {h: references.get(h) for h in ("5m", "15m")},
            "futures_contract_continuity": continuity,
            "range_position": position, "opening_range_complete": complete,
            "opening_high": high, "opening_low": low, "opening_midpoint": midpoint,
            "spot": spot, "spot_to_opening_midpoint": spot_relation,
            "future_to_opening_midpoint": future_relation,
            "vwap": ctx.get("vwap"), "phase15_regime": ctx.get("regime"),
            "acts_regime": actual or "RANGE", "acts_alpha_status": alpha,
            "acts_regime_evidence": acts_input,
            "acts_decision_regime": acts_decision.get("regime"),
            "acts_decision_blockers": acts_decision.get("blockers"),
            "trend_pullback_direction": trend_direction,
            "trend_pullback_status": trend.get("status"),
            "trend_pullback_blockers": trend.get("blockers"),
        }
        details.append(detail)

    def strength(row: dict[str, Any], sign: int) -> float:
        five, fifteen, position = (row[key] for key in (
            "futures_5m_bps", "futures_15m_bps", "range_position"))
        if five is None or fifteen is None or position is None:
            return float("-inf")
        return sign * five / 4 + sign * fifteen / 8 + sign * (position - .5) * 10

    strongest = sorted((row for row in details
                        if strength(row, 1) != float("-inf")),
                       key=lambda row: (-max(strength(row, 1), strength(row, -1)),
                                        row["timestamp"]))[:10]
    keys = ("acts_directional_regime_blocker", "acts_input_mismatch_futures_5m_bps",
            "acts_input_mismatch_futures_15m_bps",
            "acts_input_mismatch_range_position",
            "valid_nifty_future", "valid_future_identity",
            "valid_futures_5m", "valid_futures_15m",
            "futures_5m_bull_pass", "futures_5m_bear_pass",
            "futures_15m_bull_pass", "futures_15m_bear_pass",
            "valid_range_position", "range_bull_pass", "range_bear_pass",
            "valid_opening_midpoint", "opening_range_complete",
            "spot_above_opening_midpoint",
            "spot_below_opening_midpoint", "full_bull_regime_before_alpha",
            "full_bear_regime_before_alpha", "pre_vwap_bull_pass",
            "pre_vwap_bear_pass", "trend_bull_acts_other", "trend_bear_acts_other",
            "same_future_contract_5m", "same_future_contract_15m",
            "future_contract_change_5m", "future_contract_change_15m")
    summary = {"observations": len(contexts),
               "first_capture": contexts[0]["timestamp"] if contexts else None,
               "last_capture": contexts[-1]["timestamp"] if contexts else None,
               "opening_window_observations": len(opening_times),
               "opening_window_first": opening_times[0] if opening_times else None,
               "opening_window_last": opening_times[-1] if opening_times else None,
               "counts": {key: counts[key] for key in keys},
               "missing_fields": dict(sorted(missing.items())),
               "acts_direction_split": dict(sorted(acts_split.items())),
               "acts_decision_regime_split": dict(sorted(decision_split.items())),
               "trend_pullback_direction_split": dict(sorted(trend_split.items())),
               "alpha_status_split": dict(sorted(alpha_split.items())),
               "strongest_candidates": strongest}
    return summary, details


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-dir", required=True, type=Path,
                        help="existing replay period directory with contexts/decisions JSONL")
    parser.add_argument("--output", type=Path,
                        help="new diagnostic directory; source replay remains untouched")
    args = parser.parse_args()
    summary, details = diagnose(_rows(args.lab_dir / "contexts.jsonl"),
                                _rows(args.lab_dir / "decisions.jsonl"))
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
        with (args.output / "observations.jsonl").open("x", encoding="utf-8") as target:
            for detail in details:
                target.write(json.dumps(detail, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
