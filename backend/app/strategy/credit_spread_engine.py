"""Flagged Phase 14.2 selection; existing pricing/liquidity/payoff rules reused."""

from datetime import datetime
from app.strategy.policy import LOGIC_VERSION, STRENGTH_RANK
from app.strategy.economics import (
    expiry_context,
    expected_move,
    required_buffer,
    side_safety,
    candidate_economics,
)
from app.strategy.models import (
    CreditSpreadCandidate,
    StrategyCandidateSet,
    StrategyType,
    LiquidityMetrics,
    PricingBasis,
)
from app.strategy.economics_models import StrategyFamily
from app.strategy.strike_selection import structural_reference
from app.strategy.liquidity import check_liquidity, bid_ask_spread_pct
from app.strategy.payoff import calculate_payoff
from app.strategy.pricing import price_spread
from app.strategy.construction import (construction_key, ordinal_score,
                                       defined_risk_rejection, phase14_quote_failure, phase14_short_quality)


def generate_credit_spread_candidates(
    snapshot, feature, regime, regime_id, config, *, enforce_freshness=False, now=None
):
    from app.strategy.candidate_engine import (
        _none_result,
        _leg,
        _delta_allowed,
        QUALITY_RANK,
        IST,
    )

    p = config.credit_spread_policy
    safety = {}

    def none(*reasons):
        result = _none_result(
            feature.snapshot_id, regime_id, regime.regime.value, list(reasons)
        )
        return result.model_copy(
            update=dict(
                strategy_version=LOGIC_VERSION,
                strategy_logic_version=LOGIC_VERSION,
                strategy_family=StrategyFamily.NO_TRADE,
                policy_hash=p.policy_hash,
                research_run_id=p.research_run_id,
                execution_mode=p.execution_mode,
                side_safety=safety,
                created_at=snapshot.timestamp_ist,
            )
        )

    if regime.strategy_logic_version != LOGIC_VERSION or regime.market_bias is None:
        return none("PHASE14_2_MARKET_STATE_REQUIRED")
    if (
        regime.market_bias.value in {"CONFLICT", "INSUFFICIENT"}
        or regime.data_quality_state != "VALID"
    ):
        return none("CONFLICT_OR_INSUFFICIENT_DATA")
    if (
        QUALITY_RANK[regime.evidence_quality.value]
        < QUALITY_RANK[config.min_evidence_quality]
    ):
        return none("EVIDENCE_QUALITY_TOO_LOW")
    from app.research.oi import static_oi_usable
    if not static_oi_usable(feature):
        return none("STATIC_OI_UNUSABLE")
    if (
        snapshot.expiry != feature.expiry
        or snapshot.nifty_spot != feature.spot
        or not snapshot.options
    ):
        return none("OPTION_CHAIN_UNAVAILABLE")
    if (
        snapshot.timestamp_ist != feature.timestamp
        or regime.timestamp != feature.timestamp
        or regime.snapshot_id != feature.snapshot_id
    ):
        return none("SNAPSHOT_CONTEXT_MISMATCH")
    if enforce_freshness:
        age = ((now or datetime.now(IST)) - snapshot.timestamp_ist).total_seconds()
        if age < 0 or age > config.max_snapshot_age_seconds:
            return none("SNAPSHOT_STALE")
    expiry = expiry_context(snapshot, p)
    if expiry["fractional_time_to_expiry"] <= 0:
        return none("EXPIRY_ELAPSED")
    if feature.volatility_features.vix_regime is None:
        return none("VOLATILITY_CONTEXT_UNAVAILABLE")
    if (
        config.volatility_buffer_enabled
        and config.no_candidate_on_high_vix
        and feature.volatility_features.vix_regime == "HIGH"
    ):
        return none("VOLATILITY_POLICY_NO_CANDIDATE")
    move = expected_move(
        snapshot,
        feature,
        expiry["fractional_time_to_expiry"],
        p.expected_move_source,
        config.max_snapshot_age_seconds,
        quality_policy=p if p.replay_integrity_enabled else None,
    )
    if move is None:
        return none("EXPECTED_MOVE_UNAVAILABLE")
    safety = side_safety(feature, move, p)
    bias = regime.market_bias.value
    strength = regime.directional_strength.value
    eligible = regime.strategy_family_eligibility.value
    choices = []
    if (
        eligible in {"BOTH", "DIRECTIONAL_ONLY"}
        and bias in {"BULLISH", "BEARISH"}
        and strength in {"STRONG", "MODERATE"}
    ):
        choices.append((StrategyFamily.DIRECTIONAL_CREDIT_SPREAD, bias))
    if p.theta_carry_enabled and eligible in {"BOTH", "THETA_CARRY_ONLY"}:
        # Even with bias, a weak/neutral carry proposal must have observed asymmetry.
        side = (
            bias
            if strength in {"STRONG", "MODERATE"} and bias in {"BULLISH", "BEARISH"}
            else safety["selected_bias"]
        )
        if side:
            opposing = (
                regime.statistical_alpha_bias in {"BULLISH", "BEARISH"}
                and regime.statistical_alpha_bias != side
            )
            opposing_strength = STRENGTH_RANK.get(
                regime.statistical_alpha_strength or "NONE", 0
            )
            if (
                not opposing
                or opposing_strength
                <= STRENGTH_RANK[p.theta_carry_max_opposing_alpha_strength]
            ):
                choices.append((StrategyFamily.THETA_CARRY_CREDIT_SPREAD, side))
    if not choices:
        return none("NO_ELIGIBLE_STRATEGY_FAMILY_OR_SAFER_SIDE")
    candidates = []
    rejections = set()
    for family, side in choices:
        option_type = "PE" if side == "BULLISH" else "CE"
        strategy = (
            StrategyType.BULL_PUT_SPREAD
            if side == "BULLISH"
            else StrategyType.BEAR_CALL_SPREAD
        )
        reference = structural_reference(feature, side)
        if reference is None and config.require_structure_reference:
            rejections.add("NO_STRUCTURE_REFERENCE")
            continue
        buffer = required_buffer(
            config,
            strength,
            family.value,
            feature.volatility_features.vix_regime,
            expiry["fractional_time_to_expiry"],
            snapshot.nifty_spot,
        )
        contracts = [
            c
            for c in snapshot.options
            if c.option_type.value == option_type
            and c.expiry == snapshot.expiry
            and c.strike > 0
            and c.trading_symbol.strip()
        ]
        # Ambiguous duplicate contract records are excluded, never selected by order.
        by_strike = {
            c.strike: c
            for c in contracts
            if sum(o.strike == c.strike for o in contracts) == 1
        }
        for short in by_strike.values():
            distance = (
                snapshot.nifty_spot - short.strike
                if side == "BULLISH"
                else short.strike - snapshot.nifty_spot
            )
            if distance <= 0 or distance < buffer:
                rejections.add("SHORT_STRIKE_BUFFER_NOT_MET")
                continue
            if reference is not None and (
                short.strike > reference
                if side == "BULLISH"
                else short.strike < reference
            ):
                rejections.add("SHORT_STRIKE_NOT_BEYOND_STRUCTURE")
                continue
            if (
                p.expected_move_min_distance_units is not None
                and distance / move["expected_move_points"]
                < p.expected_move_min_distance_units
            ):
                rejections.add("EXPECTED_MOVE_DISTANCE_NOT_MET")
                continue
            if not _delta_allowed(short, config):
                rejections.add("DELTA_OUT_OF_RANGE")
                continue
            # Inspect actual captured hedges for this short. Configured widths
            # remain an allowlist/safety boundary, never a ranking objective.
            hedges = [long for long in by_strike.values()
                      if (long.strike < short.strike if side == "BULLISH" else long.strike > short.strike)
                      and abs(long.strike-short.strike) in config.allowed_spread_widths]
            if not hedges:
                rejections.add("NO_VALID_LONG_HEDGE")
            for long in hedges:
                failure = phase14_quote_failure(snapshot, short, long, config)
                if failure:
                    rejections.update(("HEDGE_REJECTED_EXECUTION", failure))
                    continue
                # Same contract and observed quote freshness; timestamps missing stay explicitly estimated.
                if p.replay_integrity_enabled:
                    from app.research.quotes import information_time, policy_quote_contract, validate_book
                    quote_policy = policy_quote_contract(p, max_spread_percent=config.max_bid_ask_spread_pct)
                    qualities = [validate_book(c, snapshot.timestamp_ist, quote_policy, evaluated_at=information_time(snapshot)) for c in (short, long)]
                    if not all(item.valid for item in qualities):
                        rejections.add("HEDGE_REJECTED_EXECUTION")
                        rejections.update(item.reason for item in qualities if not item.valid)
                        continue
                    if abs((short.source_market_timestamp-long.source_market_timestamp).total_seconds()) > p.quote_max_leg_skew_seconds:
                        rejections.add("LEG_TIME_SKEW")
                        continue
                if any(
                    c.source_market_timestamp is not None
                    and not 0
                    <= (
                        snapshot.timestamp_ist - c.source_market_timestamp
                    ).total_seconds()
                    <= config.max_snapshot_age_seconds
                    for c in (short, long)
                ):
                    rejections.add("STALE_LEG_QUOTE")
                    continue
                liquidity = check_liquidity(
                    short, long, config, feature.data_quality.intraday_volume_usable
                )
                if not liquidity.eligible:
                    rejections.add("HEDGE_REJECTED_EXECUTION")
                    rejections.update(liquidity.reason_codes)
                    continue
                price = price_spread(short, long)
                if price is None or price.short_price < config.min_short_premium:
                    rejections.add("INSUFFICIENT_CREDIT")
                    continue
                payoff = calculate_payoff(
                    strategy, short.strike, long.strike, price.net_credit
                )
                if payoff is None:
                    rejections.add("INVALID_DEFINED_RISK_PAYOFF")
                    continue
                risk_failure = defined_risk_rejection(payoff.width, price.net_credit,
                    snapshot.lot_size, config.requested_lots,
                    max_loss=config.max_defined_loss_rupees,
                    max_capital=config.max_defined_capital_rupees,
                    max_width=config.max_defined_width)
                if risk_failure:
                    rejections.add(risk_failure)
                    continue
                if (
                    price.net_credit < config.min_net_credit
                    or price.net_credit / payoff.width
                    < config.min_credit_to_width_ratio
                ):
                    rejections.add("INSUFFICIENT_CREDIT")
                    continue
                economics = candidate_economics(
                    snapshot,
                    feature,
                    regime,
                    short,
                    long,
                    price,
                    payoff,
                    reference,
                    family,
                    side,
                    config,
                    expiry,
                    move,
                    safety,
                    buffer,
                )
                min_survival = (
                    p.theta_min_survival
                    if family == StrategyFamily.THETA_CARRY_CREDIT_SPREAD
                    else (
                        p.strong_min_survival
                        if strength == "STRONG"
                        else p.moderate_min_survival
                    )
                )
                min_carry = (
                    p.theta_min_carry
                    if family == StrategyFamily.THETA_CARRY_CREDIT_SPREAD
                    else (
                        p.strong_min_carry
                        if strength == "STRONG"
                        else p.moderate_min_carry
                    )
                )
                if (
                    economics["survival_score"] < min_survival
                    or economics["carry_score"] < min_carry
                    or (economics["net_credit_after_cost"] is not None and economics["net_credit_after_cost"] <= 0)
                    or economics["gamma_risk_state"] == "EXTREME"
                ):
                    rejections.add("SURVIVAL_CARRY_OR_GAMMA_POLICY")
                    continue
                warnings = list(liquidity.warnings) + ["UNCALIBRATED_RESEARCH_PROXIES"]
                if not economics["cost_estimate_complete"]:
                    warnings.append("COST_ESTIMATE_INCOMPLETE")
                if price.basis == PricingBasis.LTP_ESTIMATE:
                    warnings.append("PRICING_USING_LTP_ESTIMATE")
                if short.delta is None:
                    warnings.append("DELTA_UNAVAILABLE")
                lot = snapshot.lot_size
                candidates.append(
                    CreditSpreadCandidate(
                        **economics,
                        candidate_id=f"{LOGIC_VERSION}:{feature.snapshot_id}:{family.value}:{strategy.value}:{short.strike:g}:{long.strike:g}",
                        market_snapshot_id=feature.snapshot_id,
                        regime_snapshot_id=regime_id,
                        strategy_version=LOGIC_VERSION,
                        strategy_type=strategy,
                        expiry=snapshot.expiry,
                        spot=snapshot.nifty_spot,
                        atm_strike=snapshot.atm_strike,
                        short_leg=_leg(short, "SELL"),
                        long_leg=_leg(long, "BUY"),
                        spread_width=payoff.width,
                        net_credit=price.net_credit,
                        credit_to_width_ratio=price.net_credit / payoff.width,
                        max_profit=payoff.max_profit,
                        max_loss=payoff.max_loss,
                        breakeven=payoff.breakeven,
                        max_profit_per_lot=(
                            None if lot is None else price.net_credit * lot
                        ),
                        max_loss_per_lot=None if lot is None else payoff.max_loss * lot,
                        lot_size=lot,
                        short_leg_distance_from_spot=distance,
                        short_leg_distance_pct=distance / snapshot.nifty_spot * 100,
                        support_or_resistance_reference=reference,
                        liquidity_metrics=LiquidityMetrics(
                            short_open_interest=short.open_interest,
                            long_open_interest=long.open_interest,
                            short_volume=short.volume,
                            long_volume=long.volume,
                            short_bid_ask_spread_pct=bid_ask_spread_pct(short),
                            long_bid_ask_spread_pct=bid_ask_spread_pct(long),
                            volume_usable=feature.data_quality.intraday_volume_usable,
                        ),
                        pricing_basis=price.basis,
                        construction_method="SHORT_LEG_FIRST_V1",
                        construction_evidence={"short_sell_price": price.short_price,
                            "hedge_protection_cost": price.long_price,
                            "prior_vertical_score": economics["selection_score"]},
                        reason_codes=["CANDIDATE_ELIGIBLE", "SHORT_LEG_SELECTED",
                                      "HEDGE_SELECTED_FOR_PREMIUM_RETENTION"],
                        warnings=warnings,
                        created_at=snapshot.timestamp_ist,
                    )
                )
    premium_reference = max((c.construction_evidence["short_sell_price"] for c in candidates), default=1)
    candidates = [c.model_copy(update={"construction_evidence": {**c.construction_evidence,
                  **phase14_short_quality(c, premium_reference, config)}}) for c in candidates]
    candidates.sort(
        key=lambda c: construction_key(short_quality=c.construction_evidence["short_quality_score"],
            retention=c.premium_retention_ratio,
            width=c.spread_width, identity=(-c.construction_evidence["prior_vertical_score"],
                c.strategy_family.value, c.candidate_id))
    )
    # One physical spread has one duplicate-position fingerprint in the risk ledger.
    # Keep the best family interpretation; separate family-mode replays compare policies.
    unique = {}
    for candidate in candidates:
        key = (
            candidate.strategy_type,
            candidate.expiry,
            candidate.short_leg.strike,
            candidate.long_leg.strike,
        )
        unique.setdefault(key, candidate)
    candidates = list(unique.values())
    candidates = [c.model_copy(update={"selection_score": ordinal_score(i, len(candidates)),
                  "construction_evidence": {**c.construction_evidence,
                      "ordinal_rank": i+1, "eligible_pair_count": len(candidates),
                      "ranking_basis": "BALANCED_SHORT_QUALITY_THEN_PREMIUM_RETENTION_V2"}})
                  for i, c in enumerate(candidates)]
    candidates = candidates[: config.max_candidates]
    if not candidates:
        return none(*sorted(rejections), "NO_ELIGIBLE_CANDIDATES")
    return StrategyCandidateSet(
        policy_hash=p.policy_hash,
        research_run_id=p.research_run_id,
        execution_mode=p.execution_mode,
        snapshot_id=feature.snapshot_id,
        regime_snapshot_id=regime_id,
        regime=regime.regime.value,
        strategy_type=candidates[0].strategy_type,
        eligible=True,
        candidates=candidates,
        candidate_count=len(candidates),
        reason_codes=["CANDIDATE_ELIGIBLE", "SHORT_LEG_SELECTED",
                      "HEDGE_SELECTED_FOR_PREMIUM_RETENTION", *sorted(rejections)],
        warnings=sorted({w for c in candidates for w in c.warnings}),
        strategy_version=LOGIC_VERSION,
        strategy_logic_version=LOGIC_VERSION,
        strategy_family=candidates[0].strategy_family,
        side_safety=safety,
        created_at=snapshot.timestamp_ist,
    )
