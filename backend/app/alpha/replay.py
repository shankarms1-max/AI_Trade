"""Conservative chronological research replay from stored observations only.

No broker access, nearest-contract substitution, or invented fills. A trade whose
required exact-leg quote path is absent is NOT_EVALUABLE, never a zero-P&L trade.
"""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from math import floor
from statistics import mean

from app.alpha.engine import AlphaConfig, direction, joint_direction, signal_persistence
from app.alpha.experiments import ExperimentParameters
from app.alpha.models import AlphaFeatureSnapshot, AlphaHypothesis, AlphaValidity
from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.features.models import MarketFeatureSnapshot
from app.regime.engine import RegimeConfig, classify_regime
from app.regime.models import RegimeResult
from app.risk.candidate_checks import strategy_fingerprint
from app.risk.engine import evaluate_risk
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.risk.exposure import RiskState
from app.risk.limits import RiskConfig
from app.risk.models import EvaluationContext, RiskDecisionType
from app.shadow.exits import ShadowConfig
from app.strategy.candidate_engine import StrategyConfig, generate_candidates
from app.strategy.models import CreditSpreadCandidate


@dataclass(frozen=True)
class ReplayRow:
    snapshot_id: int
    snapshot: MarketSnapshot
    feature: MarketFeatureSnapshot
    feature_id: int
    alpha: AlphaFeatureSnapshot | None


@dataclass(frozen=True)
class ReplayCosts:
    brokerage_per_order: float = 0
    exchange_rate: float = 0
    stt_rate: float = 0
    gst_rate: float = 0
    stamp_rate: float = 0
    slippage_points_per_leg: float = 0

    def per_unit(self, lot_size: int, entry_turnover: float, exit_turnover: float) -> float:
        if lot_size <= 0:
            raise ValueError("lot size is required for per-unit costs")
        turnover = (entry_turnover + exit_turnover) * lot_size
        brokerage = 4 * self.brokerage_per_order
        exchange = turnover * self.exchange_rate
        stt = exit_turnover * lot_size * self.stt_rate
        stamp = entry_turnover * lot_size * self.stamp_rate
        gst = (brokerage + exchange) * self.gst_rate
        return (brokerage + exchange + stt + stamp + gst) / lot_size + 4 * self.slippage_points_per_leg


@dataclass(frozen=True)
class QuotePair:
    short: float
    long: float
    age_seconds: float
    leg_skew_seconds: float
    depth_available: bool
    fill_quality_state: str = "NEXT_OBSERVATION_SIMULATION"


class ReplayRiskStateProvider:
    def __init__(self, entries_by_day, realized_by_day):
        self.entries_by_day = entries_by_day
        self.realized_by_day = realized_by_day

    def get_state(self, trading_date):
        return RiskState(self.entries_by_day[trading_date], self.realized_by_day[trading_date],
                         frozenset(), False, False, "RESEARCH", True)


def _exact(snapshot: MarketSnapshot, leg) -> OptionContractSnapshot | None:
    if not leg.instrument_token:
        return None
    return next((item for item in snapshot.options
                 if item.exchange == "nse_fo" and item.instrument_token == leg.instrument_token and item.strike == leg.strike
                 and item.expiry == leg.expiry and item.option_type.value == leg.option_type), None)


