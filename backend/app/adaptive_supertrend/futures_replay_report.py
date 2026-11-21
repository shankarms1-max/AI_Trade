"""Closed-trade statistics and execution evidence for the offline futures replay."""

from collections import Counter
from datetime import datetime
from decimal import Decimal
import json

from app.adaptive_supertrend.models import AstTrade
from app.adaptive_supertrend.optimizer import _quality, _segments, signal_trades
from app.adaptive_supertrend.indicators import supertrend

SCOPE_NOT_COMPARABLE = "DIFFERENT_COHORT_NOT_DIRECTLY_COMPARABLE"
# Context only. These aggregates come from walk-forward evaluations over date cohorts that differ from any replay.
# Nothing here is a benchmark and nothing is ever subtracted from, or compared with, a replay total.
HISTORICAL_CONTEXT = {
    "frozen_quality_heavy_oos": {
        "trades": 11,
        "gross_points": 485.7,
        "expectancy_points": 44.15,
        "profit_factor": 4.51,
        "max_drawdown_points": 92.9,
        "sharpe_equivalent": 1.69,
        "scope_status": SCOPE_NOT_COMPARABLE,
    },
    "combined_policy_selection_path": {
        "path": "COMBINED_POLICY_SELECTION_PATH",
        "trades": 14,
        "gross_points": 395.6,
        "profit_factor": 2.56,
        "max_drawdown_points": 119.4,
        "scope_status": SCOPE_NOT_COMPARABLE,
        "note": "Result of the historical policy-selection path, not the frozen QUALITY_HEAVY policy's own OOS result.",
    },
}
DELTA_KEYS = ("expectancy_points", "max_drawdown_points")


def trade_result(row: AstTrade) -> dict:
    doc = json.loads(row.document)
    parameters = doc["session_parameters"]
    return {
        "trading_date": str(row.trading_date),
        "state": row.state,
        "atr_period": parameters["atr_period"],
        "multiplier": parameters["multiplier_tenths"] / 10,
        "ast_transition": doc["strategy_features"]["ast_transition"],
        "decision_price": doc["strategy_features"]["current_future_price"],
        "opening_high": doc["strategy_features"]["opening_high"],
        "opening_low": doc["strategy_features"]["opening_low"],
        **{
            key: doc.get(key)
            for key in (
                "trade_id",
                "opportunity_id",
                "side",
                "future_instrument_id",
                "future_expiry",
                "lot_size",
                "decision_timestamp",
                "entry_timestamp",
                "entry_price",
                "exit_timestamp",
                "exit_price",
                "exit_reason",
                "rejection_reason",
                "unresolved_reason",
                "gross_points",
                "mae_points",
                "mfe_points",
            )
        },
        "gross_inr": doc.get("gross_pnl"),
        "net_inr": doc.get("net_pnl"),
    }


def drawdown(values):
    equity = high = worst = Decimal(0)
    for value in values:
        equity += value
        high = max(high, equity)
        worst = max(worst, high - equity)
    return float(worst)


def metrics(points: list[Decimal]) -> dict:
    gains, losses = [x for x in points if x > 0], [x for x in points if x < 0]
    return {
        "trades": len(points),
        "wins": len(gains),
        "losses": len(losses),
        "gross_points": float(sum(points)),
        "expectancy_points": float(sum(points) / len(points)) if points else None,
        "profit_factor": float(sum(gains) / -sum(losses)) if losses else None,
        "win_rate": len(gains) / len(points) if points else None,
        "max_drawdown_points": drawdown(points),
        "average_win_points": float(sum(gains) / len(gains)) if gains else None,
        "average_loss_points": float(sum(losses) / len(losses)) if losses else None,
    }


def signal_summary(signals: list[dict]) -> dict:
    """Same closed-trade metrics for the five-minute signal counterpart, in exit order."""
    ordered = sorted(signals, key=lambda t: datetime.fromisoformat(t["exit_timestamp"]))
    return metrics([Decimal(str(t["gross_points"])) for t in ordered])


