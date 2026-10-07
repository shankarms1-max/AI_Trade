"""Authoritative Phase 14.2.1 causal replay with retained incomplete exposure."""
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.alpha.replay import _relabel_alpha
from app.regime.engine import classify_regime
from app.research.analytics import path_coverage, replay_analytics
from app.research.config import replay_configuration, validate_replay_parameters
from app.research.features import reconstruct_feature
from app.research.manifest import authorize_split, digest, implementation_hash, json_value, registered_rows, validate_experiment_axes
from app.research.oi import oi_quality
from app.research.quotes import ContractIdentity, aware, execution_pair
from app.risk.candidate_checks import strategy_fingerprint
from app.risk.engine import evaluate_risk
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.risk.exposure import RiskState
from app.risk.models import EvaluationContext, RiskDecisionType
from app.shadow.exits import exit_reason
from app.strategy.candidate_engine import generate_candidates
from app.strategy.economics import expected_move, expiry_context
from app.strategy.models import StrategyType


class IsolatedRiskState:
    def __init__(self):
        self.entries = defaultdict(int)
        self.realized = defaultdict(float)
        self.keys = defaultdict(set)
        self.incomplete = set()

    def get_state(self, day):
        return RiskState(self.entries[day], self.realized[day], frozenset(self.keys[day]),
                         False, False, "RESEARCH", day not in self.incomplete)


def available_at(snapshot):
    receipt = snapshot.response_received_at
    return max(snapshot.timestamp_ist, receipt) if aware(receipt) else snapshot.timestamp_ist


def persistence_identity(snapshot, policy_hash, mode, move_source, alpha=None):
    return {"session": snapshot.timestamp_ist.date().isoformat(), "expiry": snapshot.expiry.isoformat(),
            "strategy_logic_version": "phase14_2_v1", "policy_hash": policy_hash,
            "reference": (alpha.reference_instrument_id if alpha else None) or snapshot.future_instrument_id or "NIFTY_SPOT",
            "reference_expiry": str(snapshot.future_expiry) if snapshot.future_instrument_id else None,
            "calculation_mode": mode, "expected_move_source_policy": move_source}


def _buckets(candidate):
    def score_bucket(value):
        return "UNAVAILABLE" if value is None else "LOW" if value < 40 else "MEDIUM" if value < 70 else "HIGH"
    distance = candidate.distance_in_expected_move_units
    return {"strategy_family": candidate.strategy_family.value, "directional_strength": candidate.directional_strength,
            "survival_bucket": score_bucket(candidate.survival_score), "carry_bucket": score_bucket(candidate.carry_score),
            "dte_bucket": candidate.dte_bucket, "spread_width": candidate.spread_width,
            "move_distance_bucket": "UNAVAILABLE" if distance is None else "LT_1" if distance < 1 else "1_TO_2" if distance < 2 else "GE_2",
            "expiry_stress_state": candidate.gamma_risk_state}