def quote_pair(snapshot: MarketSnapshot, candidate: CreditSpreadCandidate, *, entry: bool,
               quantity: int, max_age_seconds: int = 30,
               max_leg_skew_seconds: int = 10) -> tuple[QuotePair | None, str]:
    short, long = _exact(snapshot, candidate.short_leg), _exact(snapshot, candidate.long_leg)
    if short is None or long is None:
        return None, "MISSING_EXACT_CONTRACT"
    if short.source_market_timestamp is None or long.source_market_timestamp is None:
        return None, "MISSING_QUOTE_TIMESTAMP"
    ages = [(snapshot.timestamp_ist - item.source_market_timestamp).total_seconds()
            for item in (short, long)]
    skew = abs((short.source_market_timestamp - long.source_market_timestamp).total_seconds())
    if min(ages) < 0 or max(ages) > max_age_seconds:
        return None, "STALE_LEG_QUOTE"
    if skew > max_leg_skew_seconds:
        return None, "LEG_TIME_SKEW"
    if (short.bid is None or short.ask is None or long.bid is None or long.ask is None
            or short.bid < 0 or long.bid < 0 or short.ask < short.bid or long.ask < long.bid):
        return None, "INVALID_OR_CROSSED_BOOK"
    depth_available = short.bid_quantity is not None and short.ask_quantity is not None and long.bid_quantity is not None and long.ask_quantity is not None
    if depth_available:
        available = (short.bid_quantity, long.ask_quantity) if entry else (short.ask_quantity, long.bid_quantity)
        if min(available) < quantity:
            return None, "INSUFFICIENT_DEPTH"
    return QuotePair(short.bid if entry else short.ask,
                     long.ask if entry else long.bid, max(ages), skew, depth_available), "OK"


def sample_tier(trades: int, sessions: int, *, early: int = 30,
                moderate: int = 100, strong: int = 500,
                minimum_strong_sessions: int = 20) -> str:
    if trades < early:
        return "INSUFFICIENT"
    if trades < moderate:
        return "EARLY"
    if trades < strong or sessions < minimum_strong_sessions:
        return "MODERATE"
    return "STRONG_CANDIDATE"


def _relabel_alpha(alpha: AlphaFeatureSnapshot | None, parameters: ExperimentParameters,
                   previous: list[AlphaFeatureSnapshot]) -> AlphaFeatureSnapshot | None:
    if alpha is None or alpha.hypothesis_type != AlphaHypothesis.CONTINUATION:
        return None
    threshold = parameters.alpha_threshold
    config = AlphaConfig(strong_upper=threshold, strong_lower=1-threshold,
                         moderate_upper=min(.7, threshold), moderate_lower=max(.3, 1-threshold))
    first = direction(alpha.alpha_1, config, alpha.signed_log_return)
    second = direction(alpha.alpha_2, config, alpha.signed_log_return)
    joint = joint_direction(first, second)
    count, reset = signal_persistence(
        joint, previous, timestamp=alpha.timestamp, session=alpha.session_id or "",
        reference_id=alpha.reference_instrument_id, reference_expiry=alpha.reference_expiry,
        option_expiry=alpha.expiry, ce_token=alpha.atm_ce_token, pe_token=alpha.atm_pe_token,
        hypothesis=alpha.hypothesis_type, max_gap_seconds=600, validity=alpha.validity_state)
    return alpha.model_copy(update={"alpha_1_direction": first, "alpha_2_direction": second,
                                    "joint_alpha_direction": joint,
                                    "consecutive_confirmation_count": count,
                                    "confirmed": count >= parameters.confirmations,
                                    "confirmation_reset_reason": reset})


