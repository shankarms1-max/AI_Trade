"""Four isolated paper ledgers over one ordered, read-only observation stream."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from statistics import mean, median
from typing import Any

from app.research.costs import CostSchedule
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.research.manifest import digest, json_value
from app.scalper.config import ScalperConfig
from app.scalper.models import ScalperCandidate, ScalperDirection, ScalperSignal, ScalperStrength
from app.scalper.paper import executable_pair
from app.scalper.strategy import build_candidates
from app.scalper_lab import LAB_VERSION, STRATEGY_IDS
from app.scalper_lab.context import ResearchObservation, context_at
from app.scalper_lab.strategies import decide


@dataclass
class LabTrade:
    strategy_id: str
    candidate: ScalperCandidate
    decision: dict[str, Any]
    decision_snapshot_id: int
    state: str = "PENDING_ENTRY"
    entry_credit: float | None = None
    entry_timestamp: datetime | None = None
    entry_quotes: dict[str, Any] | None = None
    entry_spot: float | None = None
    exit_timestamp: datetime | None = None
    exit_reason: str | None = None
    exit_thesis: dict[str, Any] | None = None
    exit_quotes: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    rejection_reason: str | None = None
    best_gross_points: float = 0
    mae: float = 0
    mfe: float = 0
    last_mark_debit: float | None = None
    adverse_vwap_count: int = 0
    adverse_structure_count: int = 0
    premium_flip_count: int = 0

    def payload(self) -> dict[str, Any]:
        return json_value({
            "trade_id": digest([LAB_VERSION, self.strategy_id,
                                self.decision_snapshot_id, self.candidate.candidate_id]),
            "strategy_id": self.strategy_id, "state": self.state,
            "candidate": self.candidate, "decision": self.decision,
            "decision_snapshot_id": self.decision_snapshot_id,
            "entry_credit": self.entry_credit, "entry_timestamp": self.entry_timestamp,
            "entry_spot": self.entry_spot, "entry_quotes": self.entry_quotes,
            "exit_timestamp": self.exit_timestamp, "exit_reason": self.exit_reason,
            "exit_thesis": self.exit_thesis, "exit_quotes": self.exit_quotes,
            "outcome": self.outcome, "rejection_reason": self.rejection_reason,
            "best_gross_points": self.best_gross_points, "mae": self.mae,
            "mfe": self.mfe, "last_mark_debit": self.last_mark_debit,
            "holding_seconds": ((self.exit_timestamp - self.entry_timestamp).total_seconds()
                                if self.exit_timestamp and self.entry_timestamp else None)})


@dataclass
class Ledger:
    watch: dict[str, Any] = field(default_factory=dict)
    trades: list[LabTrade] = field(default_factory=list)

    def active(self) -> LabTrade | None:
        return next((trade for trade in reversed(self.trades)
                     if trade.state in {"PENDING_ENTRY", "OPEN", "UNRESOLVED"}), None)


@dataclass(frozen=True)
class LabResult:
    manifest: dict[str, Any]
    contexts: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    trades: tuple[dict[str, Any], ...]
    summaries: dict[str, dict[str, Any]]
    comparison: tuple[dict[str, Any], ...]


def _construction_signal(raw, direction: str) -> ScalperSignal:
    # Adapter for existing short-leg-first executable construction. It carries
    # a constant placeholder; the lab's actual entry decision has no score gate.
    return ScalperSignal(timestamp=raw.captured_at, direction=ScalperDirection(direction),
                         strength=ScalperStrength.STRONG, score=100, components={"structure": 0},
                         contradiction_penalty=0, reasons=[], warnings=[],
                         confirmation_count=1, confirmed=True)


def _candidate(raw, decision: dict, context: dict, config: ScalperConfig):
    strike_keys = [(quote.expiry, quote.strike, quote.option_type)
                   for quote in raw.quotes]
    if len(strike_keys) != len(set(strike_keys)):
        return None, {"AMBIGUOUS_OPTION_STRIKE": 1}
    built = build_candidates(raw, _construction_signal(raw, decision["direction"]), config)
    wall = context["options"]["walls"]["put_support" if decision["direction"] == "BULL"
                                       else "call_resistance"]
    rows = {tuple(row["identity"]): row for row in context["options"]["contracts"]}

    def key(candidate):
        quote = candidate.quote_evidence["short"]
        identity = (quote["exchange"], quote["instrument_token"], quote["expiry"],
                    quote["strike"], quote["option_type"], quote["trading_symbol"])
        position = rows.get(identity, {})
        activity = position.get("activity", {}).get("1m")
        price = position.get("premium_change", {}).get("1m")
        wall_distance = abs(candidate.short_leg.strike - wall["strike"]) if wall else 500
        # Wall relevance is combined with fresh premium/OI behavior and the
        # builder's actual liquidity, book and hedge economics. Max OI alone
        # never decides a short leg.
        return (wall_distance / 50,
                0 if activity == "WRITING" else 1 if activity == "STABLE" else 2,
                0 if price is not None and price <= 0 else 1,
                -candidate.construction_evidence["short_quality_score"],
                -candidate.premium_retention_ratio,
                candidate.spread_width, candidate.candidate_id)

    ranked = sorted(built.candidates, key=key)
    return (ranked[0] if ranked else None), built.rejection_counts


def _outcome(trade: LabTrade) -> dict[str, Any]:
    assert trade.outcome is not None
    return trade.outcome


def _exited_at(trade: LabTrade) -> datetime:
    assert trade.exit_timestamp is not None
    return trade.exit_timestamp


def _holding_seconds(trade: LabTrade) -> float:
    assert trade.entry_timestamp is not None
    return (_exited_at(trade) - trade.entry_timestamp).total_seconds()


def _risk_blockers(ledger: Ledger, config: ScalperConfig, at: datetime,
                   event_blocked: bool) -> list[str]:
    reasons: list[str] = []
    today = [trade for trade in ledger.trades if trade.decision["timestamp"][:10] == str(at.date())]
    closed = [trade for trade in today if trade.state == "CLOSED"]
    settled = sorted(closed, key=_exited_at)
    losses = [trade for trade in settled if _outcome(trade)["gross_rupees"] < 0]
    streak = 0
    for trade in reversed(settled):
        if _outcome(trade)["gross_rupees"] >= 0:
            break
        streak += 1
    local_time = at.time().replace(tzinfo=None)
    if config.kill_switch:
        reasons.append("KILL_SWITCH_ACTIVE")
    if event_blocked:
        reasons.append("MARKET_EVENT_BLOCKED")
    if not config.start_time <= local_time <= config.entry_end_time:
        reasons.append("OUTSIDE_ENTRY_WINDOW")
    if ledger.active() is not None:
        reasons.append("MAX_OPEN_POSITIONS_REACHED")
    if sum(trade.entry_timestamp is not None for trade in today) >= config.max_trades_per_day:
        reasons.append("MAX_TRADES_PER_DAY_REACHED")
    if sum(_outcome(trade)["gross_rupees"] for trade in closed) <= -config.hard_daily_loss:
        reasons.append("HARD_DAILY_LOSS_REACHED")
    if streak >= config.max_consecutive_losses:
        reasons.append("MAX_CONSECUTIVE_LOSSES_REACHED")
    if settled and (at - _exited_at(settled[-1])).total_seconds() < config.cooldown_after_exit_seconds:
        reasons.append("ANY_EXIT_COOLDOWN_ACTIVE")
    if losses and (at - _exited_at(losses[-1])).total_seconds() < config.cooldown_after_loss_seconds:
        reasons.append("LOSS_COOLDOWN_ACTIVE")
    return reasons


def _wall_row(trade: LabTrade, ctx: dict) -> dict | None:
    held = trade.decision["thesis"].get("wall")
    if held is None:
        return None
    return next((row for row in ctx["options"]["contracts"]
                 if row["identity"] == held["identity"]), None)


def _entry_valid(trade: LabTrade, ctx: dict) -> str | None:
    strategy = trade.strategy_id
    direction = trade.decision["direction"]
    sign = 1 if direction == "BULL" else -1
    if strategy in {"VWAP_OI_REJECTION", "ORB_RETEST"}:
        if ctx["vwap"]["status"] != "AVAILABLE":
            return "PENDING_VWAP_UNAVAILABLE"
        if sign * (ctx["future"] - ctx["vwap"]["value"]) <= 0:
            return "PENDING_VWAP_ALIGNMENT_LOST"
    if strategy == "VWAP_OI_REJECTION" and ctx["regime"] != (
            "BULLISH" if sign > 0 else "BEARISH"):
        return "PENDING_REGIME_REVERSED"
    if strategy == "ORB_RETEST":
        line = (trade.decision["thesis"]["opening_high"] if sign > 0
                else trade.decision["thesis"]["opening_low"])
        if sign * (ctx["spot"] - line) < -2:
            return "PENDING_ORB_STRUCTURE_FAILED"
    if strategy == "TREND_PULLBACK":
        five = ctx["futures_returns_bps"].get("5m")
        fifteen = ctx["futures_returns_bps"].get("15m")
        if five is None or fifteen is None or sign * five < 4 or sign * fifteen < 8:
            return "PENDING_TREND_REVERSED"
    if strategy == "OI_WALL":
        row = _wall_row(trade, ctx)
        if row is not None and row["activity"]["1m"] in {"LONG_BUILDUP", "SHORT_COVERING"}:
            return "PENDING_WALL_BEHAVIOR_REVERSED"
    return None


def _exit_reason(trade: LabTrade, ctx: dict, debit: float,
                 config: ScalperConfig, *, event_blocked: bool) -> str | None:
    assert trade.entry_credit is not None and trade.entry_timestamp is not None
    direction = trade.decision["direction"]
    sign = 1 if direction == "BULL" else -1
    spot = ctx["spot"]
    thesis = trade.decision["thesis"]
    raw_at = datetime.fromisoformat(ctx["timestamp"])
    # Hard exits have no waiting period. They still require a real exit book.
    if raw_at.time().replace(tzinfo=None) >= config.forced_exit_time:
        return "FORCED_CLOSE"
    if config.kill_switch or event_blocked:
        return "KILL_SWITCH" if config.kill_switch else "MARKET_EVENT_EXIT"
    if debit >= trade.entry_credit * config.stop_credit_multiple:
        return "SPREAD_STOP"
    if (debit - trade.entry_credit) * ctx["lot_size"] * config.lots >= config.max_loss_per_trade:
        return "HARD_MAX_LOSS"

    if trade.strategy_id == "VWAP_OI_REJECTION":
        wall = thesis.get("wall")
        if wall and sign * (spot - wall["strike"]) < -10:
            return "STRUCTURE_FAILURE"
        current = _wall_row(trade, ctx)
        delta = current["delta_oi"]["1m"] if current else None
        prior = current["open_interest"] - delta if current and delta is not None else None
        if prior and delta is not None and delta < 0 and -delta / prior >= .10:
            return "OI_WALL_UNWIND"
        adverse = (ctx["vwap"]["status"] == "AVAILABLE"
                   and sign * (ctx["future"] - ctx["vwap"]["value"]) <= 0)
        trade.adverse_vwap_count = trade.adverse_vwap_count + 1 if adverse else 0
        if trade.adverse_vwap_count >= 2:
            return "SUSTAINED_VWAP_RECLAIM"
    elif trade.strategy_id == "ORB_RETEST":
        line = thesis["opening_high"] if sign > 0 else thesis["opening_low"]
        signed = sign * (spot - line)
        if signed < -5:
            return "ORB_STRUCTURE_FAILURE"
        trade.adverse_structure_count = trade.adverse_structure_count + 1 if signed < 0 else 0
        if trade.adverse_structure_count >= 2:
            return "SUSTAINED_ORB_REENTRY"
    elif trade.strategy_id == "OI_WALL":
        failed_wall = thesis.get("failed_wall")
        if failed_wall is not None and sign * (spot - failed_wall) < -5:
            return "WALL_BREAK_REENTRY"
        wall = thesis.get("wall")
        if wall and sign * (spot - wall["strike"]) < -10:
            return "WALL_STRUCTURE_FAILURE"
        current = _wall_row(trade, ctx)
        delta = current["delta_oi"]["1m"] if current else None
        prior = current["open_interest"] - delta if current and delta is not None else None
        if prior and delta is not None and delta < 0 and -delta / prior >= .10:
            return "OI_WALL_UNWIND"
        flipped = bool(current and current["activity"]["1m"] in
                       {"LONG_BUILDUP", "SHORT_COVERING"})
        trade.premium_flip_count = trade.premium_flip_count + 1 if flipped else 0
        if trade.premium_flip_count >= 2:
            return "PREMIUM_OI_FLIP"
    elif trade.strategy_id == "TREND_PULLBACK":
        anchor = thesis["pullback_anchor"]
        if sign * (spot - anchor) < -5:
            return "PULLBACK_STRUCTURE_FAILURE"
        five = ctx["futures_returns_bps"].get("5m")
        fifteen = ctx["futures_returns_bps"].get("15m")
        if (five is not None and fifteen is not None
                and sign * five <= -4 and sign * fifteen <= -8):
            return "TREND_REVERSAL"

    pnl = trade.entry_credit - debit
    best = max(trade.best_gross_points, pnl)
    if (best >= trade.entry_credit * config.trailing_activation_pct / 100
            and pnl <= best - trade.entry_credit * config.trailing_giveback_pct / 100):
        return "TRAILING_EXIT"
    if debit <= trade.entry_credit * (1 - config.profit_capture_pct / 100):
        return "PROFIT_CAPTURE"
    if (raw_at - trade.entry_timestamp).total_seconds() >= config.time_stop_minutes * 60:
        return "TIME_STOP"
    return None


def _transition(trade: LabTrade, row: ResearchObservation, ctx: dict,
                config: ScalperConfig, costs: CostSchedule,
                *, event_blocked: bool) -> None:
    raw = row.snapshot
    if trade.state == "PENDING_ENTRY":
        age = (raw.captured_at - datetime.fromisoformat(trade.decision["timestamp"])).total_seconds()
        if age <= 0:
            return
        pair, failure = executable_pair(raw, trade.candidate, config, entry=True)
        reason = ("ENTRY_TTL_EXCEEDED" if age > config.entry_ttl_seconds else
                  "EXPIRY_DAY_ENTRY_FORBIDDEN" if raw.captured_at.date() >= trade.candidate.short_leg.expiry else
                  "KILL_SWITCH_ACTIVE" if config.kill_switch else
                  "MARKET_EVENT_BLOCKED" if event_blocked else failure or _entry_valid(trade, ctx))
        if reason is not None:
            trade.state, trade.rejection_reason = "ENTRY_REJECTED", reason
            return
        assert pair is not None
        loss = (trade.candidate.spread_width - pair.value) * raw.lot_size * config.lots
        if pair.value < config.min_credit or loss > config.max_loss_per_trade:
            trade.state, trade.rejection_reason = "ENTRY_REJECTED", "FILL_ECONOMICS_OUTSIDE_RISK"
            return
        trade.state = "OPEN"
        trade.entry_credit, trade.entry_timestamp = pair.value, raw.captured_at
        trade.entry_spot, trade.entry_quotes = raw.nifty_spot, pair.evidence
        trade.last_mark_debit = pair.value
        return
    if trade.state != "OPEN":
        return
    pair, failure = executable_pair(raw, trade.candidate, config, entry=False)
    if failure is not None:
        if raw.captured_at.time().replace(tzinfo=None) >= config.forced_exit_time:
            trade.state, trade.rejection_reason = "UNRESOLVED", failure
        return
    assert pair is not None and trade.entry_credit is not None
    pnl = trade.entry_credit - pair.value
    trade.mae, trade.mfe = min(trade.mae, pnl), max(trade.mfe, pnl)
    trade.last_mark_debit = pair.value
    reason = _exit_reason(trade, ctx, pair.value, config, event_blocked=event_blocked)
    trade.best_gross_points = max(trade.best_gross_points, pnl)
    if reason is None:
        return
    trade.state, trade.exit_reason, trade.exit_timestamp = "CLOSED", reason, raw.captured_at
    trade.exit_quotes = pair.evidence
    trade.exit_thesis = {"reason": reason, "context_timestamp": ctx["timestamp"],
                         "regime": ctx["regime"], "vwap": ctx["vwap"],
                         "wall": _wall_row(trade, ctx), "fast_direction": ctx["fast_direction"],
                         "spread_debit": pair.value}
    assert trade.entry_quotes is not None
    trade.outcome = costs.calculate(
        day=raw.captured_at.date(), lot_size=raw.lot_size, lots=config.lots,
        entry_short=trade.entry_quotes["short"]["fill_price"],
        entry_long=trade.entry_quotes["long"]["fill_price"],
        exit_short=pair.short_price, exit_long=pair.long_price)


def _summary(strategy: str, observations: int, decisions: list[dict],
             trades: list[LabTrade]) -> dict[str, Any]:
    closed = sorted((trade for trade in trades if trade.state == "CLOSED"),
                    key=_exited_at)
    pnls = [_outcome(trade)["gross_rupees"] for trade in closed]
    wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x < 0]
    equity = peak = max_drawdown = 0.0
    streak = worst_streak = 0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        streak = streak + 1 if pnl < 0 else 0
        worst_streak = max(worst_streak, streak)
    outcomes_complete = bool(closed and all(_outcome(trade)["net_rupees"] is not None
                                            for trade in closed))
    direction = Counter(trade.decision["direction"] for trade in trades)
    vix = Counter(trade.decision["thesis"].get("vix_regime", "UNAVAILABLE")
                  for trade in trades)
    regime = Counter(trade.decision["regime"] for trade in trades)
    rejection = Counter(trade.rejection_reason for trade in trades if trade.rejection_reason)
    blockers = Counter(reason for decision in decisions for reason in decision["blockers"])
    return {"strategy_id": strategy, "observations": observations,
            "setups": sum(row["status"] == "WATCH" for row in decisions),
            "watch_states": sum(row["status"] == "WATCH" for row in decisions),
            "qualified_entries": sum(row["status"] == "ENTER" and not row["blockers"]
                                     for row in decisions),
            "executed_trades": sum(trade.entry_timestamp is not None for trade in trades),
            "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else None,
            "gross_pnl": sum(pnls),
            "net_pnl": sum(_outcome(t)["net_rupees"] for t in closed) if outcomes_complete else None,
            "accounting_status": "NET_COMPLETE" if outcomes_complete else "GROSS_ONLY",
            "average_winner": mean(wins) if wins else None,
            "average_loser": mean(losses) if losses else None,
            "expectancy_per_trade": mean(pnls) if pnls else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "profit_factor_status": ("DEFINED" if losses else
                                     "NO_LOSING_TRADES" if wins else "NO_CLOSED_TRADES"),
            "max_drawdown": max_drawdown, "worst_trade": min(pnls) if pnls else None,
            "max_consecutive_losses": worst_streak,
            "average_holding_seconds": (mean(_holding_seconds(t) for t in closed) if closed else None),
            "median_holding_seconds": (median(_holding_seconds(t) for t in closed) if closed else None),
            "direction_split": dict(direction), "vix_regime_split": dict(vix),
            "market_regime_split": dict(regime),
            "exit_reason_split": dict(Counter(t.exit_reason for t in closed)),
            "unresolved_trades": sum(t.state == "UNRESOLVED" for t in trades),
            "rejection_counts": dict(rejection), "blocker_counts": dict(blockers)}


class ScalperLab:
    def __init__(self, config: ScalperConfig, costs: CostSchedule) -> None:
        self.config = replace(config, lots=1, allowed_widths=(100, 200, 300, 400))
        self.costs = costs
        self.events = ConfiguredMarketEventProvider.from_json(
            self.config.event_configuration_json)

    def run(self, observations: list[ResearchObservation], *,
            period: str = "DIRECT", event_blocked_at: set[datetime] | None = None) -> LabResult:
        if any(left.snapshot.captured_at >= right.snapshot.captured_at
               for left, right in zip(observations, observations[1:])):
            raise ValueError("LAB_OBSERVATIONS_NOT_STRICTLY_ORDERED")
        if len({row.snapshot_id for row in observations}) != len(observations):
            raise ValueError("LAB_DUPLICATE_SNAPSHOT_ID")
        ledgers = {strategy: Ledger() for strategy in STRATEGY_IDS}
        history: list[ResearchObservation] = []
        contexts, decisions = [], []
        for index, row in enumerate(observations):
            day = row.snapshot.captured_at.date()
            if history and history[-1].snapshot.captured_at.date() != day:
                for ledger in ledgers.values():
                    active = ledger.active()
                    if active is not None:
                        active.state = "UNRESOLVED" if active.state == "OPEN" else "ENTRY_REJECTED"
                        active.rejection_reason = "SESSION_DATA_ENDED"
                    ledger.watch.clear()
                history.clear()
            ctx = context_at(row, history, self.config.interval_seconds,
                             self.config.max_quote_age_seconds)
            ctx["snapshot_id"] = row.snapshot_id
            contexts.append(ctx)
            blocked = (row.snapshot.captured_at in (event_blocked_at or set())
                       or any(event.block_entries for event in
                              self.events.events_at(row.snapshot.captured_at)))
            for strategy, ledger in ledgers.items():
                active = ledger.active()
                if active is not None:
                    _transition(active, row, ctx, self.config, self.costs,
                                event_blocked=blocked)
                decision = decide(strategy, ctx, ledger.watch, index)
                decision["snapshot_id"] = row.snapshot_id
                decision["evidence_snapshot_id"] = row.snapshot_id
                if decision["status"] == "ENTER":
                    reason = _risk_blockers(ledger, self.config, row.snapshot.captured_at, blocked)
                    candidate, rejected = _candidate(row.snapshot, decision, ctx, self.config)
                    decision["candidate_rejections"] = rejected
                    if candidate is None:
                        reason.extend(["NO_EXECUTABLE_CANDIDATE", *sorted(rejected)])
                    if candidate is not None and row.snapshot.captured_at.date() >= candidate.short_leg.expiry:
                        reason.append("EXPIRY_DAY_ENTRY_FORBIDDEN")
                    if reason:
                        decision["blockers"] = list(dict.fromkeys([*decision["blockers"], *reason]))
                    else:
                        thesis = decision["thesis"]
                        short_quote = candidate.quote_evidence["short"]
                        long_quote = candidate.quote_evidence["long"]
                        wall = thesis.get("wall")
                        matching = next((contract for contract in ctx["options"]["contracts"]
                                         if contract["identity"] == [
                                             short_quote["exchange"],
                                             short_quote["instrument_token"],
                                             short_quote["expiry"], short_quote["strike"],
                                             short_quote["option_type"],
                                             short_quote["trading_symbol"]]), None)
                        thesis["short_leg_reason"] = {
                            "strike": candidate.short_leg.strike,
                            "wall_strike": wall["strike"] if wall else None,
                            "wall_distance": (abs(candidate.short_leg.strike - wall["strike"])
                                              if wall else None),
                            "delta_oi_1m": matching["delta_oi"]["1m"] if matching else None,
                            "premium_change_1m": (matching["premium_change"]["1m"]
                                                  if matching else None),
                            "activity_1m": matching["activity"]["1m"] if matching else None,
                            "distance_from_spot": candidate.short_distance_points,
                            "bid": short_quote["bid"],
                            "short_quality": candidate.construction_evidence["short_quality_score"],
                            "book_validation": short_quote["validation"]}
                        thesis["hedge_reason"] = {
                            "strike": candidate.long_leg.strike,
                            "width": candidate.spread_width,
                            "ask": long_quote["ask"],
                            "premium_retention": candidate.premium_retention_ratio,
                            "defined_max_loss_per_lot": candidate.defined_max_loss_per_lot,
                            "book_validation": long_quote["validation"]}
                        thesis.update(failed_wall=decision.get("failed_wall"),
                                      opening_high=ctx["opening_high"],
                                      opening_low=ctx["opening_low"],
                                      pullback_anchor=decision.get("setup_anchor", ctx["spot"]),
                                      vix_regime=("UNAVAILABLE" if ctx["vix"] is None else
                                                  "LOW" if ctx["vix"] < 12 else
                                                  "NORMAL" if ctx["vix"] < 20 else
                                                  "ELEVATED" if ctx["vix"] < 30 else "HIGH"),
                                      selected_candidate=candidate.model_dump(mode="json"))
                        ledger.trades.append(LabTrade(strategy, candidate, decision.copy(),
                                                      row.snapshot_id))
                decisions.append(decision)
            history.append(row)
        for ledger in ledgers.values():
            active = ledger.active()
            if active is not None:
                active.state = "UNRESOLVED" if active.state == "OPEN" else "ENTRY_REJECTED"
                active.rejection_reason = "END_OF_DATA"
        summaries = {strategy: _summary(strategy, len(observations),
                     [d for d in decisions if d["strategy_id"] == strategy],
                     ledger.trades) for strategy, ledger in ledgers.items()}
        comparison = tuple(summaries[strategy] for strategy in STRATEGY_IDS)
        dataset_hash = digest([(row.snapshot_id, row.snapshot.model_dump(mode="json"),
                                row.futures) for row in observations])
        manifest = {"lab_version": LAB_VERSION, "period": period,
                    "dataset_hash": dataset_hash,
                    "config_hash": digest(self.config.payload()),
                    "cost_schedule_hash": digest(self.costs.payload()),
                    "snapshot_count": len(observations),
                    "start": observations[0].snapshot.captured_at.isoformat() if observations else None,
                    "end": observations[-1].snapshot.captured_at.isoformat() if observations else None,
                    "future_leakage": False, "execution_model": "NEXT_OBSERVATION_BID_ASK",
                    "profitability_claim": False}
        return LabResult(manifest, tuple(contexts), tuple(json_value(d) for d in decisions),
                         tuple(trade.payload() for ledger in ledgers.values()
                               for trade in ledger.trades), summaries, comparison)