def summary(trades: list[dict], opportunities: int) -> dict:
    closed = sorted((t for t in trades if t["state"] == "CLOSED"), key=lambda t: t["exit_timestamp"])
    points = [Decimal(str(t["gross_points"])) for t in closed]
    rupees = [Decimal(str(t["gross_inr"])) for t in closed]
    counts = Counter(t["state"] for t in trades)
    return {
        "opportunities": opportunities,
        "pending_entries_created": len(trades),
        "pending_entries_at_eof": counts["PENDING_PAPER_ENTRY"],
        "filled_trades": sum(t["entry_timestamp"] is not None for t in trades),
        "rejected_entries": counts["ENTRY_REJECTED"],
        "entry_rejection_reasons": dict(
            Counter(t["rejection_reason"] for t in trades if t["state"] == "ENTRY_REJECTED")
        ),
        "closed_trades": len(closed),
        "unresolved_trades": counts["UNRESOLVED_EXPOSURE"],
        "open_at_eof": counts["OPEN"],
        "pending_exits_at_eof": counts["PENDING_PAPER_EXIT"],
        "unpriced_filled_trades": sum(counts[s] for s in ("OPEN", "PENDING_PAPER_EXIT", "UNRESOLVED_EXPOSURE")),
        **{k: v for k, v in metrics(points).items() if k != "trades"},
        "flat": points.count(Decimal(0)),
        "gross_inr": float(sum(rupees)),
        "max_drawdown_inr": drawdown(rupees),
        "exit_reason_counts": dict(Counter(t["exit_reason"] for t in closed)),
    }


def signal_counterpart(csv_prefix, day, parameters, policy):
    """Existing bar-open signal accounting, using the day's already-selected parameters."""
    result = []
    for segment in _segments(csv_prefix):
        if segment[-1].start.date() < day:
            continue
        directions = supertrend(segment, parameters["atr_period"], parameters["multiplier_tenths"])
        for trade in signal_trades(segment, directions, _quality(segment, policy)):
            if trade["date"] == str(day):
                result.append(
                    {
                        "trading_date": str(day),
                        "side": "LONG" if trade["sign"] == 1 else "SHORT",
                        "ast_transition": trade["key"][2],
                        "future_instrument_id": segment[0].future_id,
                        "future_expiry": str(segment[0].future_expiry),
                        "entry_timestamp": trade["entry_at"],
                        "entry_price": trade["price"],
                        "exit_timestamp": trade["exit_at"],
                        "gross_points": trade["signal_points"],
                    }
                )
    return result


def date_scope(replay_dates, eligible_dates, compared_dates, trades):
    """Dates seen by each side. Aggregates are comparable only when both sides evaluated identical sessions."""
    eligible, compared = set(eligible_dates), set(compared_dates)
    trade_dates = {t["trading_date"] for t in trades}
    unmatched_paper = sorted(eligible - compared)
    unmatched_5m = sorted(compared - eligible)
    outside = sorted(trade_dates - compared)
    if not eligible and not compared:
        status = "UNAVAILABLE_NO_COMPARABLE_SESSIONS"
    elif unmatched_paper or unmatched_5m or outside:
        status = "BLOCKED_DATE_SCOPE_MISMATCH"
    else:
        status = "IDENTICAL_DATE_SCOPE"
    return {
        "replay_session_dates": sorted(replay_dates),
        "paper_replay_dates": sorted(eligible),
        "same_session_5m_dates": sorted(compared),
        "paper_trade_dates": sorted(trade_dates),
        "paper_dates_without_5m_comparison": unmatched_paper,
        "same_session_5m_dates_outside_paper_scope": unmatched_5m,
        "paper_trade_dates_outside_5m_scope": outside,
        "direct_comparison_status": status,
    }