def replay_integrity(rows, parameters, strategy_config, risk_config, regime_config, shadow_config,
                     *, integrity, manifest, ledger, cost_schedule, split="TRAIN", quantity=1):
    if not integrity.enabled or not strategy_config.credit_spread_policy.enabled:
        raise ValueError("explicit isolated integrity/Phase14.2 research configuration required")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity is requested lots and must be a positive integer")
    validate_replay_parameters(parameters, strategy_config, shadow_config)
    config = replay_configuration(parameters, strategy_config, risk_config, regime_config, shadow_config, integrity, quantity)
    registered = manifest.payload()
    if (digest(config) != registered["config_hash"] or digest(cost_schedule) != registered["cost_schedule_hash"]
            or implementation_hash() != registered["working_tree_source_hash"]
            or ledger.manifest != manifest or ledger.split != split):
        raise ValueError("RUNTIME_MANIFEST_MISMATCH")
    sessions = authorize_split(manifest, split, policy_hash=registered["policy_hash"])
    if not sessions:
        raise ValueError("registered split is empty")
    validate_experiment_axes({name: [] for name in registered["active_experiment_axes"]},
                             use_alpha=regime_config.use_statistical_alpha,
                             theta_carry_enabled=strategy_config.credit_spread_policy.theta_carry_enabled)
    if parameters.volatility_distance_multiplier != 1 or parameters.confirmations != 2:
        raise ValueError("INACTIVE_EXPERIMENT_AXIS: legacy volatility multiplier / alpha confirmations")
    if parameters.strategy_family_mode in {"BOTH", "THETA_CARRY_ONLY"} and not strategy_config.credit_spread_policy.theta_carry_enabled:
        raise ValueError("INACTIVE_FAMILY_AXIS: theta carry disabled")
    if (not regime_config.use_statistical_alpha and parameters.alpha_threshold != .8
            or any(value is not None for value in (parameters.alpha1_upper, parameters.alpha1_lower,
                                                   parameters.alpha2_upper, parameters.alpha2_lower))):
        raise ValueError("INACTIVE_EXPERIMENT_AXIS: alpha thresholds")
    rows = registered_rows(manifest, rows)
    rows = sorted(rows, key=lambda row: (available_at(row.snapshot), row.snapshot_id))
    rows = [row for row in rows if row.snapshot.timestamp_ist.date().isoformat() <= max(sessions)]
    exposure = ledger.record_final_exposure() if split == "FINAL_TEST" else None
    source = registered["expected_move_source_policy"]
    policy = replace(strategy_config.credit_spread_policy, replay_integrity_enabled=True,
                     policy_hash=registered["policy_hash"], research_run_id=manifest.run_id,
                     execution_mode="ISOLATED_OFFLINE_REPLAY", expected_move_source=source,
                     research_quote_policy=integrity.quote_policy,
                     cost_schedule=cost_schedule, costs_complete=False, requested_lots=quantity,
                     source_quote_max_age_seconds=integrity.quote_policy.max_source_age_seconds,
                     quote_max_leg_skew_seconds=integrity.quote_policy.max_leg_skew_seconds,
                     expected_move_min_distance_units=parameters.expected_move_min_distance_units
                     if parameters.expected_move_min_distance_units is not None else strategy_config.credit_spread_policy.expected_move_min_distance_units,
                     family_mode=parameters.strategy_family_mode or strategy_config.credit_spread_policy.family_mode,
                     directional_alpha_min_strength=parameters.directional_min_strength or strategy_config.credit_spread_policy.directional_alpha_min_strength)
    if parameters.minimum_carry_score is not None:
        policy = replace(policy, strong_min_carry=parameters.minimum_carry_score,
                         moderate_min_carry=parameters.minimum_carry_score, theta_min_carry=parameters.minimum_carry_score)
    strategy = replace(strategy_config, credit_spread_policy=policy, allowed_spread_widths=parameters.spread_widths,
                       max_defined_loss_rupees=risk_config.max_loss_per_trade,
                       max_defined_capital_rupees=risk_config.max_capital_per_trade,
                       max_defined_width=risk_config.max_spread_width, requested_lots=quantity,
                       min_short_distance_points=parameters.short_strike_buffer,
                       min_credit_to_width_ratio=parameters.minimum_credit_to_width)
    risk = replace(risk_config, credit_spread_policy=policy, require_bid_ask=True, allow_ltp_estimate=False,
                   max_trades_per_day=min(risk_config.max_trades_per_day, shadow_config.max_new_trades_per_day),
                   allow_expiry_day=False, entry_start_time=parameters.entry_start, entry_end_time=parameters.entry_end,
                   max_loss_per_trade=None if risk_config.max_loss_per_trade is None else risk_config.max_loss_per_trade / quantity,
                   max_capital_per_trade=None if risk_config.max_capital_per_trade is None else risk_config.max_capital_per_trade / quantity)
    regime_conf = replace(regime_config, credit_spread_policy=policy)
    shadow = replace(shadow_config,
                     profit_target_credit_capture_pct=parameters.profit_target_credit_capture_pct or shadow_config.profit_target_credit_capture_pct,
                     stop_loss_credit_multiple=parameters.stop_loss_credit_multiple or shadow_config.stop_loss_credit_multiple,
                     force_exit_time=parameters.force_exit_time or shadow_config.force_exit_time)
    provider = ConfiguredMarketEventProvider.from_json(__import__("json").dumps(registered["event_config"]))
    state, attempts, events = IsolatedRiskState(), [], []
    previous_raw = previous_feature = previous_at = None
    prior_regimes, prior_keys, prior_alpha = [], [], []
    pending = open_trade = None
    raw_history = []
    marked_equity, gross_realized = [0.0], 0.0

    def emit(kind, at, attempt=None, **details):
        event = {"event_type": kind, "timestamp": at, "attempt_id": None if attempt is None else attempt["attempt_id"], **details}
        events.append(json_value(event))
        ledger.append(event)

    def finish(attempt, at, state_name, reason, evaluability="NOT_EVALUABLE"):
        attempt.update(state=state_name, missing_reason=reason, final_evaluability=evaluability)
        if state_name in {"NO_FILL", "INVALID_FILL"}:
            attempt["fill_status"] = state_name
        emit("FINAL_OUTCOME", at, attempt, outcome=attempt)

    def unresolved(attempt, at, reason):
        day = attempt["entry_at"].date()
        state.incomplete.add(day)
        end = datetime.combine(day, integrity.session_end, attempt["entry_at"].tzinfo)
        elapsed = max(0, (end-attempt["entry_observation_at"]).total_seconds())
        expected = int(elapsed // integrity.expected_interval_seconds) + 1
        observed = len(attempt["marks"])
        attempt.update(expected_observations=expected, observed_observations=observed,
                       missing_observations=max(0, expected-observed), path_coverage_ratio=min(1, observed/expected),
                       gross_pnl=None, net_pnl=None, cost_completeness="UNRESOLVED",
                       sampled_MAE_points_per_unit=min(attempt["marks"]), sampled_MFE_points_per_unit=max(attempt["marks"]))
        finish(attempt, at, "UNRESOLVED_EXPOSURE", reason, "PARTIAL_PATH")

    def decision(snapshot, at, candidate=None, approved=False, state_name="REJECTED", reason=None, risk_decision=None):
        attempt = {"run_id": manifest.run_id, "attempt_id": f"{manifest.run_id}:{len(attempts)+1}",
                   "strategy_logic_version": "phase14_2_v1", "policy_hash": policy.policy_hash,
                   "decision_timestamp": at, "decision_observation_timestamp": snapshot.timestamp_ist,
                   "expected_next_observation_timestamp": snapshot.timestamp_ist+timedelta(seconds=integrity.expected_interval_seconds),
                   "actual_next_observation_timestamp": None, "decision_to_fill_delay_seconds": None,
                   "approved": approved, "state": state_name, "move_source": source,
                   "fill_status": "NOT_ATTEMPTED",
                   "decision_time_candidate": None if candidate is None else candidate.model_dump(mode="python"),
                   "decision_time_risk": None if risk_decision is None else risk_decision.model_dump(mode="python"),
                   "exact_contracts": None if candidate is None else [ContractIdentity.of(candidate.short_leg), ContractIdentity.of(candidate.long_leg)],
                   "entry_execution": None, "exit_execution": None, "final_outcome": None,
                   "missing_reason": reason, "final_evaluability": "NOT_EVALUABLE",
                   "depth_status": "NOT_CHECKED", "cost_completeness": "UNAVAILABLE"}
        if candidate is not None:
            attempt.update(_buckets(candidate), candidate=candidate)
        attempts.append(attempt)
        # Runtime model object is deliberately excluded from immutable recorded truth.
        emit("DECISION_ATTEMPT", at, attempt, decision={key: value for key, value in attempt.items() if key != "candidate"})
        return attempt

    for row in rows:
        snapshot, at = row.snapshot, available_at(row.snapshot)
        day, local_time = snapshot.timestamp_ist.date(), snapshot.timestamp_ist.time().replace(tzinfo=None)
        in_split = day.isoformat() in sessions
        entry_window = (in_split and parameters.entry_start <= local_time <= parameters.entry_end
                        and integrity.session_start <= local_time <= integrity.session_end)
        gap = None if previous_at is None else (snapshot.timestamp_ist-previous_at).total_seconds()
        emit("OBSERVATION", at, snapshot_id=row.snapshot_id, observation_timestamp=snapshot.timestamp_ist,
             feature_available=row.feature is not None, elapsed_interval_seconds=gap)
        duplicate_or_late = previous_at is not None and snapshot.timestamp_ist <= previous_at
        if duplicate_or_late:
            emit("INVALID_OBSERVATION", at, reason="DUPLICATE_OR_LATE_SOURCE_ORDER")
            if open_trade is not None:
                unresolved(open_trade, at, "LATE_OR_DUPLICATE_PATH_OBSERVATION")
                open_trade = None
            continue
        if gap is not None and gap > integrity.max_path_gap_seconds:
            previous_feature = None
            prior_regimes, prior_keys, prior_alpha = [], [], []
        feature, regime, candidates = None, None, None
        rebuilt = reconstruct_feature(row, raw_history, integrity.feature_engine_config)
        if rebuilt is not None:
            feature = rebuilt.model_copy(update={"data_quality": rebuilt.data_quality.model_copy(update=
                oi_quality(snapshot, previous_raw, max_interval_seconds=integrity.max_path_gap_seconds))})
            emit("CAUSAL_FEATURE_RECONSTRUCTION", at, snapshot_id=row.snapshot_id,
                 stored_feature_hash=digest(row.feature), reconstructed_feature_hash=digest(feature),
                 available_history_snapshot_count=len(raw_history))
            alpha = _relabel_alpha(row.alpha, parameters, prior_alpha) if regime_conf.use_statistical_alpha else None
            if alpha is not None:
                prior_alpha.append(alpha)
            regime = classify_regime(row.feature_id, feature, previous_feature, regime_conf, alpha)
            regime = regime.model_copy(update={"policy_hash": policy.policy_hash, "research_run_id": manifest.run_id,
                                               "execution_mode": policy.execution_mode,
                                               "persistence_identity": persistence_identity(snapshot, policy.policy_hash,
                                                   registered["calculation_mode"], source, alpha)})
            candidates = generate_candidates(snapshot, feature, regime, row.snapshot_id, strategy, enforce_freshness=False)
            if parameters.dte_bucket is not None:
                selected = [c for c in candidates.candidates if c.dte_bucket == parameters.dte_bucket]
                candidates = candidates.model_copy(update={"candidates": selected, "candidate_count": len(selected),
                    "eligible": bool(selected), "strategy_type": selected[0].strategy_type if selected else StrategyType.NONE})
        if open_trade is not None:
            if (day != open_trade["entry_at"].date() or local_time > integrity.session_end
                    or feature is None or gap is None or gap > integrity.max_path_gap_seconds):
                unresolved(open_trade, at, "SESSION_END_UNRESOLVED" if day != open_trade["entry_at"].date() or local_time > integrity.session_end else
                           "MISSING_FEATURE_PATH" if feature is None else "MISSING_EXPECTED_PATH_OBSERVATION")
                open_trade = None
            else:
                pair, reason, details = execution_pair(snapshot, open_trade["candidate"], entry=False,
                                                        lots=quantity, policy=integrity.quote_policy, evaluated_at=at)
                emit("EXIT_ATTEMPT", at, open_trade, validity_reason=reason,
                     exact_quotes=[q.payload() for q in details], fill_method="CONTEMPORANEOUS_OBSERVED_BOOK_LIQUIDATION")
                if pair is None:
                    unresolved(open_trade, at, reason)
                    open_trade = None
                else:
                    short, long = pair
                    pnl = open_trade["entry_credit"]-(short.fill_price-long.fill_price)
                    open_trade["marks"].append(pnl)
                    marked_equity.append(gross_realized + pnl * open_trade["lot_size"] * quantity)
                    emit("PATH_MARK", at, open_trade, gross_points_per_unit=pnl,
                         sampled_MAE_points_per_unit=min(open_trade["marks"]), sampled_MFE_points_per_unit=max(open_trade["marks"]))
                    # Same observed book supplies both the trigger mark and liquidation.
                    trigger_regime = None if regime is None else SimpleNamespace(regime=getattr(regime.market_bias, "value", regime.regime))
                    trade_proxy = SimpleNamespace(entry_credit=open_trade["entry_credit"], strategy_type=open_trade["candidate"].strategy_type.value,
                                                  structural_reference=open_trade["candidate"].support_or_resistance_reference)
                    mark = SimpleNamespace(pnl_per_unit=pnl, timestamp=snapshot.timestamp_ist, spot=snapshot.nifty_spot)
                    reason = exit_reason(trade_proxy, mark, trigger_regime, shadow)
                    if reason:
                        quote_entry = open_trade["entry_execution"]
                        accounting = cost_schedule.calculate(day=day, lot_size=open_trade["lot_size"], lots=quantity,
                            entry_short=quote_entry["short"]["fill_price"], entry_long=quote_entry["long"]["fill_price"],
                            exit_short=short.fill_price, exit_long=long.fill_price)
                        depth = "EXECUTABLE_DEPTH" if (open_trade["depth_status"] == "EXECUTABLE_DEPTH"
                            and all(q.depth_status == "EXECUTABLE_DEPTH" for q in pair)) else "UNKNOWN_DEPTH_SIMULATION"
                        elapsed = (snapshot.timestamp_ist-open_trade["entry_observation_at"]).total_seconds()
                        expected = int(round(elapsed/integrity.expected_interval_seconds))+1
                        observed = len(open_trade["marks"])
                        open_trade.update(exit_reason=reason, exit_at=at, depth_status=depth,
                            exit_execution={"timestamp": at, "short": short.payload(), "long": long.payload(),
                                            "fill_method": "CONTEMPORANEOUS_OBSERVED_BOOK_LIQUIDATION"},
                            final_outcome=accounting, cost_completeness=accounting["cost_completeness"],
                            sampled_MAE_points_per_unit=min(open_trade["marks"]), sampled_MFE_points_per_unit=max(open_trade["marks"]),
                            expected_observations=expected, observed_observations=observed,
                            missing_observations=max(0, expected-observed), path_coverage_ratio=min(1, observed/expected))
                        gross_realized += accounting["gross_rupees"]
                        marked_equity.append(gross_realized)
                        state.keys[day].discard(open_trade["fingerprint"])
                        if accounting["net_rupees"] is None:
                            state.incomplete.add(day)
                        else:
                            state.realized[day] += accounting["net_rupees"]
                        finish(open_trade, at, "CLOSED", None, "EVALUABLE")
                        open_trade = None
        if pending is not None:
            attempt, pending = pending, None
            attempt["fill_status"] = "ATTEMPTED"
            attempt.update(actual_next_observation_timestamp=snapshot.timestamp_ist,
                           decision_to_fill_delay_seconds=(at-attempt["decision_timestamp"]).total_seconds())
            reason = ("CROSS_SESSION_FILL" if day != attempt["decision_timestamp"].date() else
                      "DECISION_TO_FILL_TTL_EXCEEDED" if attempt["decision_to_fill_delay_seconds"] > integrity.max_fill_delay_seconds else
                      "OFF_CADENCE_NEXT_OBSERVATION" if abs((snapshot.timestamp_ist-attempt["expected_next_observation_timestamp"]).total_seconds()) > integrity.interval_tolerance_seconds else
                      "MISSING_FILL_FEATURE" if feature is None else None)
            pair, pair_reason, detail = execution_pair(snapshot, attempt["candidate"], entry=True,
                                                       lots=quantity, policy=integrity.quote_policy, evaluated_at=at)
            reason = reason or (pair_reason if pair is None else None)
            emit("FILL_ATTEMPT", at, attempt, expected_next_observation=attempt["expected_next_observation_timestamp"],
                 actual_next_observation=snapshot.timestamp_ist, delay_seconds=attempt["decision_to_fill_delay_seconds"],
                 reason=reason, quotes=[q.payload() for q in detail])
            fresh = None
            if reason is None:
                original = attempt["candidate"]
                fresh = next((c for c in candidates.candidates
                    if ContractIdentity.of(c.short_leg) == ContractIdentity.of(original.short_leg)
                    and ContractIdentity.of(c.long_leg) == ContractIdentity.of(original.long_leg)
                    and c.strategy_family == original.strategy_family), None)
                if fresh is None:
                    reason = "FILL_TIME_POLICY_NO_LONGER_ELIGIBLE"
                elif not entry_window:
                    reason = "FILL_OUTSIDE_ENTRY_WINDOW"
                else:
                    selected = candidates.model_copy(update={"candidates": [fresh], "candidate_count": 1, "eligible": True,
                                                            "strategy_type": fresh.strategy_type})
                    rechecked = evaluate_risk(selected, None, snapshot, feature, regime, risk, EvaluationContext.HISTORICAL,
                        state, provider, prior_regimes=prior_regimes, prior_candidate_keys=prior_keys, evaluated_at=at)
                    attempt["fill_time_validation"] = rechecked.model_dump(mode="python")
                    emit("FILL_TIME_VALIDATION", at, attempt, validation=attempt["fill_time_validation"])
                    if not rechecked.approved_count:
                        reason = "FILL_TIME_HARD_RISK_REJECTED"
            if reason:
                invalid_reasons = {"NONFINITE_EXECUTABLE_PRICE", "NONPOSITIVE_EXECUTABLE_PRICE", "CROSSED_BOOK",
                                   "INVALID_TICK_PRICE", "EXACT_CONTRACT_AMBIGUOUS", "INVALID_DEPTH",
                                   "INVALID_EXACT_CONTRACT_IDENTITY", "INVALID_EXECUTABLE_PAYOFF"}
                finish(attempt, at, "INVALID_FILL" if reason in invalid_reasons else "NO_FILL", reason)
            else:
                short, long = pair
                credit = short.fill_price-long.fill_price
                if not 0 < credit < fresh.spread_width:
                    finish(attempt, at, "INVALID_FILL", "INVALID_EXECUTABLE_PAYOFF")
                else:
                    fingerprint = strategy_fingerprint(fresh, day)
                    state.entries[day] += 1
                    state.keys[day].add(fingerprint)
                    attempt.update(state="OPEN", entry_at=at, entry_observation_at=snapshot.timestamp_ist,
                        fill_status="FILLED", candidate=fresh, entry_time_candidate=fresh.model_dump(mode="python"),
                        gross_max_loss_rupees_for_quantity=fresh.max_loss_per_lot*quantity,
                        entry_credit=credit, lot_size=snapshot.lot_size, requested_lots=quantity,
                        requested_units=snapshot.lot_size*quantity, fingerprint=fingerprint, marks=[0.0],
                        depth_status="EXECUTABLE_DEPTH" if all(q.depth_status == "EXECUTABLE_DEPTH" for q in pair) else "UNKNOWN_DEPTH_SIMULATION",
                        cost_completeness="NET_COMPLETE" if cost_schedule.complete_at(day) else "GROSS_ONLY",
                        entry_execution={"timestamp": at, "short": short.payload(), "long": long.payload(),
                                         "fill_method": "NEXT_OBSERVATION_SIMULATION"})
                    attempt.update(_buckets(fresh))
                    emit("ENTRY_EXECUTION", at, attempt, execution=attempt["entry_execution"], fill_time_candidate=fresh,
                         entry_state={key: value for key, value in attempt.items() if key != "candidate"})
                    open_trade = attempt
        if entry_window:
            if day in state.incomplete:
                attempt = decision(snapshot, at, state_name="BLOCKED_INCOMPLETE_RISK_STATE", reason="UNRESOLVED_OR_GROSS_ONLY_MONETARY_STATE")
                finish(attempt, at, attempt["state"], attempt["missing_reason"])
            elif open_trade is None and pending is None:
                if day >= snapshot.expiry:
                    attempt = decision(snapshot, at, reason="ZERO_DTE_EXCLUDED")
                    finish(attempt, at, "RESTRICTED", "ZERO_DTE_EXCLUDED")
                elif feature is None:
                    attempt = decision(snapshot, at, reason="MISSING_FEATURE_OBSERVATION")
                    finish(attempt, at, "NOT_EVALUABLE", "MISSING_FEATURE_OBSERVATION")
                elif expected_move(snapshot, feature, expiry_context(snapshot, policy)["fractional_time_to_expiry"],
                                   source, quality_policy=policy) is None:
                    attempt = decision(snapshot, at, reason="NOT_EVALUABLE_EXPECTED_MOVE")
                    finish(attempt, at, "NOT_EVALUABLE", "NOT_EVALUABLE_EXPECTED_MOVE")
                else:
                    evaluated = evaluate_risk(candidates, None, snapshot, feature, regime, risk, EvaluationContext.HISTORICAL,
                        state, provider, prior_regimes=prior_regimes, prior_candidate_keys=prior_keys, evaluated_at=at)
                    by_id = {candidate.candidate_id: candidate for candidate in candidates.candidates}
                    selected = evaluated.best_approved_candidate
                    for risk_decision in evaluated.decisions:
                        candidate = by_id.get(risk_decision.candidate_reference)
                        approved = risk_decision.decision == RiskDecisionType.APPROVED
                        attempt = decision(snapshot, at, candidate, approved,
                                           "PENDING" if approved else "REJECTED", risk_decision=risk_decision)
                        if approved and candidate is not None and candidate.candidate_id == selected:
                            pending = attempt
                        else:
                            finish(attempt, at, "APPROVED_NOT_SELECTED" if approved else "REJECTED",
                                   None if approved else ",".join(risk_decision.reason_codes))
        if regime is not None:
            prior_regimes.insert(0, regime)
            prior_keys.insert(0, {strategy_fingerprint(c, day) for c in candidates.candidates})
        else:
            prior_regimes, prior_keys = [], []
        previous_raw, previous_feature, previous_at = snapshot, feature, snapshot.timestamp_ist
        raw_history.append(snapshot)
    last_at = available_at(rows[-1].snapshot)
    if pending is not None:
        finish(pending, last_at, "NO_FILL", "END_OF_REGISTERED_PATH_NO_FILL")
    if open_trade is not None:
        unresolved(open_trade, last_at, "SESSION_END_UNRESOLVED")
    coverage = path_coverage(rows, sessions, integrity)
    summary = replay_analytics(attempts, events, coverage, marked_equity)
    summary.update(run_id=manifest.run_id, manifest_hash=manifest.manifest_hash, policy_hash=policy.policy_hash,
                   split=split, expected_move_source_policy=source, final_test_exposure=exposure,
                   attempts=[{key: value for key, value in item.items() if key != "candidate"} for item in attempts])
    emit("RUN_SUMMARY", last_at, summary={key: value for key, value in summary.items() if key != "attempts"})
    ledger.finish(summary)
    return summary
