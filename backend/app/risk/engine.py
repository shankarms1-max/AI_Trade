from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo

from app.data.models import MarketSnapshot
from app.features.models import MarketFeatureSnapshot
from app.regime.models import RegimeResult
from app.risk.candidate_checks import (
    defined_risk_is_valid,
    expected_strategy_for_regime,
    strategy_fingerprint,
)
from app.risk.event_checks import MarketEventProvider
from app.risk.exposure import RiskStateProvider
from app.risk.freshness import snapshot_age_seconds
from app.risk.limits import RiskConfig
from app.risk.market_checks import evidence_at_least
from app.risk.models import (
    CheckStatus,
    EvaluationContext,
    RiskCheck,
    RiskDecision,
    RiskDecisionType,
    RiskEvaluationSet,
)
from app.strategy.models import CreditSpreadCandidate, PricingBasis, StrategyCandidateSet

IST = ZoneInfo("Asia/Kolkata")


def _value(item) -> str:
    return str(getattr(item, "value", item))


def _check(
    checks: list[RiskCheck], code: str, status: CheckStatus, actual=None,
    threshold=None, message: str = "",
) -> None:
    checks.append(RiskCheck(
        check_code=code, status=status, actual_value=actual,
        threshold=threshold, message=message,
    ))


def _spread_pct(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    midpoint = (bid + ask) / 2
    return None if midpoint <= 0 else (ask - bid) / midpoint * 100


def _evaluate_candidate(
    candidate: CreditSpreadCandidate,
    candidate_set: StrategyCandidateSet,
    candidate_set_id: int | None,
    snapshot: MarketSnapshot,
    feature: MarketFeatureSnapshot,
    regime: RegimeResult,
    config: RiskConfig,
    context: EvaluationContext,
    state_provider: RiskStateProvider,
    event_provider: MarketEventProvider,
    prior_regimes: list[RegimeResult],
    prior_candidate_keys: list[set[str]],
    evaluated_at: datetime,
) -> RiskDecision:
    checks: list[RiskCheck] = []
    reasons: list[str] = []
    warnings: list[str] = []

    structure_valid = defined_risk_is_valid(candidate)
    _check(checks, "DEFINED_RISK_STRUCTURE", CheckStatus.PASS if structure_valid else CheckStatus.FAIL,
           structure_valid, True, "Spread must have two correctly ordered, same-expiry legs")
    reasons.append("DEFINED_RISK_CONFIRMED" if structure_valid else "INVALID_SPREAD_STRUCTURE")

    regime_name = _value(regime.regime)
    evidence_quality = _value(regime.evidence_quality)
    phase142 = config.credit_spread_policy.enabled
    repriced = None
    if phase142:
        from app.risk.repricing import independent_reprice
        repriced = independent_reprice(candidate, candidate_set, snapshot, regime, config, evaluated_at)
        _check(checks, "INDEPENDENT_EXECUTABLE_REPRICE", CheckStatus.PASS if repriced.valid else CheckStatus.FAIL,
               {"credit": repriced.credit, "width": repriced.width, "gross_max_loss": repriced.gross_max_loss},
               "authoritative exact quotes; absolute tolerance 1e-6", "Independent raw-contract payoff reconstruction")
        reasons.extend(repriced.reasons)
    expected = expected_strategy_for_regime(regime_name)
    # Snapshot identity is checked by the service/repository foreign-key context.
    regime_valid = expected == candidate.strategy_type and candidate_set.regime == regime_name
    _check(checks, "REGIME_STILL_ELIGIBLE", CheckStatus.PASS if regime_valid else CheckStatus.FAIL,
           regime_name, candidate.strategy_type.value, "Phase 4 must still map to this strategy")
    if not regime_valid:
        reasons.append("REGIME_NOT_DIRECTIONAL")

    if phase142:
        side = "BULLISH" if candidate.strategy_type.value == "BULL_PUT_SPREAD" else "BEARISH"
        family = candidate.strategy_family.value if candidate.strategy_family else "NO_TRADE"
        family_eligibility = regime.strategy_family_eligibility.value if regime.strategy_family_eligibility else "NONE"
        state_valid = (regime.strategy_logic_version == candidate_set.strategy_logic_version == "phase14_2_v1"
                       and candidate.strategy_version == candidate_set.strategy_version == "phase14_2_v1"
                       and regime.data_quality_state == "VALID" and regime.market_bias is not None and _value(regime.market_bias) not in {"CONFLICT", "INSUFFICIENT"})
        if family == "DIRECTIONAL_CREDIT_SPREAD":
            regime_valid = state_valid and side == _value(regime.market_bias) and family_eligibility in {"BOTH", "DIRECTIONAL_ONLY"}
        elif family == "THETA_CARRY_CREDIT_SPREAD":
            from app.strategy.economics import expected_move, expiry_context, side_safety
            policy = config.credit_spread_policy
            move = expected_move(snapshot, feature, expiry_context(snapshot, policy)["fractional_time_to_expiry"], policy.expected_move_source,
                                 quality_policy=policy if policy.replay_integrity_enabled else None)
            safer = side_safety(feature, move, policy)["selected_bias"] if move else None
            chosen = _value(regime.market_bias) if _value(regime.directional_strength) in {"STRONG", "MODERATE"} else safer
            from app.strategy.policy import STRENGTH_RANK
            opposing = regime.statistical_alpha_bias in {"BULLISH", "BEARISH"} and regime.statistical_alpha_bias != side
            alpha_ok = not opposing or STRENGTH_RANK.get(regime.statistical_alpha_strength or "NONE",0) <= STRENGTH_RANK[policy.theta_carry_max_opposing_alpha_strength]
            regime_valid = state_valid and policy.theta_carry_enabled and side == chosen and alpha_ok and family_eligibility in {"BOTH", "THETA_CARRY_ONLY"}
        else:
            regime_valid = False
        # Replace legacy directional mapping check, preserve all capital/data vetoes.
        checks.pop()
        if "REGIME_NOT_DIRECTIONAL" in reasons:
            reasons.remove("REGIME_NOT_DIRECTIONAL")
        _check(checks, "REGIME_STILL_ELIGIBLE", CheckStatus.PASS if regime_valid else CheckStatus.FAIL,
               family, family_eligibility, "Current bias and deterministic family eligibility")
        if not regime_valid:
            reasons.append("STRATEGY_FAMILY_NO_LONGER_ELIGIBLE")
    confidence_ok = phase142 or regime.confidence >= config.min_regime_confidence
    _check(checks, "REGIME_CONFIDENCE", CheckStatus.NOT_AVAILABLE if phase142 else CheckStatus.PASS if confidence_ok else CheckStatus.FAIL,
           regime.confidence, config.min_regime_confidence, "Deterministic regime confidence threshold")
    if not confidence_ok:
        reasons.append("REGIME_CONFIDENCE_TOO_LOW")
    evidence_ok = evidence_at_least(evidence_quality, config.min_evidence_quality)
    _check(checks, "EVIDENCE_QUALITY", CheckStatus.PASS if evidence_ok else CheckStatus.FAIL,
           evidence_quality, config.min_evidence_quality, "Minimum deterministic evidence quality")
    if not evidence_ok:
        reasons.append("EVIDENCE_QUALITY_TOO_LOW")

    data_consistent = (
        candidate.expiry == snapshot.expiry == feature.expiry
        and candidate.market_snapshot_id == feature.snapshot_id == regime.snapshot_id
        and (not phase142 or candidate.strategy_version == candidate_set.strategy_version)
        and candidate.regime_snapshot_id == candidate_set.regime_snapshot_id
        and candidate.strategy_type == candidate_set.strategy_type
        and bool(candidate.short_leg.trading_symbol)
        and bool(candidate.long_leg.trading_symbol)
    )
    _check(checks, "DATA_QUALITY", CheckStatus.PASS if data_consistent else CheckStatus.FAIL,
           data_consistent, True, "Snapshot, regime, expiry, and legs must be internally consistent")
    if not data_consistent:
        reasons.append("CRITICAL_DATA_QUALITY_FAILURE")
    from app.research.oi import static_oi_usable
    oi_usable = static_oi_usable(feature) if phase142 else feature.data_quality.intraday_oi_usable
    oi_ok = oi_usable or not config.require_intraday_oi
    _check(checks, "INTRADAY_OI", CheckStatus.PASS if oi_ok else CheckStatus.FAIL,
           oi_usable, config.require_intraday_oi,
           "Directional approval can require usable intraday OI")
    if not oi_ok:
        reasons.append("INTRADAY_OI_UNUSABLE")

    age = snapshot_age_seconds(snapshot.timestamp_ist, evaluated_at, context)
    fresh = age is None or 0 <= age <= config.max_snapshot_age_seconds
    _check(checks, "SNAPSHOT_FRESHNESS", CheckStatus.PASS if fresh else CheckStatus.FAIL,
           age, config.max_snapshot_age_seconds, "Historical context bypasses wall-clock freshness")
    _check(checks, "BROKER_DATA_STALENESS", CheckStatus.PASS if fresh else CheckStatus.FAIL,
           age, config.max_snapshot_age_seconds, "Persisted broker observation age")
    if not fresh:
        reasons.append("SNAPSHOT_STALE")

    timestamp = snapshot.timestamp_ist.astimezone(IST)
    in_window = (
        context == EvaluationContext.HISTORICAL
        or config.entry_start_time <= timestamp.time().replace(tzinfo=None) <= config.entry_end_time
    )
    _check(checks, "ENTRY_TIME_WINDOW", CheckStatus.PASS if in_window else CheckStatus.FAIL,
           timestamp.time().isoformat(),
           f"{config.entry_start_time.isoformat()}-{config.entry_end_time.isoformat()}",
           "Entry window is mandatory only for live evaluations")
    if not in_window:
        reasons.append("ENTRY_TOO_EARLY" if timestamp.time().replace(tzinfo=None) < config.entry_start_time else "ENTRY_TOO_LATE")
    expiry_ok = config.allow_expiry_day or timestamp.date() != candidate.expiry
    _check(checks, "EXPIRY_DAY_POLICY", CheckStatus.PASS if expiry_ok else CheckStatus.FAIL,
           timestamp.date() == candidate.expiry, config.allow_expiry_day, "Expiry-day entries are disabled by default")
    if not expiry_ok:
        reasons.append("EXPIRY_DAY_DISABLED")

    if candidate.pricing_basis == PricingBasis.BID_ASK:
        pricing_status = CheckStatus.PASS
    elif config.require_bid_ask or not config.allow_ltp_estimate:
        pricing_status = CheckStatus.FAIL
        reasons.append("BID_ASK_REQUIRED")
    else:
        pricing_status = CheckStatus.WARN
        warnings.append("NON_EXECUTABLE_PRICING_ESTIMATE")
        reasons.append("LTP_ESTIMATE_ONLY")
    _check(checks, "PRICING_QUALITY", pricing_status, candidate.pricing_basis.value,
           "BID_ASK" if config.require_bid_ask else "BID_ASK_OR_WARNED_LTP",
           "LTP is an estimate and not an executable credit")

    credit_ok = candidate.net_credit >= config.min_net_credit
    _check(checks, "MINIMUM_CREDIT", CheckStatus.PASS if credit_ok else CheckStatus.FAIL,
           candidate.net_credit, config.min_net_credit, "Minimum per-unit credit")
    if not credit_ok:
        reasons.append("MINIMUM_CREDIT_NOT_MET")
    ratio_ok = candidate.credit_to_width_ratio >= config.min_credit_to_width
    _check(checks, "CREDIT_TO_WIDTH", CheckStatus.PASS if ratio_ok else CheckStatus.FAIL,
           candidate.credit_to_width_ratio, config.min_credit_to_width, "Minimum credit efficiency")
    if not ratio_ok:
        reasons.append("CREDIT_TO_WIDTH_TOO_LOW")

    liquidity_values = [
        ("SHORT_OI", candidate.short_leg.open_interest, config.min_short_oi, "SHORT_OI_TOO_LOW"),
        ("LONG_OI", candidate.long_leg.open_interest, config.min_long_oi, "LONG_OI_TOO_LOW"),
    ]
    if config.require_volume:
        liquidity_values.extend([
            ("SHORT_VOLUME", candidate.short_leg.volume, config.min_short_volume, "SHORT_VOLUME_TOO_LOW"),
            ("LONG_VOLUME", candidate.long_leg.volume, config.min_long_volume, "LONG_VOLUME_TOO_LOW"),
        ])
    else:
        _check(checks, "SHORT_VOLUME", CheckStatus.NOT_AVAILABLE, candidate.short_leg.volume,
               config.min_short_volume, "Volume requirement disabled")
        _check(checks, "LONG_VOLUME", CheckStatus.NOT_AVAILABLE, candidate.long_leg.volume,
               config.min_long_volume, "Volume requirement disabled")
    for code, actual, threshold, reason in liquidity_values:
        ok = actual is not None and actual >= threshold
        _check(checks, code, CheckStatus.PASS if ok else CheckStatus.FAIL, actual, threshold,
               "Risk-stage independent liquidity validation")
        if not ok:
            reasons.append(reason)
    spreads = [_spread_pct(candidate.short_leg.bid, candidate.short_leg.ask),
               _spread_pct(candidate.long_leg.bid, candidate.long_leg.ask)]
    available_spreads = [value for value in spreads if value is not None]
    if available_spreads:
        spread_ok = max(available_spreads) <= config.max_bid_ask_spread_pct
        _check(checks, "BID_ASK_SPREAD", CheckStatus.PASS if spread_ok else CheckStatus.FAIL,
               max(available_spreads), config.max_bid_ask_spread_pct, "Maximum leg bid/ask spread")
        if not spread_ok:
            reasons.append("BID_ASK_TOO_WIDE")
    else:
        _check(checks, "BID_ASK_SPREAD", CheckStatus.NOT_AVAILABLE, None,
               config.max_bid_ask_spread_pct, "Bid/ask spread unavailable")

    width_ok = isfinite(candidate.spread_width) and candidate.spread_width > 0 and (
        config.max_spread_width is None or candidate.spread_width <= config.max_spread_width)
    _check(checks, "MAX_SPREAD_WIDTH", CheckStatus.PASS if width_ok else CheckStatus.FAIL,
           candidate.spread_width, config.max_spread_width,
           "Positive defined-risk width; optional explicitly configured hard ceiling")
    if not width_ok:
        reasons.append("MAX_SPREAD_WIDTH_EXCEEDED")
    calculated_loss = (repriced.gross_max_loss if repriced and repriced.gross_max_loss is not None
                       else candidate.spread_width - candidate.net_credit)
    payoff_ok = (
        candidate.net_credit > 0 and calculated_loss > 0
        and abs(calculated_loss - candidate.max_loss) < 1e-6
        and abs(candidate.net_credit - candidate.max_profit) < 1e-6
    )
    _check(checks, "MAX_LOSS_VALID", CheckStatus.PASS if payoff_ok else CheckStatus.FAIL,
           calculated_loss, candidate.max_loss, "Theoretical defined-risk payoff must reconcile")
    if not payoff_ok:
        reasons.append("INVALID_MAX_LOSS")
    reward_to_risk = (None if calculated_loss <= 0 else
                      (repriced.credit if repriced and repriced.credit is not None else candidate.net_credit) / calculated_loss)
    ratio_valid = reward_to_risk is not None and reward_to_risk > 0
    _check(checks, "REWARD_TO_RISK_VALID", CheckStatus.PASS if ratio_valid else CheckStatus.FAIL,
           reward_to_risk, ">0", "Reward-to-risk is max profit divided by max loss")
    if not ratio_valid:
        reasons.append("INVALID_RISK_REWARD")

    lot_size = snapshot.lot_size
    lot_ok = lot_size is not None and lot_size > 0
    _check(checks, "LOT_SIZE_AVAILABLE", CheckStatus.PASS if lot_ok else CheckStatus.FAIL,
           lot_size, "broker-confirmed positive integer", "Lot-level approval never guesses lot size")
    if not lot_ok:
        reasons.append("LOT_SIZE_UNAVAILABLE")
    executable_credit = repriced.credit if repriced and repriced.credit is not None else candidate.net_credit
    max_profit_lot = executable_credit * lot_size if lot_ok else None
    max_loss_lot = calculated_loss * lot_size if lot_ok else None
    capital_required = max_loss_lot
    capital_basis = "DEFINED_RISK_MAX_LOSS_PROXY" if capital_required is not None else None
    capital_at_risk_pct = (
        None if capital_required is None or config.capital_base is None
        else capital_required / config.capital_base * 100
    )

    if context == EvaluationContext.SHADOW and config.capital_base is None:
        _check(checks, "SHADOW_CAPITAL_BASE", CheckStatus.FAIL, None, "configured",
               "Shadow capital base is mandatory and never inferred")
        reasons.append("SHADOW_RISK_LIMIT_UNCONFIGURED")

    if config.max_loss_per_trade is None:
        _check(checks, "MAX_LOSS_PER_TRADE", CheckStatus.FAIL, max_loss_lot, None,
               "Mandatory monetary limit is not configured")
        reasons.append("SHADOW_RISK_LIMIT_UNCONFIGURED" if context == EvaluationContext.SHADOW else "RISK_LIMIT_UNCONFIGURED")
    else:
        loss_ok = max_loss_lot is not None and max_loss_lot <= config.max_loss_per_trade
        _check(checks, "MAX_LOSS_PER_TRADE", CheckStatus.PASS if loss_ok else CheckStatus.FAIL,
               max_loss_lot, config.max_loss_per_trade, "Theoretical max loss per lot")
        if not loss_ok:
            reasons.append("MAX_LOSS_EXCEEDED")
    if config.max_capital_per_trade is None:
        _check(checks, "MAX_CAPITAL_PER_TRADE", CheckStatus.FAIL, capital_required, None,
               "Mandatory capital proxy limit is not configured")
        reasons.append("SHADOW_RISK_LIMIT_UNCONFIGURED" if context == EvaluationContext.SHADOW else "RISK_LIMIT_UNCONFIGURED")
    else:
        capital_ok = capital_required is not None and capital_required <= config.max_capital_per_trade
        _check(checks, "MAX_CAPITAL_PER_TRADE", CheckStatus.PASS if capital_ok else CheckStatus.FAIL,
               capital_required, config.max_capital_per_trade,
               "Defined-risk max-loss proxy; not broker margin")
        if not capital_ok:
            reasons.append("MAX_CAPITAL_EXCEEDED")

    state = state_provider.get_state(timestamp.date())
    if context == EvaluationContext.HISTORICAL:
        state_usable = state.provider_kind in {"RESEARCH", "CUSTOM"}
    elif context == EvaluationContext.SHADOW:
        state_usable = state.authoritative_for_shadow and state.provider_kind == "SHADOW"
    else:
        state_usable = state.authoritative_for_live and state.provider_kind not in {"RESEARCH", "SHADOW"}
    _check(checks, "RISK_STATE_AVAILABLE", CheckStatus.PASS if state_usable else CheckStatus.FAIL,
           state.provider_kind, context.value, "State authority must match evaluation context")
    if not state_usable:
        reasons.append("RISK_STATE_UNAVAILABLE")
    if config.credit_spread_policy.replay_integrity_enabled:
        _check(checks, "MONETARY_RISK_STATE_COMPLETE", CheckStatus.PASS if state.monetary_pnl_complete else CheckStatus.FAIL,
               state.monetary_pnl_complete, True, "Unresolved exposure / incomplete costs block subsequent monetary decisions")
        if not state.monetary_pnl_complete:
            reasons.append("INCOMPLETE_MONETARY_RISK_STATE")
    trades_ok = state.trades_today < config.max_trades_per_day
    _check(checks, "MAX_TRADES_PER_DAY", CheckStatus.PASS if trades_ok else CheckStatus.FAIL,
           state.trades_today, config.max_trades_per_day, "Committed strategy count hook")
    if not trades_ok:
        reasons.append("MAX_TRADES_REACHED")
    if config.max_daily_loss is None:
        daily_status = CheckStatus.FAIL if context == EvaluationContext.SHADOW else CheckStatus.NOT_AVAILABLE
        _check(checks, "DAILY_LOSS_LIMIT", daily_status,
               state.realized_pnl_today, None, "Realized daily-loss limit not configured")
        if context == EvaluationContext.SHADOW:
            reasons.append("SHADOW_RISK_LIMIT_UNCONFIGURED")
    else:
        daily_ok = state.monetary_pnl_complete and state.realized_pnl_today > -config.max_daily_loss
        _check(checks, "DAILY_LOSS_LIMIT", CheckStatus.PASS if daily_ok else CheckStatus.FAIL,
               state.realized_pnl_today, -config.max_daily_loss, "Realized P&L only")
        if not daily_ok:
            reasons.append("DAILY_LOSS_LIMIT_REACHED" if state.monetary_pnl_complete else "LOT_SIZE_UNAVAILABLE")
    fingerprint = strategy_fingerprint(candidate, timestamp.date())
    duplicate = fingerprint in state.open_strategy_keys
    _check(checks, "DUPLICATE_STRATEGY", CheckStatus.FAIL if duplicate else CheckStatus.PASS,
           fingerprint, "not already open/committed", "Stable date/expiry/strategy/strike fingerprint")
    if duplicate:
        reasons.append("DUPLICATE_STRATEGY")

    events = event_provider.events_at(timestamp)
    blocking = [item.name for item in events if item.block_entries]
    nonblocking = [item.name for item in events if not item.block_entries]
    if blocking:
        _check(checks, "EVENT_RISK", CheckStatus.FAIL, blocking, "no blocking event", "Configured event window")
        reasons.append("BLOCKING_MARKET_EVENT")
    elif nonblocking:
        _check(checks, "EVENT_RISK", CheckStatus.WARN, nonblocking, "no blocking event", "Nonblocking configured event")
        warnings.append("NONBLOCKING_MARKET_EVENT")
    else:
        _check(checks, "EVENT_RISK", CheckStatus.PASS, [], "no blocking event", "No configured active event")

    if phase142:
        quotes_ok = repriced.valid
        _check(checks, "EXACT_LEG_QUOTE_VALIDITY", CheckStatus.PASS if quotes_ok else CheckStatus.FAIL,
               quotes_ok, True, "Same contract, valid book, observed quote freshness")
        if not quotes_ok:
            reasons.append("STALE_OR_INVALID_EXACT_LEG_QUOTE")
    required = config.required_consecutive_directional_snapshots
    sequence = [regime, *prior_regimes[:max(0, required - 1)]]
    confirmed = required <= 1 or (
        len(sequence) >= required
        and all(_value(item.regime) == regime_name and item.confidence >= config.min_regime_confidence
                for item in sequence[:required])
    )
    if phase142:
        confirmed = len(sequence) >= required and all(
            item.strategy_logic_version == "phase14_2_v1" and item.data_quality_state == "VALID"
            and item.market_bias == regime.market_bias and item.strategy_family_eligibility == regime.strategy_family_eligibility
            and item.timestamp.date() == regime.timestamp.date()
            and item.timestamp <= regime.timestamp
            and (not config.credit_spread_policy.replay_integrity_enabled or
                 item.persistence_identity == regime.persistence_identity)
            for item in sequence[:required]) and all(
                0 < (newer.timestamp-older.timestamp).total_seconds() <= config.max_snapshot_age_seconds
                for newer, older in zip(sequence[:required], sequence[1:required]))
    _check(checks, "CONSECUTIVE_REGIME_CONFIRMATION", CheckStatus.PASS if confirmed else CheckStatus.FAIL,
           [_value(item.regime) for item in sequence], required, "Consecutive same-direction Phase 4 snapshots")
    if not confirmed:
        reasons.append("INSUFFICIENT_REGIME_CONFIRMATION")

    if config.require_candidate_stability:
        needed = max(0, config.candidate_stability_snapshots - 1)
        stable = len(prior_candidate_keys) >= needed and all(
            fingerprint in keys for keys in prior_candidate_keys[:needed]
        )
        _check(checks, "CANDIDATE_STABILITY", CheckStatus.PASS if stable else CheckStatus.FAIL,
               sum(fingerprint in keys for keys in prior_candidate_keys[:needed]), needed,
               "Same strategy/expiry/strikes across prior candidate sets")
        if not stable:
            reasons.append("CANDIDATE_UNSTABLE")
    else:
        _check(checks, "CANDIDATE_STABILITY", CheckStatus.NOT_AVAILABLE, None, False,
               "Candidate stability policy disabled")

    failed = [item.check_code for item in checks if item.status == CheckStatus.FAIL]
    warning_checks = [item.check_code for item in checks if item.status in {CheckStatus.WARN, CheckStatus.NOT_AVAILABLE}]
    warnings.extend(warning_checks)
    decision = RiskDecisionType.REJECTED if failed else RiskDecisionType.APPROVED
    if decision == RiskDecisionType.APPROVED:
        reasons.append("RISK_APPROVED")
    including_costs = None
    cost_basis = None
    if config.credit_spread_policy.replay_integrity_enabled:
        complete_cost = repriced.cost_estimate_complete and repriced.estimated_cost_points_per_unit is not None
        cost_basis = "ESTIMATED" if complete_cost else "GROSS_ONLY"
        if complete_cost and max_loss_lot is not None:
            including_costs = max_loss_lot + repriced.estimated_cost_points_per_unit * lot_size
    return RiskDecision(
        research_run_id=config.credit_spread_policy.research_run_id,
        policy_hash=config.credit_spread_policy.policy_hash,
        execution_mode=config.credit_spread_policy.execution_mode,
        independent_reprice=None if repriced is None else {"valid": repriced.valid,
            "gross_credit_points_per_unit": repriced.credit, "width_points": repriced.width,
            "gross_max_loss_points_per_unit": repriced.gross_max_loss,
            "gross_max_loss_rupees_per_lot": repriced.gross_loss_per_lot,
            "credit_to_width": repriced.credit_to_width, "credit_to_max_loss": repriced.credit_to_max_loss},
        max_loss_including_estimated_costs_per_lot=including_costs,
        estimated_cost_basis=cost_basis,
        market_snapshot_id=candidate.market_snapshot_id,
        regime_snapshot_id=candidate.regime_snapshot_id,
        strategy_candidate_set_id=candidate_set_id,
        candidate_reference=candidate.candidate_id,
        candidate_fingerprint=fingerprint,
        strategy_version=candidate.strategy_version,
        decision=decision,
        candidate_strategy=candidate.strategy_type.value,
        max_profit_per_unit=executable_credit,
        max_loss_per_unit=calculated_loss,
        lot_size=lot_size,
        max_profit_per_lot=max_profit_lot,
        max_loss_per_lot=max_loss_lot,
        estimated_capital_required=capital_required,
        capital_basis=capital_basis,
        capital_at_risk_pct=capital_at_risk_pct,
        reward_to_risk_ratio=reward_to_risk,
        credit_to_width_ratio=candidate.credit_to_width_ratio,
        selection_score=candidate.selection_score,
        checks=checks,
        failed_checks=failed,
        warnings=list(dict.fromkeys(warnings)),
        reason_codes=list(dict.fromkeys(reasons)),
        created_at=evaluated_at,
    )


def evaluate_risk(
    candidate_set: StrategyCandidateSet,
    candidate_set_id: int | None,
    snapshot: MarketSnapshot,
    feature: MarketFeatureSnapshot,
    regime: RegimeResult,
    config: RiskConfig,
    context: EvaluationContext,
    state_provider: RiskStateProvider,
    event_provider: MarketEventProvider,
    *,
    prior_regimes: list[RegimeResult] | None = None,
    prior_candidate_keys: list[set[str]] | None = None,
    evaluated_at: datetime | None = None,
) -> RiskEvaluationSet:
    now = evaluated_at or datetime.now(IST)
    if not candidate_set.eligible or not candidate_set.candidates:
        decision = RiskDecision(
            research_run_id=config.credit_spread_policy.research_run_id,
            policy_hash=config.credit_spread_policy.policy_hash,
            execution_mode=config.credit_spread_policy.execution_mode,
            market_snapshot_id=candidate_set.snapshot_id,
            regime_snapshot_id=candidate_set.regime_snapshot_id,
            strategy_candidate_set_id=candidate_set_id,
            candidate_reference=None,
            candidate_fingerprint="NO_CANDIDATE",
            strategy_version=candidate_set.strategy_version,
            decision=RiskDecisionType.NOT_APPLICABLE,
            candidate_strategy="NONE",
            max_profit_per_unit=None,
            max_loss_per_unit=None,
            lot_size=snapshot.lot_size,
            max_profit_per_lot=None,
            max_loss_per_lot=None,
            estimated_capital_required=None,
            capital_basis=None,
            reward_to_risk_ratio=None,
            credit_to_width_ratio=None,
            selection_score=None,
            checks=[RiskCheck(check_code="CANDIDATE_EXISTS", status=CheckStatus.NOT_AVAILABLE,
                              actual_value=0, threshold=1, message="No eligible Phase 6 candidate")],
            failed_checks=[],
            warnings=[],
            reason_codes=["NO_STRATEGY_CANDIDATE"],
            created_at=now,
        )
        return RiskEvaluationSet(
            research_run_id=config.credit_spread_policy.research_run_id,
            policy_hash=config.credit_spread_policy.policy_hash,
            execution_mode=config.credit_spread_policy.execution_mode,
            snapshot_id=candidate_set.snapshot_id,
            strategy_version=candidate_set.strategy_version,
            candidate_count=0,
            approved_count=0,
            rejected_count=0,
            not_applicable=True,
            decisions=[decision],
            best_approved_candidate=None,
            created_at=now,
        )
    decisions = [
        _evaluate_candidate(
            candidate, candidate_set, candidate_set_id, snapshot, feature, regime,
            config, context, state_provider, event_provider,
            prior_regimes or [], prior_candidate_keys or [], now,
        )
        for candidate in candidate_set.candidates
    ]
    approved = [item for item in decisions if item.decision == RiskDecisionType.APPROVED]
    best = max(approved, key=lambda item: item.selection_score or 0).candidate_reference if approved else None
    return RiskEvaluationSet(
        research_run_id=config.credit_spread_policy.research_run_id,
        policy_hash=config.credit_spread_policy.policy_hash,
        execution_mode=config.credit_spread_policy.execution_mode,
        snapshot_id=candidate_set.snapshot_id,
        strategy_version=candidate_set.strategy_version,
        candidate_count=len(candidate_set.candidates),
        approved_count=len(approved),
        rejected_count=sum(item.decision == RiskDecisionType.REJECTED for item in decisions),
        not_applicable=False,
        decisions=decisions,
        best_approved_candidate=best,
        created_at=now,
    )
