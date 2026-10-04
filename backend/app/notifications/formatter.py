from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.notifications.models import NotificationPriority

IST = ZoneInfo("Asia/Kolkata")


def _number(value: Any, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    number = float(value)
    return f"{number:,.{digits}f}".rstrip("0").rstrip(".")


def _money(value: Any) -> str:
    if value is None:
        return "N/A"
    number = float(value)
    return f"{'+' if number > 0 else ''}₹{number:,.2f}"


def _time(value: datetime | str | None) -> str:
    if value is None:
        return "N/A"
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST).strftime("%H:%M IST")


def _strategy(value: str) -> str:
    return value.replace("_", " ").title()


def _message(lines: list[str]) -> str:
    return "\n".join(lines)[:4096]


def format_operational_alert(
    component: str, status: str, issue: str, timestamp: datetime,
    priority: NotificationPriority, *, recovered: bool = False,
    snapshot_id: int | None = None,
) -> str:
    headline = "SYSTEM RECOVERED" if recovered else "SYSTEM ALERT"
    lines = [headline, "", f"Priority: {priority.value}", f"Component: {component}",
             f"Status: {status}", f"Issue: {issue}"]
    if snapshot_id is not None:
        lines.append(f"Snapshot: {snapshot_id}")
    lines.append(f"Time: {_time(timestamp)}")
    return _message(lines)


def format_regime(
    regime: dict[str, Any], feature: dict[str, Any], *, changed: bool,
    alpha: dict[str, Any] | None = None,
) -> str:
    support = feature.get("support_resistance", {}).get("potential_support_clusters", [])
    resistance = feature.get("support_resistance", {}).get("potential_resistance_clusters", [])
    lines = [
        "NIFTY REGIME CHANGED" if changed else "NIFTY REGIME CONFIRMED", "",
        f"Direction: {regime['regime']}", f"Confidence: {_number(regime.get('confidence'), 0)}",
        f"Evidence: {regime.get('evidence_quality', 'N/A')}",
        f"Spot: {_number(feature.get('spot'), 0)}",
        f"VIX: {_number(feature.get('volatility_features', {}).get('india_vix'))}",
        f"Support: {_number(support[0].get('center_strike'), 0) if support else 'N/A'}",
        f"Resistance: {_number(resistance[0].get('center_strike'), 0) if resistance else 'N/A'}",
        f"Time: {_time(regime.get('timestamp'))}",
    ]
    if alpha and alpha.get("confirmed"):
        lines.extend([
            f"Alpha1: {_number(alpha.get('alpha_1'), 2)}",
            f"Alpha2: {_number(alpha.get('alpha_2'), 2)}",
            f"Alpha Confirmation: {alpha.get('consecutive_confirmation_count', 0)}",
        ])
    lines.extend(["", "No real order has been placed."])
    return _message(lines)


def format_candidate(candidate: dict[str, Any]) -> str:
    pricing = candidate.get("pricing_basis", "N/A")
    if pricing == "LTP_ESTIMATE":
        pricing = "LTP ESTIMATE — not executable quote"
    return _message([
        "CREDIT SPREAD CANDIDATE", "", _strategy(candidate["strategy_type"]),
        f"Short: {_number(candidate['short_leg']['strike'], 0)} {candidate['short_leg']['option_type']}",
        f"Long: {_number(candidate['long_leg']['strike'], 0)} {candidate['long_leg']['option_type']}",
        f"Width: {_number(candidate.get('spread_width'), 0)}",
        f"Estimated credit: {_number(candidate.get('net_credit'))}", f"Pricing: {pricing}",
        f"Candidate score: {_number(candidate.get('selection_score'), 0)}",
        f"Spot: {_number(candidate.get('spot'), 0)}", "", "Status: Awaiting risk approval",
    ])