def _metrics(closed: list[dict], counts: dict[str, int], observation_count: int) -> dict:
    pnls = [item["net_pnl_per_unit"] for item in closed]
    sessions = len({item["entry_at"].date() for item in closed})
    wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x < 0]
    equity = peak = max_drawdown = 0.0
    peak_at = recovery = None
    for index, pnl in enumerate(pnls):
        equity += pnl
        if equity > peak:
            if peak_at is not None:
                recovery = index - peak_at
            peak, peak_at = equity, index
        max_drawdown = max(max_drawdown, peak - equity)
    by_day, by_week = defaultdict(float), defaultdict(float)
    for item in closed:
        day = item["exit_at"].date()
        by_day[day] += item["net_pnl_per_unit"]
        by_week[day.isocalendar()[:2]] += item["net_pnl_per_unit"]
    def grouped(key):
        buckets = defaultdict(list)
        for item in closed:
            buckets[str(item[key])].append(item["net_pnl_per_unit"])
        return {name: {"trades": len(values), "net_expectancy": mean(values)}
                for name, values in sorted(buckets.items())}
    tail = sorted(pnls)[:max(1, floor(len(pnls) * .05))] if pnls else []
    return {
        "status": ("NOT_EVALUABLE" if not closed else
                   "PARTIAL_COVERAGE" if counts["missing"] or counts["no_fill"] else "EVALUABLE"),
        "sample_tier": sample_tier(len(closed), sessions), "trades": len(closed),
        "sessions": sessions, "net_expectancy": mean(pnls) if pnls else None,
        "net_pnl": sum(pnls) if pnls else None,
        "average_win": mean(wins) if wins else None,
        "average_loss": mean(losses) if losses else None,
        "payoff_ratio": mean(wins) / abs(mean(losses)) if wins and losses else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "max_drawdown": max_drawdown if pnls else None,
        "recovery_duration_trades": recovery, "worst_trade": min(pnls) if pnls else None,
        "worst_day": min(by_day.values()) if by_day else None,
        "worst_week": min(by_week.values()) if by_week else None,
        "expected_shortfall_5pct": mean(tail) if tail else None,
        "sampled_MAE": mean(item["sampled_MAE"] for item in closed) if closed else None,
        "sampled_MFE": mean(item["sampled_MFE"] for item in closed) if closed else None,
        "mark_coverage": (sum(item["marks"] for item in closed) /
                          sum(item["expected_marks"] for item in closed))
        if closed and not counts["missing"] and sum(item["expected_marks"] for item in closed) else None,
        "rejection_rate": counts["rejected"] / observation_count if observation_count else None,
        "missing_data_rate": counts["missing"] / observation_count if observation_count else None,
        "no_fill_rate": counts["no_fill"] / counts["approved"] if counts["approved"] else None,
        "performance_by_dte": grouped("dte"), "performance_by_direction": grouped("direction"),
        "performance_by_volatility_state": grouped("volatility_state"),
        "performance_by_entry_window": grouped("entry_window"),
        "parameter_neighborhood_stability": None,
        "incremental_evidence_group_contribution": "NOT_EVALUABLE_WITHOUT_ABLATION_REPLAY",
        "fill_method": "NEXT_OBSERVATION_SIMULATION",
        "sampled_extrema_warning": "Three-minute marks may miss intrainterval extremes",
        "profitability_claim": False,
        "counts": dict(counts),
    }