def comparison(totals, signals, trades, replay_dates, eligible_dates, compared_dates):
    scope = date_scope(replay_dates, eligible_dates, compared_dates, trades)
    available = scope["direct_comparison_status"] == "IDENTICAL_DATE_SCOPE"
    five_minute = signal_summary(signals)
    paper_metrics = {k: totals["closed_trades" if k == "trades" else k] for k in five_minute}
    delta = None
    if available:
        delta = {"trade_count": paper_metrics["trades"] - five_minute["trades"]}
        delta["gross_points"] = paper_metrics["gross_points"] - five_minute["gross_points"]
        for name in DELTA_KEYS:
            a, b = paper_metrics[name], five_minute[name]
            delta[name] = a - b if a is not None and b is not None else None

    def key(t):
        return t["trading_date"], t["side"], t["ast_transition"], t["future_instrument_id"], t["future_expiry"]

    filled = {key(t): t for t in trades if t["entry_timestamp"]}
    signal_keys = {key(t) for t in signals}
    paired = []
    matched_count = 0
    entry_effect = exit_effect = 0.0
    signal_only_points = 0.0
    for signal in signals:
        paper = filled.get(key(signal))
        record = {"signal": signal, "paper_trade_id": paper["trade_id"] if paper else None}
        if paper:
            sign = 1 if paper["side"] == "LONG" else -1
            record["entry_time_delta_seconds"] = (
                datetime.fromisoformat(paper["entry_timestamp"]) - datetime.fromisoformat(signal["entry_timestamp"])
            ).total_seconds()
            record["entry_effect_points"] = sign * (signal["entry_price"] - paper["entry_price"])
            if paper["state"] == "CLOSED":
                difference = paper["gross_points"] - signal["gross_points"]
                record["exit_effect_points"] = difference - record["entry_effect_points"]
                matched_count += 1
                entry_effect += record["entry_effect_points"]
                exit_effect += record["exit_effect_points"]
        if paper is None or paper["state"] != "CLOSED":
            signal_only_points += signal["gross_points"]
        paired.append(record)
    paper_only = [t for k, t in filled.items() if k not in signal_keys and t["state"] == "CLOSED"]
    return {
        "direct_comparison_basis": "SAME_SESSION_5M_SIGNAL_COUNTERPART",
        "date_scope": scope,
        "paper_replay_summary": paper_metrics,
        "same_session_5m_summary": five_minute,
        "paper_minus_same_session_5m": delta,
        "paper_minus_same_session_5m_status": scope["direct_comparison_status"],
        "historical_context": HISTORICAL_CONTEXT,
        "historical_context_note": "Context only, from different walk-forward cohorts. Never subtracted from or "
        "compared arithmetically with replay results.",
        "same_session_5m_closed_signal_trades": paired,
        "closed_trade_difference_attribution": {
            "both_closed": matched_count,
            "closed_only_in_5m": len(signals) - matched_count,
            "closed_only_in_paper": len(paper_only),
            "matched_entry_effect_points": entry_effect,
            "matched_exit_effect_points": exit_effect,
            "unmatched_paper_points": sum(t["gross_points"] for t in paper_only),
            "unmatched_5m_points": signal_only_points,
        },
        "same_session_5m_gross_points": sum(t["gross_points"] for t in signals),
        "paper_fills_without_matching_closed_5m_signal": [
            t["trade_id"] for k, t in filled.items() if k not in signal_keys
        ],
        "match_basis": "Same session, side, futures identity and completed SuperTrend transition. "
        "Prices and timestamps are independent.",
        "execution_basis": "5m signals use completed-bar breakouts and next-bar opens. Paper tests the observed "
        "intra-bar futures LTP and rechecks the opportunity at the next observation. Session "
        "coverage, identity, lot and gap evidence are reported by date. Unpriced positions "
        "carry forward under paper rules and contribute no realized P&L.",
    }