def format_risk(decision: dict[str, Any], regime: dict[str, Any] | None, *, approved: bool) -> str:
    if not approved:
        return _message([
            "IMPORTANT RISK REJECTION", "", f"Strategy: {_strategy(decision['candidate_strategy'])}",
            f"Reason: {', '.join(decision.get('failed_checks') or ['N/A'])}",
            "", "Research decision only. No real order has been placed.",
        ])
    source = decision.get("source_candidate") or {}
    return _message([
        "RISK APPROVED — SHADOW ONLY", "", f"Strategy: {_strategy(decision['candidate_strategy'])}",
        f"Short: {_number(source.get('short_leg', {}).get('strike'), 0)} {source.get('short_leg', {}).get('option_type', '')}".rstrip(),
        f"Long: {_number(source.get('long_leg', {}).get('strike'), 0)} {source.get('long_leg', {}).get('option_type', '')}".rstrip(),
        f"Max Profit/Lot: {_money(decision.get('max_profit_per_lot'))}",
        f"Max Loss/Lot: {_money(decision.get('max_loss_per_lot'))}",
        f"Credit/Width: {_number(decision.get('credit_to_width_ratio'), 3)}",
        f"Regime: {regime.get('regime', 'N/A') if regime else 'N/A'}",
        f"Confidence: {_number(regime.get('confidence'), 0) if regime else 'N/A'}", "",
        "This is a shadow-research approval.", "NO REAL ORDER HAS BEEN PLACED",
    ])


def format_shadow_entry(trade: dict[str, Any]) -> str:
    return _message([
        "SHADOW TRADE OPENED", "", _strategy(trade["strategy_type"]),
        f"Short: {_number(trade['short_leg']['strike'], 0)} {trade['short_leg']['option_type']}",
        f"Long: {_number(trade['long_leg']['strike'], 0)} {trade['long_leg']['option_type']}",
        f"Entry Credit: {_number(trade.get('entry_credit'))}",
        f"Pricing: {str(trade.get('entry_pricing_basis', 'N/A')).replace('_', ' ')}",
        f"Spot: {_number(trade.get('entry_spot'), 0)}",
        f"Max Profit/Lot: {_money(trade.get('max_profit_per_lot'))}",
        f"Max Loss/Lot: {_money(trade.get('max_loss_per_lot'))}",
        f"Entry Time: {_time(trade.get('entry_timestamp'))}", "",
        "Research simulation only.", "No broker order placed.",
    ])


def format_shadow_exit(trade: dict[str, Any]) -> str:
    return _message([
        "SHADOW TRADE CLOSED", "", _strategy(trade["strategy_type"]),
        f"{_number(trade['short_leg']['strike'], 0)} / {_number(trade['long_leg']['strike'], 0)} {trade['short_leg']['option_type']}",
        f"Exit Reason: {trade.get('exit_reason') or 'N/A'}",
        f"Entry Credit: {_number(trade.get('entry_credit'))}",
        f"Exit Debit: {_number(trade.get('exit_debit'))}",
        f"P&L/Lot: {_money(trade.get('realized_pnl_per_lot'))}",
        f"MFE: {_money(trade.get('mfe_per_lot'))}", f"MAE: {_money(trade.get('mae_per_lot'))}",
        f"Holding: {_number(trade.get('holding_minutes'), 0)} min", "",
        "No real trade was executed.",
    ])


def format_daily_summary(research: dict[str, Any], operations: dict[str, Any]) -> str:
    regimes = research.get("regime_counts", {})
    return _message([
        "DAILY RESEARCH & OPERATIONS SUMMARY", str(research["date"]), "",
        f"Snapshots: {research.get('snapshots_collected', 0)}",
        f"Usable OI: {research.get('snapshots_with_usable_oi', 0)}", "",
        "Regimes:", f"Bullish: {regimes.get('BULLISH', 0)}",
        f"Bearish: {regimes.get('BEARISH', 0)}", f"Range: {regimes.get('RANGE', 0)}",
        f"No Trade: {regimes.get('NO_TRADE', 0)}", "",
        f"Candidates: {research.get('candidate_sets_created', 0)}",
        f"Risk Approved: {research.get('approved_risk_decisions', 0)}",
        f"Shadow Entries/Exits: {research.get('shadow_entries', 0)} / {research.get('shadow_exits', 0)}",
        f"Shadow P&L: {_money(research.get('shadow_realized_pnl'))}",
        f"AI Calls/Cost: {research.get('ai_calls', 0)} / ${float(research.get('ai_cost', 0)):.4f}", "",
        f"Collector success/failure: {operations['collector']['success']} / {operations['collector']['failure']}",
        f"Pipeline success/partial/failure: {operations['pipeline']['success']} / {operations['pipeline']['partial']} / {operations['pipeline']['failure']}",
        f"Warnings/errors/critical: {operations['operational_events']['WARN']} / {operations['operational_events']['ERROR']} / {operations['operational_events']['CRITICAL']}",
    ])