def replay_parameters(rows: list[ReplayRow], parameters: ExperimentParameters,
                      strategy_config: StrategyConfig, risk_config: RiskConfig,
                      regime_config: RegimeConfig, shadow_config: ShadowConfig,
                      costs: ReplayCosts = ReplayCosts(), quantity: int = 1,
                      entry_period: tuple[datetime, datetime] | None = None,
                      purge_unclosed_at_boundary: bool = False) -> dict:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    rows = sorted(rows, key=lambda item: (item.snapshot.timestamp_ist, item.snapshot_id))
    strategy_config = replace(strategy_config, allowed_spread_widths=parameters.spread_widths,
                              min_short_distance_points=parameters.short_strike_buffer,
                              min_credit_to_width_ratio=parameters.minimum_credit_to_width,
                              vix_elevated_distance_multiplier=parameters.volatility_distance_multiplier)
    risk_config = replace(risk_config, entry_start_time=parameters.entry_start,
                          entry_end_time=parameters.entry_end)
    shadow_config = replace(
        shadow_config,
        profit_target_credit_capture_pct=(parameters.profit_target_credit_capture_pct
                                          if parameters.profit_target_credit_capture_pct is not None else
                                          shadow_config.profit_target_credit_capture_pct),
        stop_loss_credit_multiple=(parameters.stop_loss_credit_multiple
                                   if parameters.stop_loss_credit_multiple is not None else
                                   shadow_config.stop_loss_credit_multiple),
        force_exit_time=parameters.force_exit_time or shadow_config.force_exit_time,
    )
    regime_config = replace(regime_config, use_statistical_alpha=True)
    previous_feature = None
    previous_regimes: list[RegimeResult] = []
    previous_keys: list[set[str]] = []
    previous_alpha: list[AlphaFeatureSnapshot] = []
    closed = []
    counts = defaultdict(int)
    pending = None
    open_trade = None
    entries_by_day = defaultdict(int)
    realized_by_day = defaultdict(float)
    state_provider = ReplayRiskStateProvider(entries_by_day, realized_by_day)
    for row in rows:
        snapshot = row.snapshot
        alpha = _relabel_alpha(row.alpha, parameters, previous_alpha)
        if alpha is not None:
            previous_alpha.append(alpha)
        regime = classify_regime(row.feature_id, row.feature, previous_feature, regime_config, alpha)
        previous_feature = row.feature
        candidate_set = generate_candidates(snapshot, row.feature, regime, row.snapshot_id,
                                             strategy_config, enforce_freshness=False)
        evaluated = evaluate_risk(candidate_set, None, snapshot, row.feature, regime, risk_config,
                                  EvaluationContext.HISTORICAL, state_provider,
                                  ConfiguredMarketEventProvider(), prior_regimes=previous_regimes,
                                  prior_candidate_keys=previous_keys,
                                  evaluated_at=snapshot.timestamp_ist)
        previous_regimes.insert(0, regime)
        previous_keys.insert(0, {strategy_fingerprint(item, snapshot.timestamp_ist.date())
                                 for item in candidate_set.candidates})
        if pending is not None:
            candidate, decision_at, entry_vix = pending
            pending = None
            if entry_period is not None and snapshot.timestamp_ist > entry_period[1]:
                counts["split_overlap_purged"] += 1
            elif snapshot.timestamp_ist.date() != decision_at.date():
                counts["missing"] += 1
            else:
                quote, reason = quote_pair(snapshot, candidate, entry=True, quantity=quantity)
                if quote is None:
                    counts["missing" if reason.startswith("MISSING") else "no_fill"] += 1
                elif quote.short <= quote.long or snapshot.lot_size is None:
                    counts["no_fill"] += 1
                else:
                    entries_by_day[snapshot.timestamp_ist.date()] += 1
                    open_trade = {"candidate": candidate, "entry_at": snapshot.timestamp_ist,
                                  "entry_credit": quote.short - quote.long, "entry_quote": quote,
                                  "entry_turnover": quote.short + quote.long,
                                  "vix": entry_vix, "lot_size": snapshot.lot_size,
                                  "marks": [], "expected_marks": 0}
        elif open_trade is not None and snapshot.timestamp_ist > open_trade["entry_at"]:
            item = open_trade
            if snapshot.timestamp_ist.date() != item["entry_at"].date():
                counts["missing"] += 1
                open_trade = None
                continue
            item["expected_marks"] += 1
            quote, reason = quote_pair(snapshot, item["candidate"], entry=False, quantity=quantity)
            if quote is None:
                if reason.startswith("MISSING"):
                    counts["missing"] += 1
                else:
                    counts["no_fill"] += 1
                # Missing exit observations prevent a valid path outcome.
                open_trade = None
            else:
                pnl = item["entry_credit"] - (quote.short - quote.long)
                item["marks"].append(pnl)
                opposite = ((item["candidate"].strategy_type.value == "BULL_PUT_SPREAD" and regime.regime.value == "BEARISH")
                            or (item["candidate"].strategy_type.value == "BEAR_CALL_SPREAD" and regime.regime.value == "BULLISH"))
                reference = item["candidate"].support_or_resistance_reference
                breached = (reference is not None and
                            ((item["candidate"].strategy_type.value == "BULL_PUT_SPREAD" and snapshot.nifty_spot < reference)
                             or (item["candidate"].strategy_type.value == "BEAR_CALL_SPREAD" and snapshot.nifty_spot > reference)))
                exit_now = (pnl >= item["entry_credit"] * shadow_config.profit_target_credit_capture_pct / 100
                            or pnl <= -item["entry_credit"] * shadow_config.stop_loss_credit_multiple
                            or snapshot.timestamp_ist.time().replace(tzinfo=None) >= shadow_config.force_exit_time
                            or shadow_config.exit_on_opposite_regime and opposite
                            or shadow_config.exit_on_structural_breach and breached)
                if exit_now:
                    cost = costs.per_unit(item["lot_size"], item["entry_turnover"], quote.short + quote.long)
                    entry_at = item["entry_at"]
                    outcome = {"entry_at": entry_at, "exit_at": snapshot.timestamp_ist,
                                   "net_pnl_per_unit": pnl - cost, "gross_pnl_per_unit": pnl,
                                   "sampled_MAE": min(item["marks"]), "sampled_MFE": max(item["marks"]),
                                   "marks": len(item["marks"]), "expected_marks": item["expected_marks"],
                                   "dte": (item["candidate"].expiry - entry_at.date()).days,
                                   "direction": item["candidate"].strategy_type.value,
                                   "volatility_state": item["vix"] or "UNAVAILABLE",
                                   "entry_window": f"{entry_at.hour:02d}:{entry_at.minute // 15 * 15:02d}",
                                   "entry_quote_age_seconds": item["entry_quote"].age_seconds,
                                   "exit_quote_age_seconds": quote.age_seconds,
                                   "leg_time_skew_seconds": max(item["entry_quote"].leg_skew_seconds,
                                                                  quote.leg_skew_seconds),
                                   "depth_available": item["entry_quote"].depth_available and quote.depth_available,
                                   "fill_quality_state": "NEXT_OBSERVATION_SIMULATION"}
                    if entry_period is not None and snapshot.timestamp_ist > entry_period[1]:
                        counts["split_overlap_purged"] += 1
                    else:
                        closed.append(outcome)
                        realized_by_day[snapshot.timestamp_ist.date()] += outcome["net_pnl_per_unit"] * item["lot_size"] * quantity
                    open_trade = None
        if open_trade is not None or pending is not None:
            continue
        local_time = snapshot.timestamp_ist.time().replace(tzinfo=None)
        if entry_period is not None and not entry_period[0] <= snapshot.timestamp_ist <= entry_period[1]:
            continue
        if not parameters.entry_start <= local_time <= parameters.entry_end:
            continue
        if entries_by_day[snapshot.timestamp_ist.date()] >= shadow_config.max_new_trades_per_day:
            continue
        if alpha is None or not alpha.confirmed or alpha.validity_state != AlphaValidity.VALID:
            continue
        approved = [decision for decision in evaluated.decisions
                    if decision.decision == RiskDecisionType.APPROVED and decision.candidate_reference]
        if not approved:
            counts["rejected"] += 1
            continue
        counts["approved"] += 1
        candidate_by_id = {item.candidate_id: item for item in candidate_set.candidates}
        approved_candidates = [candidate_by_id[item.candidate_reference] for item in approved
                               if item.candidate_reference in candidate_by_id]
        if approved_candidates:
            pending = (max(approved_candidates, key=lambda item: item.selection_score),
                       snapshot.timestamp_ist, row.feature.volatility_features.vix_regime)
    if pending is not None or open_trade is not None:
        counts["split_overlap_purged" if purge_unclosed_at_boundary and entry_period else "missing"] += 1
    observed = len(rows) if entry_period is None else sum(
        entry_period[0] <= row.snapshot.timestamp_ist <= entry_period[1] for row in rows)
    return _metrics(closed, counts, observed)
