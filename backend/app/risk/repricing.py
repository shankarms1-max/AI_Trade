"""Risk independently reconstructs economics from authoritative exact raw legs."""
from dataclasses import dataclass
from math import isclose
from app.research.quotes import ContractIdentity, QuotePolicy, exact_contract, finite, policy_quote_contract, validate_book
from app.strategy.models import PricingBasis
from app.strategy.policy import LOGIC_VERSION


@dataclass(frozen=True)
class IndependentReprice:
    valid: bool
    reasons: tuple[str, ...]
    width: float | None = None
    credit: float | None = None
    gross_max_loss: float | None = None
    gross_max_profit: float | None = None
    credit_to_width: float | None = None
    credit_to_max_loss: float | None = None
    gross_loss_per_lot: float | None = None
    gross_profit_per_lot: float | None = None
    cost_estimate_complete: bool = False
    estimated_cost_points_per_unit: float | None = None


def independent_reprice(candidate, candidate_set, snapshot, regime, config, evaluated_at=None):
    policy = config.credit_spread_policy
    reasons = []
    if not (candidate.strategy_logic_version == candidate_set.strategy_logic_version
            == regime.strategy_logic_version == LOGIC_VERSION
            and candidate.strategy_version == candidate_set.strategy_version == regime.regime_version == LOGIC_VERSION):
        reasons.append("POLICY_VERSION_MISMATCH")
    if policy.replay_integrity_enabled and not (
        policy.policy_hash and candidate.policy_hash == candidate_set.policy_hash == regime.policy_hash == policy.policy_hash
        and candidate.research_run_id == candidate_set.research_run_id == regime.research_run_id == policy.research_run_id
        and candidate.execution_mode == candidate_set.execution_mode == regime.execution_mode == policy.execution_mode):
        reasons.append("POLICY_VERSION_MISMATCH")
    raw = []
    for leg in (candidate.short_leg, candidate.long_leg):
        contract, reason = exact_contract(snapshot, ContractIdentity.of(leg))
        if contract is None:
            reasons.append(reason)
        raw.append(contract)
    if any(item is None for item in raw):
        return IndependentReprice(False, tuple(dict.fromkeys(reasons)))
    short, long = raw
    if candidate.pricing_basis == PricingBasis.BID_ASK:
        quote_policy = QuotePolicy(
            source_timestamp_required=policy.replay_integrity_enabled,
            max_source_age_seconds=policy.source_quote_max_age_seconds if policy.replay_integrity_enabled else config.max_snapshot_age_seconds,
            max_spread_percent=config.max_bid_ask_spread_pct)
        if policy.replay_integrity_enabled:
            quote_policy = policy_quote_contract(policy, max_spread_percent=config.max_bid_ask_spread_pct)
        for contract in raw:
            quality = validate_book(contract, snapshot.timestamp_ist, quote_policy,
                                    evaluated_at=evaluated_at if policy.replay_integrity_enabled else None)
            if not quality.valid:
                reasons.append(quality.reason)
        if policy.replay_integrity_enabled and all(item.source_market_timestamp for item in raw):
            from app.research.quotes import aware
            if (all(aware(item.source_market_timestamp) for item in raw)
                    and abs((short.source_market_timestamp-long.source_market_timestamp).total_seconds()) > policy.quote_max_leg_skew_seconds):
                reasons.append("LEG_TIME_SKEW")
        short_price, long_price = short.bid, long.ask
        if any(leg.bid != row.bid or leg.ask != row.ask
               for leg, row in zip((candidate.short_leg, candidate.long_leg), raw)):
            reasons.append("INDEPENDENT_REPRICE_MISMATCH")
    elif candidate.pricing_basis == PricingBasis.LTP_ESTIMATE and not policy.replay_integrity_enabled:
        short_price, long_price = short.ltp, long.ltp
        if candidate.short_leg.ltp != short.ltp or candidate.long_leg.ltp != long.ltp:
            reasons.append("INDEPENDENT_REPRICE_MISMATCH")
    else:
        return IndependentReprice(False, tuple(dict.fromkeys([*reasons, "PRICING_BASIS_MISMATCH"])))
    if not finite(short_price) or not finite(long_price):
        return IndependentReprice(False, tuple(dict.fromkeys([*reasons, "NONFINITE_EXECUTABLE_PRICE"])))
    if short_price <= 0 or long_price <= 0:
        return IndependentReprice(False, tuple(dict.fromkeys([*reasons, "NONPOSITIVE_EXECUTABLE_PRICE"])))
    width = abs(short.strike - long.strike)
    credit = short_price - long_price
    loss = width - credit
    if not all(finite(v) and v > 0 for v in (width, credit, loss)):
        return IndependentReprice(False, tuple(dict.fromkeys([*reasons, "INVALID_EXECUTABLE_PAYOFF"])))
    lot = snapshot.lot_size
    loss_lot = None if lot is None else loss * lot
    profit_lot = None if lot is None else credit * lot
    values = {"spread_width": width, "net_credit": credit, "max_profit": credit,
              "max_loss": loss, "credit_to_width_ratio": credit/width,
              "max_profit_per_lot": profit_lot, "max_loss_per_lot": loss_lot,
              "lot_size": lot, "breakeven": short.strike + (credit if short.option_type.value == "CE" else -credit)}
    optional = {"spread_width_points": width, "net_credit_per_unit": credit, "gross_credit": credit,
                "max_loss_per_unit": loss, "credit_to_max_loss": credit/loss}
    values.update({key: value for key, value in optional.items() if getattr(candidate, key) is not None})
    complete, cost_points = False, None
    if policy.replay_integrity_enabled:
        from app.research.costs import CostSchedule, estimated_cost
        costs = estimated_cost(policy.cost_schedule or CostSchedule(), snapshot, short_price, long_price,
                               lots=policy.requested_lots)
        complete = costs["cost_completeness"] == "NET_COMPLETE"
        cost_points = costs["total_cost_rupees"] / (lot*policy.requested_lots) if complete else None
        if candidate.cost_estimate_complete != complete:
            reasons.append("COST_COMPLETENESS_MISMATCH")
        values.update(estimated_cost=cost_points, net_credit_after_cost=credit-cost_points if complete else None)
    for key, derived in values.items():
        stored = getattr(candidate, key)
        if derived is None:
            matches = stored is None
        else:
            matches = finite(stored) and isclose(stored, derived, rel_tol=1e-9, abs_tol=1e-6)
        if not matches:
            reasons.append("NONFINITE_CANDIDATE_ECONOMICS" if stored is not None and not finite(stored)
                           else "INDEPENDENT_REPRICE_MISMATCH")
    return IndependentReprice(not reasons, tuple(dict.fromkeys(reasons)), width, credit, loss, credit,
                              credit/width, credit/loss, loss_lot, profit_lot, complete, cost_points)
