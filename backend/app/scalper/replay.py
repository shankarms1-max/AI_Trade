"""Deterministic, read-only replay of persisted Phase 15 observations."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.research.manifest import canonical, digest, implementation_hash, json_value
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.scalper import SCALPER_VERSION
from app.scalper.config import ScalperConfig, parse_widths
from app.scalper.features import build_features
from app.scalper.models import (
    ScalperCandidate,
    ScalperFeatures,
    ScalperMarketSnapshot,
    ScalperMarketSnapshotRecord,
    ScalperSignal,
    ScalperTrade,
)
from app.scalper.paper import ExecutionPair, executable_pair, exit_trigger
from app.scalper.repository import ScalperRepository
from app.scalper.risk import ScalperRiskState, evaluate_entry
from app.scalper.signals import build_signal
from app.scalper.strategy import build_candidates, validate_book

IST = ZoneInfo("Asia/Kolkata")
REPLAY_VERSION = "phase15_1_v1"
EXECUTION_MODEL = "NEXT_OBSERVATION_BID_ASK"
ACTIVE_STATES = {"PENDING_ENTRY", "OPEN", "UNRESOLVED"}

OVERRIDE_FIELDS = {
    "SCALPER_SIGNAL_MIN_SCORE": "signal_min_score",
    "SCALPER_MIN_CONFIRMATIONS": "min_confirmations",
    "SCALPER_ALLOWED_WIDTHS": "allowed_widths",
    "SCALPER_PROFIT_CAPTURE_PCT": "profit_capture_pct",
    "SCALPER_STOP_CREDIT_MULTIPLE": "stop_credit_multiple",
    "SCALPER_TRAILING_ACTIVATION_PCT": "trailing_activation_pct",
    "SCALPER_TRAILING_GIVEBACK_PCT": "trailing_giveback_pct",
    "SCALPER_TIME_STOP_MINUTES": "time_stop_minutes",
}

TRADE_FIELDS = (
    "trade_id", "state", "date", "direction", "strategy", "short_strike",
    "long_strike", "width", "decision_timestamp", "entry_timestamp",
    "entry_spot", "entry_credit", "max_defined_loss", "exit_timestamp",
    "exit_debit", "gross_points", "gross_rupees", "net_rupees",
    "accounting_status", "holding_seconds", "holding_minutes", "exit_reason",
    "rejection_reason", "entry_score", "confirmation_count",
    "signal_component_scores", "candidate_rank", "mae", "mfe",
    "max_spread_debit", "minimum_spread_debit", "dte", "vix_regime",
)

QUALITY_FIELDS = (
    "snapshot_id", "captured_at", "status", "fatal_reasons", "diagnostics",
    "cadence_gap_seconds", "quote_count", "valid_book_count",
    "source_timestamp_count", "missing_source_timestamp_count",
    "depth_units", "book_rejections",
)

SIGNAL_FIELDS = (
    "snapshot_id", "timestamp", "direction", "score", "strength",
    "confirmation_count", "confirmed", "momentum", "structure", "futures",
    "participation", "execution", "contradiction_penalty", "reasons",
    "warnings", "candidate_count", "candidate_widths", "decision",
    "LIVE_STORED_VS_REPLAY_FEATURE_MATCH",
    "LIVE_STORED_VS_REPLAY_SIGNAL_MATCH",
    "LIVE_STORED_VS_REPLAY_CANDIDATE_PRESENCE_MATCH",
    "LIVE_STORED_VS_REPLAY_TRADE_DECISION_MATCH",
)


@dataclass(frozen=True)
class ReplayRow:
    snapshot_id: int
    snapshot: ScalperMarketSnapshot
    stored_feature_json: str | None = None
    stored_signal_json: str | None = None
    context: dict[str, Any] | None = None
    capture_key: str | None = None
    stored_candidate_present: bool | None = None
    stored_decision: str | None = None


@dataclass
class ReplayTrade:
    trade_id: str
    state: str
    candidate: ScalperCandidate
    candidate_rank: int
    document: dict[str, Any]
    mae: float = 0.0
    mfe: float = 0.0
    max_spread_debit: float | None = None
    minimum_spread_debit: float | None = None


@dataclass(frozen=True)
class ReplayResult:
    manifest: dict[str, Any]
    summary: dict[str, Any]
    trades: tuple[dict[str, Any], ...]
    observation_quality: tuple[dict[str, Any], ...]
    signal_timeline: tuple[dict[str, Any], ...]
    daily_summary: tuple[dict[str, Any], ...]


@dataclass
class ReplayCounters:
    signals: int = 0
    confirmed_signals: int = 0
    candidates: int = 0
    risk_approved_entries: int = 0
    entry_rejections: int = 0
    executed_trades: int = 0


def apply_replay_overrides(
    base: ScalperConfig,
    overrides: dict[str, Any] | None,
) -> tuple[ScalperConfig, dict[str, Any]]:
    """Apply allowlisted research overrides without mutating application defaults."""
    supplied = dict(overrides or {})
    unknown = set(supplied) - set(OVERRIDE_FIELDS)
    if unknown:
        raise ValueError(f"SCALPER_REPLAY_OVERRIDE_NOT_ALLOWED:{','.join(sorted(unknown))}")
    normalized: dict[str, Any] = {}
    for external, value in supplied.items():
        if external == "SCALPER_ALLOWED_WIDTHS":
            value = parse_widths(value if isinstance(value, str)
                                 else ",".join(str(item) for item in value))
        normalized[OVERRIDE_FIELDS[external]] = value

    candidate = replace(base, **normalized)
    if not 0 <= candidate.signal_min_score <= 100:
        raise ValueError("SCALPER_SIGNAL_MIN_SCORE_INVALID")
    if not 1 <= candidate.min_confirmations <= 20:
        raise ValueError("SCALPER_MIN_CONFIRMATIONS_INVALID")
    if not 0 < candidate.profit_capture_pct <= 100:
        raise ValueError("SCALPER_PROFIT_CAPTURE_PCT_INVALID")
    if candidate.stop_credit_multiple <= 1:
        raise ValueError("SCALPER_STOP_CREDIT_MULTIPLE_INVALID")
    if not 0 < candidate.trailing_activation_pct <= 100:
        raise ValueError("SCALPER_TRAILING_ACTIVATION_PCT_INVALID")
    if not 0 < candidate.trailing_giveback_pct <= 100:
        raise ValueError("SCALPER_TRAILING_GIVEBACK_PCT_INVALID")
    if not 1 <= candidate.time_stop_minutes <= 180:
        raise ValueError("SCALPER_TIME_STOP_MINUTES_INVALID")
    return candidate, {key: json_value(value) for key, value in sorted(supplied.items())}


def _policy(config: ScalperConfig, costs: Any) -> dict[str, Any]:
    """Match the live Phase 15 policy document exactly for hash comparison."""
    return json_value({
        "version": SCALPER_VERSION,
        "config": config.payload(),
        "costs": costs.payload(),
        "execution_mode": "PAPER",
        "risk_accounting_basis": "GROSS_REALIZED",
        "profitability_claim": False,
    })


def _vix_regime(value: float | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    if value < 12:
        return "LOW"
    if value < 20:
        return "NORMAL"
    if value < 30:
        return "ELEVATED"
    return "HIGH"


def _score_bucket(score: float) -> str:
    if score < 70:
        return "BELOW_70"
    if score < 75:
        return "70_74"
    if score < 80:
        return "75_79"
    if score < 85:
        return "80_84"
    return "85_100"


def _model_match(stored: str | None, replayed: Any, model_type: Any) -> bool | None:
    if not stored:
        return None
    try:
        expected = model_type.model_validate(json.loads(stored))
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
    return canonical(expected) == canonical(replayed)


def _quality_row(
    row: ReplayRow,
    config: ScalperConfig,
    *,
    duplicate_ids: set[int],
    duplicate_times: set[datetime],
    duplicate_keys: set[str],
    out_of_order_ids: set[int],
    previous_at: datetime | None,
) -> dict[str, Any]:
    snapshot = row.snapshot
    fatal: list[str] = []
    diagnostics: list[str] = []
    key = row.capture_key or digest(snapshot)
    if row.snapshot_id in duplicate_ids:
        fatal.append("DUPLICATE_SNAPSHOT_ID")
    if snapshot.captured_at in duplicate_times:
        fatal.append("DUPLICATE_OBSERVATION_TIMESTAMP")
    if key in duplicate_keys:
        fatal.append("DUPLICATE_OBSERVATION")
    if row.snapshot_id in out_of_order_ids:
        fatal.append("OUT_OF_ORDER_OBSERVATION")
    if snapshot.source_market_timestamp is None:
        diagnostics.append("MISSING_SNAPSHOT_SOURCE_TIMESTAMP")
    if snapshot.request_started_at > snapshot.response_received_at:
        fatal.append("INVALID_CAPTURE_TIMESTAMPS")

    gap = None if previous_at is None else (
        snapshot.captured_at - previous_at).total_seconds()
    if gap is not None and gap > config.interval_seconds * 1.5:
        diagnostics.append("CADENCE_GAP")

    rejection_counts: Counter[str] = Counter()
    depth_units: Counter[str] = Counter()
    valid_books = 0
    sources = 0
    for quote in snapshot.quotes:
        depth_units[quote.depth_unit] += 1
        sources += quote.source_market_timestamp is not None
        result = validate_book(
            quote, snapshot.response_received_at, snapshot.lot_size, config)
        if result.valid:
            valid_books += 1
        else:
            rejection_counts[result.reason or "UNKNOWN_BOOK_FAILURE"] += 1
    if rejection_counts:
        diagnostics.extend(f"QUOTE_{reason}" for reason in sorted(rejection_counts))
    if not snapshot.quotes:
        fatal.append("NO_OPTION_QUOTES")

    return {
        "snapshot_id": row.snapshot_id,
        "captured_at": snapshot.captured_at.isoformat(),
        "status": "UNUSABLE" if fatal else "USABLE",
        "fatal_reasons": sorted(set(fatal)),
        "diagnostics": sorted(set(diagnostics)),
        "cadence_gap_seconds": gap,
        "quote_count": len(snapshot.quotes),
        "valid_book_count": valid_books,
        "source_timestamp_count": sources,
        "missing_source_timestamp_count": len(snapshot.quotes) - sources,
        "depth_units": dict(sorted(depth_units.items())),
        "book_rejections": dict(sorted(rejection_counts.items())),
    }


def _risk_state(trades: list[ReplayTrade], day: date) -> ScalperRiskState:
    today = [trade for trade in trades
             if trade.document["decision_timestamp"][:10] == str(day)]
    closed = [trade for trade in today if trade.state == "CLOSED"]
    ordered_closed = sorted(closed, key=lambda item: item.document["exit_timestamp"])
    consecutive = 0
    for trade in reversed(ordered_closed):
        if trade.document["final_outcome"]["gross_rupees"] >= 0:
            break
        consecutive += 1
    exits = [datetime.fromisoformat(item.document["exit_timestamp"])
             for item in closed]
    losses = [datetime.fromisoformat(item.document["exit_timestamp"])
              for item in closed
              if item.document["final_outcome"]["gross_rupees"] < 0]
    return ScalperRiskState(
        open_positions=sum(item.state in {"PENDING_ENTRY", "OPEN"} for item in trades),
        unresolved_positions=sum(item.state == "UNRESOLVED" for item in trades),
        executed_trades_today=sum("entry_timestamp" in item.document for item in today),
        gross_realized_today=sum(
            item.document["final_outcome"]["gross_rupees"] for item in closed),
        consecutive_losses=consecutive,
        last_exit_at=max(exits) if exits else None,
        last_loss_at=max(losses) if losses else None,
    )


def _update_excursions(trade: ReplayTrade, debit: float) -> None:
    pnl = trade.document["entry_credit"] - debit
    trade.mae = min(trade.mae, pnl)
    trade.mfe = max(trade.mfe, pnl)
    trade.max_spread_debit = max(
        debit, trade.max_spread_debit if trade.max_spread_debit is not None else debit)
    trade.minimum_spread_debit = min(
        debit,
        trade.minimum_spread_debit if trade.minimum_spread_debit is not None else debit,
    )


def _reject_pending(trade: ReplayTrade, row: ReplayRow, reason: str) -> None:
    trade.state = "ENTRY_REJECTED"
    trade.document.update(
        rejection_reason=reason,
        attempted_execution_snapshot_id=row.snapshot_id,
    )


def _transition_trade(
    trade: ReplayTrade,
    row: ReplayRow,
    signal: ScalperSignal,
    config: ScalperConfig,
    costs: Any,
) -> str | None:
    if trade.state == "UNRESOLVED":
        return None
    snapshot = row.snapshot
    pair, failure = executable_pair(
        snapshot, trade.candidate, config, entry=trade.state == "PENDING_ENTRY")
    if trade.state == "PENDING_ENTRY":
        decision_at = datetime.fromisoformat(trade.document["decision_timestamp"])
        age = (snapshot.captured_at - decision_at).total_seconds()
        reason = None
        if (row.snapshot_id == trade.document["decision_snapshot_id"]
                or snapshot.captured_at <= decision_at):
            reason = "SAME_OBSERVATION_FILL_FORBIDDEN"
        elif age > config.entry_ttl_seconds:
            reason = "ENTRY_TTL_EXCEEDED"
        elif snapshot.captured_at.date() >= trade.candidate.short_leg.expiry:
            reason = "EXPIRY_DAY_ENTRY_FORBIDDEN"
        elif failure:
            reason = failure
        if reason:
            _reject_pending(trade, row, reason)
            return "REJECTED"
        assert pair is not None
        actual_loss = ((trade.candidate.spread_width - pair.value)
                       * snapshot.lot_size * config.lots)
        if pair.value < config.min_credit or actual_loss > config.max_loss_per_trade:
            _reject_pending(trade, row, "FILL_ECONOMICS_OUTSIDE_RISK")
            return "REJECTED"
        trade.state = "OPEN"
        trade.document.update(
            entry_credit=pair.value,
            entry_timestamp=snapshot.captured_at.isoformat(),
            execution_snapshot_id=row.snapshot_id,
            entry_spot=snapshot.nifty_spot,
            entry_quotes=pair.evidence,
            last_snapshot_id=row.snapshot_id,
            last_observation=snapshot.captured_at.isoformat(),
            current_debit=pair.value,
            best_gross_points=0.0,
        )
        trade.max_spread_debit = pair.value
        trade.minimum_spread_debit = pair.value
        return "EXECUTED"

    if failure:
        if snapshot.captured_at.time().replace(tzinfo=None) >= config.forced_exit_time:
            trade.state = "UNRESOLVED"
            trade.document.update(
                unresolved_reason=failure,
                last_snapshot_id=row.snapshot_id,
                last_observation=snapshot.captured_at.isoformat(),
            )
        return None

    assert isinstance(pair, ExecutionPair)
    pnl = trade.document["entry_credit"] - pair.value
    trade.document.update(
        current_debit=pair.value,
        gross_mark_points=pnl,
        gross_mark_rupees=pnl * snapshot.lot_size * config.lots,
        best_gross_points=max(trade.document.get("best_gross_points", 0), pnl),
        last_snapshot_id=row.snapshot_id,
        last_observation=snapshot.captured_at.isoformat(),
    )
    _update_excursions(trade, pair.value)
    trigger = exit_trigger(trade.document, snapshot, signal, pair.value, config)
    if trigger is None:
        return None
    outcome = costs.calculate(
        day=snapshot.captured_at.date(),
        lot_size=snapshot.lot_size,
        lots=config.lots,
        entry_short=trade.document["entry_quotes"]["short"]["fill_price"],
        entry_long=trade.document["entry_quotes"]["long"]["fill_price"],
        exit_short=pair.short_price,
        exit_long=pair.long_price,
    )
    trade.state = "CLOSED"
    trade.document.update(
        exit_snapshot_id=row.snapshot_id,
        exit_timestamp=snapshot.captured_at.isoformat(),
        exit_reason=trigger,
        exit_debit=pair.value,
        exit_quotes=pair.evidence,
        final_outcome=outcome,
        holding_seconds=(snapshot.captured_at - datetime.fromisoformat(
            trade.document["entry_timestamp"])).total_seconds(),
        profitability_claim=False,
    )
    return "CLOSED"


def _trade_output(trade: ReplayTrade) -> dict[str, Any]:
    doc = trade.document
    candidate = trade.candidate
    outcome = doc.get("final_outcome") or {}
    holding = doc.get("holding_seconds")
    return {
        "trade_id": trade.trade_id,
        "state": trade.state,
        "date": doc["decision_timestamp"][:10],
        "direction": candidate.direction.value,
        "strategy": candidate.strategy_type,
        "short_strike": candidate.short_leg.strike,
        "long_strike": candidate.long_leg.strike,
        "width": candidate.spread_width,
        "decision_timestamp": doc["decision_timestamp"],
        "entry_timestamp": doc.get("entry_timestamp"),
        "entry_spot": doc.get("entry_spot"),
        "entry_credit": doc.get("entry_credit"),
        "max_defined_loss": candidate.defined_max_loss_per_lot,
        "exit_timestamp": doc.get("exit_timestamp"),
        "exit_debit": doc.get("exit_debit"),
        "gross_points": outcome.get("gross_points_per_unit"),
        "gross_rupees": outcome.get("gross_rupees"),
        "net_rupees": outcome.get("net_rupees"),
        "accounting_status": outcome.get("cost_completeness", "GROSS_ONLY"),
        "holding_seconds": holding,
        "holding_minutes": None if holding is None else holding / 60.0,
        "exit_reason": doc.get("exit_reason"),
        "rejection_reason": doc.get("rejection_reason") or doc.get("unresolved_reason"),
        "entry_score": doc["signal"]["score"],
        "confirmation_count": doc["signal"]["confirmation_count"],
        "signal_component_scores": doc["signal"]["components"],
        "candidate_rank": trade.candidate_rank,
        "mae": trade.mae if "entry_timestamp" in doc else None,
        "mfe": trade.mfe if "entry_timestamp" in doc else None,
        "max_spread_debit": trade.max_spread_debit,
        "minimum_spread_debit": trade.minimum_spread_debit,
        "dte": doc["dte"],
        "vix_regime": doc["vix_regime"],
    }


def _breakdown(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[key])].append(row)
    result = {}
    for name, values in sorted(grouped.items()):
        pnls = [item["gross_rupees"] for item in values]
        result[name] = {
            "trades": len(values),
            "gross_pnl": sum(pnls),
            "average_pnl": mean(pnls),
            "win_rate": sum(value > 0 for value in pnls) / len(pnls),
        }
    return result


def _streak(pnls: list[float], *, wins: bool) -> int:
    best = current = 0
    for pnl in pnls:
        matched = pnl > 0 if wins else pnl < 0
        current = current + 1 if matched else 0
        best = max(best, current)
    return best


def _summary(
    observations: int,
    quality: list[dict[str, Any]],
    counters: ReplayCounters,
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    closed = [item for item in trades if item["state"] == "CLOSED"]
    pnls = [item["gross_rupees"] for item in closed]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    equity = peak = max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    performance = {
        "gross_pnl": sum(pnls),
        "net_pnl": None,
        "accounting_status": "GROSS_ONLY",
        "win_rate": len(wins) / len(closed) if closed else None,
        "loss_rate": len(losses) / len(closed) if closed else None,
        "average_pnl_per_trade": mean(pnls) if pnls else None,
        "median_pnl_per_trade": median(pnls) if pnls else None,
        "average_winner": mean(wins) if wins else None,
        "average_loser": mean(losses) if losses else None,
        "payoff_ratio": (mean(wins) / abs(mean(losses))
                         if wins and losses else None),
        "profit_factor": (sum(wins) / abs(sum(losses))
                          if losses and sum(losses) else None),
        "best_trade": max(pnls) if pnls else None,
        "worst_trade": min(pnls) if pnls else None,
        "maximum_gross_drawdown": max_drawdown if pnls else None,
        "maximum_consecutive_losses": _streak(pnls, wins=False),
        "maximum_consecutive_wins": _streak(pnls, wins=True),
        "average_holding_seconds": mean(
            item["holding_seconds"] for item in closed) if closed else None,
        "median_holding_seconds": median(
            item["holding_seconds"] for item in closed) if closed else None,
    }
    enriched = []
    for item in closed:
        value = dict(item)
        value["score_bucket"] = _score_bucket(item["entry_score"])
        value["entry_hour"] = item["entry_timestamp"][11:13] + ":00"
        enriched.append(value)
    return {
        "observations": observations,
        "usable_observations": sum(item["status"] == "USABLE" for item in quality),
        "unusable_observations": sum(item["status"] == "UNUSABLE" for item in quality),
        "signals": counters.signals,
        "confirmed_signals": counters.confirmed_signals,
        "candidates": counters.candidates,
        "risk_approved_entries": counters.risk_approved_entries,
        "entry_rejections": counters.entry_rejections,
        "executed_trades": counters.executed_trades,
        "closed_trades": len(closed),
        "unresolved_trades": sum(item["state"] == "UNRESOLVED" for item in trades),
        "performance": performance,
        "breakdowns": {
            "direction": _breakdown(enriched, "direction"),
            "score_bucket": _breakdown(enriched, "score_bucket"),
            "confirmation_count": _breakdown(enriched, "confirmation_count"),
            "spread_width": _breakdown(enriched, "width"),
            "hour": _breakdown(enriched, "entry_hour"),
            "exit_reason": _breakdown(enriched, "exit_reason"),
            "dte": _breakdown(enriched, "dte"),
            "vix_regime": _breakdown(enriched, "vix_regime"),
        },
        "statistical_significance": "NOT_ASSESSED",
        "profitability_claim": False,
    }


class ScalperReplayEngine:
    """Pure in-memory replay state machine using Phase 15 decision primitives."""

    def __init__(self, config: ScalperConfig, costs: Any) -> None:
        self.base_config = config
        self.costs = costs

    def run(
        self,
        rows: Iterable[ReplayRow],
        *,
        overrides: dict[str, Any] | None = None,
        split_label: str = "UNPARTITIONED",
        source_commit_sha: str | None = None,
        source_tree_hash: str | None = None,
        run_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ReplayResult:
        original = list(rows)
        if not original:
            raise ValueError("SCALPER_REPLAY_DATASET_EMPTY")
        config, normalized_overrides = apply_replay_overrides(
            self.base_config, overrides)
        if split_label not in {"UNPARTITIONED", "IN_SAMPLE", "OUT_OF_SAMPLE"}:
            raise ValueError("SCALPER_REPLAY_SPLIT_INVALID")

        original_keys = [(item.snapshot.captured_at, item.snapshot_id)
                         for item in original]
        out_of_order_ids = {
            original[index].snapshot_id for index in range(1, len(original))
            if original_keys[index] < original_keys[index - 1]
        }
        ordered = sorted(original, key=lambda item: (
            item.snapshot.captured_at, item.snapshot_id))
        id_counts = Counter(item.snapshot_id for item in ordered)
        time_counts = Counter(item.snapshot.captured_at for item in ordered)
        key_counts = Counter(item.capture_key or digest(item.snapshot) for item in ordered)
        duplicate_ids = {value for value, count in id_counts.items() if count > 1}
        duplicate_times = {value for value, count in time_counts.items() if count > 1}
        duplicate_keys = {value for value, count in key_counts.items() if count > 1}

        base_policy = _policy(self.base_config, self.costs)
        policy = _policy(config, self.costs)
        base_policy_hash = digest(base_policy)
        policy_hash = digest(policy)
        mode = ("AUTHORITATIVE_REPLAY" if not normalized_overrides
                else "NON_AUTHORITATIVE_RESEARCH")
        events = ConfiguredMarketEventProvider.from_json(
            config.event_configuration_json)

        histories: list[ScalperMarketSnapshot] = []
        prior_signals: list[ScalperSignal] = []
        trades: list[ReplayTrade] = []
        quality_rows: list[dict[str, Any]] = []
        timeline: list[dict[str, Any]] = []
        counters = ReplayCounters()
        previous_at: datetime | None = None

        for row in ordered:
            quality = _quality_row(
                row,
                config,
                duplicate_ids=duplicate_ids,
                duplicate_times=duplicate_times,
                duplicate_keys=duplicate_keys,
                out_of_order_ids=out_of_order_ids,
                previous_at=previous_at,
            )
            quality_rows.append(quality)
            previous_at = row.snapshot.captured_at
            if quality["status"] == "UNUSABLE":
                continue

            history = [*histories, row.snapshot]
            features = build_features(
                history, config.feature_lookback, config.interval_seconds)
            features = features.model_copy(update={"snapshot_id": row.snapshot_id})
            signal = build_signal(
                row.snapshot,
                features,
                prior_signals,
                min_score=config.signal_min_score,
                min_confirmations=config.min_confirmations,
                max_confirmation_gap_seconds=config.interval_seconds * 1.5,
                phase14_context=row.context,
            ).model_copy(update={"snapshot_id": row.snapshot_id})

            feature_match = _model_match(
                row.stored_feature_json, features, ScalperFeatures)
            signal_match = _model_match(
                row.stored_signal_json, signal, ScalperSignal)

            for active in [item for item in trades if item.state in ACTIVE_STATES]:
                transition = _transition_trade(
                    active, row, signal, config, self.costs)
                if transition == "REJECTED":
                    counters.entry_rejections += 1
                elif transition == "EXECUTED":
                    counters.executed_trades += 1

            candidate_count = 0
            candidate_widths: list[float] = []
            decision = "NO_ACTION"
            if signal.direction.value != "NEUTRAL" and signal.score > 0:
                counters.signals += 1
            if signal.confirmed:
                counters.confirmed_signals += 1
            active_count = sum(item.state in ACTIVE_STATES for item in trades)
            if active_count == 0 and signal.confirmed:
                built = build_candidates(row.snapshot, signal, config)
                candidate_count = len(built.candidates)
                candidate_widths = [item.spread_width for item in built.candidates]
                counters.candidates += candidate_count
                if built.candidates:
                    candidate = built.candidates[0]
                    risk = evaluate_entry(
                        candidate,
                        signal,
                        _risk_state(trades, row.snapshot.captured_at.date()),
                        config,
                        row.snapshot.captured_at,
                        event_blocked=any(
                            item.block_entries
                            for item in events.events_at(row.snapshot.captured_at)),
                    )
                    structural_reference = (
                        features.local_low if signal.direction.value == "BULL"
                        else features.local_high)
                    document = json_value({
                        "candidate": candidate,
                        "signal": signal,
                        "features": features,
                        "phase14_context": signal.phase14_context,
                        "risk_decision": risk,
                        "policy": policy,
                        "policy_hash": policy_hash,
                        "decision_snapshot_id": row.snapshot_id,
                        "decision_timestamp": row.snapshot.captured_at,
                        "decision_source_timestamp": row.snapshot.source_market_timestamp,
                        "decision_spot": row.snapshot.nifty_spot,
                        "decision_vix": row.snapshot.india_vix,
                        "lot_size": row.snapshot.lot_size,
                        "lots": config.lots,
                        "structural_reference": structural_reference,
                        "candidate_rejections": built.rejection_counts,
                        "profitability_claim": False,
                        "cost_completeness": "GROSS_ONLY",
                        "broker_margin": None,
                        "dte": (candidate.short_leg.expiry
                                - row.snapshot.captured_at.date()).days,
                        "vix_regime": _vix_regime(row.snapshot.india_vix),
                    })
                    trade = ReplayTrade(
                        trade_id=digest([
                            SCALPER_VERSION, row.snapshot_id, candidate.candidate_id]),
                        state="SIGNAL",
                        candidate=candidate,
                        candidate_rank=1,
                        document=document,
                    )
                    trades.append(trade)
                    if (risk.approved and row.snapshot.captured_at.date()
                            < candidate.short_leg.expiry):
                        trade.state = "PENDING_ENTRY"
                        counters.risk_approved_entries += 1
                        decision = "ENTRY_PENDING"
                    else:
                        trade.state = "ENTRY_REJECTED"
                        trade.document["rejection_reason"] = (
                            "EXPIRY_DAY_ENTRY_FORBIDDEN"
                            if row.snapshot.captured_at.date() >= candidate.short_leg.expiry
                            else ",".join(risk.reasons))
                        counters.entry_rejections += 1
                        decision = "ENTRY_REJECTED"
                else:
                    decision = "NO_CANDIDATE"

            components = signal.components
            timeline.append({
                "snapshot_id": row.snapshot_id,
                "timestamp": row.snapshot.captured_at.isoformat(),
                "direction": signal.direction.value,
                "score": signal.score,
                "strength": signal.strength.value,
                "confirmation_count": signal.confirmation_count,
                "confirmed": signal.confirmed,
                "momentum": components.get("momentum"),
                "structure": components.get("structure"),
                "futures": components.get("futures"),
                "participation": components.get("participation"),
                "execution": components.get("execution"),
                "contradiction_penalty": signal.contradiction_penalty,
                "reasons": signal.reasons,
                "warnings": signal.warnings,
                "candidate_count": candidate_count,
                "candidate_widths": candidate_widths,
                "decision": decision,
                "LIVE_STORED_VS_REPLAY_FEATURE_MATCH": feature_match,
                "LIVE_STORED_VS_REPLAY_SIGNAL_MATCH": signal_match,
                "LIVE_STORED_VS_REPLAY_CANDIDATE_PRESENCE_MATCH": (
                    None if row.stored_candidate_present is None
                    else row.stored_candidate_present == bool(candidate_count)),
                "LIVE_STORED_VS_REPLAY_TRADE_DECISION_MATCH": (
                    None if row.stored_decision is None
                    else row.stored_decision == decision),
            })
            histories.append(row.snapshot)
            prior_signals.append(signal)

        for trade in trades:
            if trade.state in {"PENDING_ENTRY", "OPEN"}:
                prior_state = trade.state
                trade.state = "UNRESOLVED"
                trade.document["unresolved_reason"] = f"END_OF_DATA_{prior_state}"

        trade_rows = [_trade_output(item) for item in trades]
        summary = _summary(len(ordered), quality_rows, counters, trade_rows)
        comparison_names = (
            "LIVE_STORED_VS_REPLAY_FEATURE_MATCH",
            "LIVE_STORED_VS_REPLAY_SIGNAL_MATCH",
            "LIVE_STORED_VS_REPLAY_CANDIDATE_PRESENCE_MATCH",
            "LIVE_STORED_VS_REPLAY_TRADE_DECISION_MATCH",
        )
        summary["live_vs_replay"] = {
            name: {
                "matches": sum(item[name] is True for item in timeline),
                "mismatches": sum(item[name] is False for item in timeline),
                "unavailable": sum(item[name] is None for item in timeline),
            }
            for name in comparison_names
        }
        dataset = [{
            "snapshot_id": item.snapshot_id,
            "captured_at": item.snapshot.captured_at,
            "capture_key": item.capture_key or digest(item.snapshot),
            "snapshot_hash": digest(item.snapshot),
            "quote_count": len(item.snapshot.quotes),
        } for item in ordered]
        now = created_at or datetime.now(timezone.utc)
        manifest = {
            "run_id": run_id or str(uuid4()),
            "phase15_version": SCALPER_VERSION,
            "replay_version": REPLAY_VERSION,
            "source_commit_sha": source_commit_sha,
            "source_tree_hash": source_tree_hash or implementation_hash(),
            "base_policy_hash": base_policy_hash,
            "policy_hash": policy_hash,
            "overrides": normalized_overrides,
            "mode": mode,
            "split_label": split_label,
            "dataset_start": ordered[0].snapshot.captured_at,
            "dataset_end": ordered[-1].snapshot.captured_at,
            "snapshot_count": len(ordered),
            "dataset_hash": digest(dataset),
            "quote_count": sum(item["quote_count"] for item in dataset),
            "created_at": now,
            "accounting_completeness": "GROSS_ONLY",
            "execution_model": EXECUTION_MODEL,
            "ordering": "CAPTURED_AT_THEN_SNAPSHOT_ID",
            "profitability_claim": False,
        }
        manifest["manifest_hash"] = digest(manifest)
        daily: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in trade_rows:
            daily[item["date"]].append(item)
        daily_rows = tuple({
            "date": day,
            "trades": len(values),
            "closed_trades": sum(item["state"] == "CLOSED" for item in values),
            "gross_pnl": sum(item["gross_rupees"] or 0 for item in values),
            "net_pnl": None,
            "accounting_status": "GROSS_ONLY",
        } for day, values in sorted(daily.items()))
        return ReplayResult(
            manifest=json_value(manifest),
            summary=json_value(summary),
            trades=tuple(json_value(item) for item in trade_rows),
            observation_quality=tuple(json_value(item) for item in quality_rows),
            signal_timeline=tuple(json_value(item) for item in timeline),
            daily_summary=daily_rows,
        )


def load_replay_rows(
    sessions: sessionmaker[Session],
    start: date,
    end: date,
) -> list[ReplayRow]:
    """Read Phase 15 observations without writing to production tables."""
    if end < start:
        raise ValueError("SCALPER_REPLAY_DATE_RANGE_INVALID")
    lower = datetime.combine(start, time.min, tzinfo=IST)
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=IST)
    with sessions() as session:
        records = session.scalars(
            select(ScalperMarketSnapshotRecord).options(
                selectinload(ScalperMarketSnapshotRecord.quotes)).where(
                    ScalperMarketSnapshotRecord.captured_at >= lower,
                    ScalperMarketSnapshotRecord.captured_at < upper,
                ).order_by(
                    ScalperMarketSnapshotRecord.captured_at,
                    ScalperMarketSnapshotRecord.id,
                )
        ).all()
        trade_records = session.scalars(select(ScalperTrade).where(
            ScalperTrade.decision_snapshot_id.in_([record.id for record in records])
        )).all() if records else []
        stored_trades = {}
        for trade in trade_records:
            document = json.loads(trade.document)
            stored_trades[trade.decision_snapshot_id] = document
        return [ReplayRow(
            snapshot_id=record.id,
            snapshot=ScalperRepository._row_to_snapshot(record),
            stored_feature_json=record.feature_json,
            stored_signal_json=record.signal_json,
            context=(json.loads(record.context_json)
                     if record.context_json else None),
            capture_key=record.capture_key,
            stored_candidate_present=record.id in stored_trades,
            stored_decision=(
                None if record.id not in stored_trades else
                "ENTRY_PENDING" if (
                    stored_trades[record.id]["risk_decision"]["approved"]
                    and stored_trades[record.id]["decision_timestamp"][:10]
                    < stored_trades[record.id]["candidate"]["short_leg"]["expiry"]
                )
                else "ENTRY_REJECTED"
            ),
        ) for record in records]


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return canonical(value)
    return value


def _write_csv(path: Path, fields: tuple[str, ...], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fields})


def _write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_replay_outputs(result: ReplayResult, output: Path) -> dict[str, str]:
    """Create immutable research artifacts; existing outputs are never overwritten."""
    output.mkdir(parents=True, exist_ok=True)
    targets = {
        "trades": output / "trades.csv",
        "summary": output / "summary.json",
        "manifest": output / "manifest.json",
        "observation_quality": output / "observation_quality.csv",
        "signal_timeline": output / "signal_timeline.csv",
        "daily_summary": output / "daily_summary.csv",
    }
    existing = [path for path in targets.values() if path.exists()]
    if existing:
        raise FileExistsError(
            "SCALPER_REPLAY_OUTPUT_EXISTS:" + ",".join(str(path) for path in existing))
    _write_csv(targets["trades"], TRADE_FIELDS, result.trades)
    _write_csv(targets["observation_quality"], QUALITY_FIELDS,
               result.observation_quality)
    _write_csv(targets["signal_timeline"], SIGNAL_FIELDS, result.signal_timeline)
    daily_fields = (
        "date", "trades", "closed_trades", "gross_pnl", "net_pnl",
        "accounting_status")
    _write_csv(targets["daily_summary"], daily_fields, result.daily_summary)
    _write_json(targets["summary"], result.summary)
    _write_json(targets["manifest"], result.manifest)
    return {name: str(path) for name, path in targets.items()}
