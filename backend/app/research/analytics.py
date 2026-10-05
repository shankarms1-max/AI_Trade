"""Explicit denominators, units and separated evidence cohorts."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo
from app.research.features import feature_row_aligned

IST = ZoneInfo("Asia/Kolkata")


def path_coverage(rows, sessions, config):
    slots = []
    for day in sorted(sessions):
        start = datetime.combine(datetime.fromisoformat(day).date(), config.session_start, IST)
        end = datetime.combine(start.date(), config.session_end, IST)
        while start <= end:
            slots.append(start)
            start += timedelta(seconds=config.expected_interval_seconds)
    observed, usable = set(), set()
    duplicates, off_cadence, missing_features = 0, 0, 0
    for row in rows:
        at = row.snapshot.timestamp_ist
        if at.date().isoformat() not in sessions:
            continue
        closest = min(slots, key=lambda slot: abs((at-slot).total_seconds())) if slots else None
        if closest is None or abs((at-closest).total_seconds()) > config.interval_tolerance_seconds:
            off_cadence += 1
            continue
        if closest in observed:
            duplicates += 1
        observed.add(closest)
        if not feature_row_aligned(row):
            missing_features += 1
        else:
            usable.add(closest)
    return {"expected_observations": len(slots), "observed_observations": len(observed),
            "missing_observations": len(slots)-len(observed), "usable_observations": len(usable),
            "missing_feature_observations": missing_features, "duplicate_observations": duplicates,
            "off_cadence_observations": off_cadence,
            "path_coverage_ratio": len(usable)/len(slots) if slots else None}


def drawdown(values):
    equity = peak = worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak-equity)
    return worst


def sample_label(trades, sessions):
    return ("INSUFFICIENT" if trades < 30 else "EARLY" if trades < 100 else
            "MODERATE" if trades < 500 or sessions < 20 else "LARGE_SAMPLE_DIVERSE_SESSIONS")


def replay_analytics(attempts, events, coverage, marked_equity):
    filled = [item for item in attempts if item.get("entry_execution")]
    closed = [item for item in filled if item.get("final_evaluability") == "EVALUABLE"]
    primary_closed = [item for item in closed if item["depth_status"] == "EXECUTABLE_DEPTH"]
    unresolved = [item for item in filled if item.get("state") == "UNRESOLVED_EXPOSURE"]
    primary_net = [item for item in closed if item["final_outcome"]["cost_completeness"] == "NET_COMPLETE"
                   and item["depth_status"] == "EXECUTABLE_DEPTH"]
    points = [item["final_outcome"]["gross_points_per_unit"] for item in primary_closed]
    gross = [item["final_outcome"]["gross_rupees"] for item in primary_closed]
    net = [item["final_outcome"]["net_rupees"] for item in primary_net]
    counts = {
        "total_decisions": len(attempts), "approved_decisions": sum(item["approved"] for item in attempts),
        "fill_attempts": sum(event["event_type"] == "FILL_ATTEMPT" for event in events),
        "filled_trades": len(filled), "closed_evaluable_trades": len(closed),
        "unresolved": len(unresolved), "no_fill": sum(item["state"] == "NO_FILL" for item in attempts),
        "invalid_fill": sum(item["state"] == "INVALID_FILL" for item in attempts),
        "missing_path": sum(item.get("final_evaluability") == "PARTIAL_PATH" for item in attempts),
        "gross_only": sum(item["final_outcome"]["cost_completeness"] == "GROSS_ONLY" for item in closed),
        "net_complete": len(primary_net), "blocked_incomplete_state": sum(item["state"] == "BLOCKED_INCOMPLETE_RISK_STATE" for item in attempts),
        "closed_unknown_depth_simulations": len(closed)-len(primary_closed),
    }
    axes = ("strategy_family", "directional_strength", "survival_bucket", "carry_bucket", "dte_bucket",
            "spread_width", "move_distance_bucket", "expiry_stress_state", "move_source", "depth_status", "cost_completeness")
    breakdown = {}
    for axis in axes:
        groups = defaultdict(list)
        for item in filled:
            groups[str(item.get(axis, "UNAVAILABLE"))].append(item)
        breakdown[axis] = {}
        for name, group in groups.items():
            outcomes = [item["final_outcome"] for item in group if item.get("final_evaluability") == "EVALUABLE"
                        and item["depth_status"] == "EXECUTABLE_DEPTH"]
            completed_net = [out["net_rupees"] for item in group if item.get("final_evaluability") == "EVALUABLE"
                             for out in [item["final_outcome"]] if out["net_rupees"] is not None
                             and item["depth_status"] == "EXECUTABLE_DEPTH"]
            breakdown[axis][name] = {"filled": len(group), "closed": len(outcomes),
                                    "unresolved": sum(item["state"] == "UNRESOLVED_EXPOSURE" for item in group),
                                    "gross_rupees": sum(out["gross_rupees"] for out in outcomes) if outcomes else None,
                                    "net_complete_count": len(completed_net),
                                    "net_rupees": sum(completed_net) if completed_net else None}
    cohorts = defaultdict(list)
    for item in filled:
        cohorts[f"{item['move_source']}|{item['depth_status']}|{item['cost_completeness']}"].append(item)
    cohort_metrics = {}
    for key, group in sorted(cohorts.items()):
        outcomes = [item["final_outcome"] for item in group if item.get("final_evaluability") == "EVALUABLE"]
        nets = [out["net_rupees"] for out in outcomes if out["net_rupees"] is not None]
        cohort_metrics[key] = {"filled": len(group), "closed": len(outcomes),
            "unresolved": sum(item["state"] == "UNRESOLVED_EXPOSURE" for item in group),
            "gross_points_per_unit_sum": sum(out["gross_points_per_unit"] for out in outcomes) if outcomes else None,
            "gross_rupees_for_quantity": sum(out["gross_rupees"] for out in outcomes) if outcomes else None,
            "net_rupees_for_quantity": sum(nets) if nets else None,
            "primary_net_evidence_eligible": all(item["depth_status"] == "EXECUTABLE_DEPTH"
                and item["cost_completeness"] == "NET_COMPLETE" for item in group)}
    sessions = len({item["entry_at"].date() for item in closed})
    net_sessions = len({item["entry_at"].date() for item in primary_net})
    return {"status": "PARTIAL_PATH" if unresolved or coverage["missing_observations"] or coverage["missing_feature_observations"]
            else "EVALUABLE" if closed else "NOT_EVALUABLE",
            **coverage, "denominators": counts, "closed_trade_sessions": sessions,
            "net_complete_sessions": net_sessions, "sample_label": sample_label(len(primary_net), net_sessions),
            "gross_points_per_unit_sum": sum(points) if points else None,
            "gross_rupees_for_quantity": sum(gross) if gross else None,
            "gross_rupees_per_lot_sum": sum(item["final_outcome"]["gross_rupees_per_lot"] for item in primary_closed) if primary_closed else None,
            "total_cost_rupees_for_quantity": sum(item["final_outcome"]["total_cost_rupees"] for item in primary_net) if primary_net else None,
            "net_rupees_for_quantity": sum(net) if net else None,
            "net_points_per_unit_sum": sum(item["final_outcome"]["net_points_per_unit"] for item in primary_net) if primary_net else None,
            "net_expectancy_rupees_for_quantity": mean(net) if net else None,
            "closed_trade_cumulative_gross_drawdown_rupees": drawdown(gross) if gross else None,
            "closed_trade_cumulative_net_drawdown_rupees": drawdown(net) if net else None,
            "diagnostic_all_depth_sampled_marked_equity_gross_drawdown_rupees":
                None if unresolved else max((max(marked_equity[:i+1])-value for i, value in enumerate(marked_equity)), default=None),
            "sampled_marked_equity_evaluability": "PARTIAL_PATH" if unresolved else "SAMPLED_OBSERVED_PATH",
            "sampled_MAE_points_per_unit": mean(item["sampled_MAE_points_per_unit"] for item in primary_closed) if primary_closed else None,
            "sampled_MFE_points_per_unit": mean(item["sampled_MFE_points_per_unit"] for item in primary_closed) if primary_closed else None,
            "exit_reason_counts": dict(Counter(item["exit_reason"] for item in closed)),
            "evaluability_rate": len(closed)/len(attempts) if attempts else None,
            "net_evidence_cohort": "EXECUTABLE_DEPTH|NET_COMPLETE",
            "breakdowns": breakdown, "cohorts": cohort_metrics,
            "sampled_extrema_warning": "Sampled path extrema include zero entry baseline; intrainterval extremes may be missed",
            "authoritative_engine": "chronological replay_parameters / reconstructed candidate path",
            "entry_fill_method": "NEXT_OBSERVATION_SIMULATION",
            "exit_fill_method": "CONTEMPORANEOUS_OBSERVED_BOOK_LIQUIDATION",
            "legacy_shadow_evidence_equivalent": False, "profitability_claim": False}
