from datetime import datetime

from app.data.models import MarketSnapshot
from app.features.models import MarketFeatureSnapshot
from app.regime.models import RegimeResult
from app.risk.models import RiskDecision, RiskDecisionType
from app.shadow.models import (
    ShadowEntryResult,
    ShadowPricingBasis,
    ShadowTrade,
)
from app.strategy.models import PricingBasis, StrategyCandidateSet


def create_shadow_entry(
    snapshot_id: int,
    snapshot: MarketSnapshot,
    feature: MarketFeatureSnapshot,
    regime: RegimeResult,
    candidate_set: StrategyCandidateSet,
    approved_decisions: list[tuple[int, RiskDecision]],
    *,
    trades_today: int,
    open_trade_exists: bool,
    max_new_trades_per_day: int,
    allow_multiple_open_trades: bool,
) -> ShadowEntryResult:
    approved = [
        item for item in approved_decisions
        if item[1].decision == RiskDecisionType.APPROVED
    ]
    if not approved:
        return ShadowEntryResult(
            snapshot_id=snapshot_id, created=False, trade=None,
            reason_codes=["NO_APPROVED_RISK_DECISION"],
        )
    if trades_today >= max_new_trades_per_day:
        return ShadowEntryResult(
            snapshot_id=snapshot_id, created=False, trade=None,
            reason_codes=["DAILY_SHADOW_LIMIT_REACHED"],
        )
    if open_trade_exists and not allow_multiple_open_trades:
        return ShadowEntryResult(
            snapshot_id=snapshot_id, created=False, trade=None,
            reason_codes=["SHADOW_POSITION_ALREADY_OPEN"],
        )
    by_id = {item.candidate_id: item for item in candidate_set.candidates}
    eligible = [
        (record_id, decision, by_id[decision.candidate_reference])
        for record_id, decision in approved
        if decision.candidate_reference in by_id
    ]
    if not eligible:
        return ShadowEntryResult(
            snapshot_id=snapshot_id, created=False, trade=None,
            reason_codes=["NO_APPROVED_RISK_DECISION"],
        )
    risk_id, decision, candidate = max(
        eligible, key=lambda item: item[2].selection_score
    )
    if candidate.pricing_basis == PricingBasis.BID_ASK:
        short_price, long_price = candidate.short_leg.bid, candidate.long_leg.ask
        basis = ShadowPricingBasis.BID_ASK
        pricing_reason = "ENTRY_PRICING_BID_ASK"
    else:
        short_price, long_price = candidate.short_leg.ltp, candidate.long_leg.ltp
        basis = ShadowPricingBasis.LTP_ESTIMATE
        pricing_reason = "ENTRY_PRICING_LTP_ESTIMATE"
    if (
        short_price is None or long_price is None
        or short_price <= 0 or long_price < 0 or short_price - long_price <= 0
    ):
        return ShadowEntryResult(
            snapshot_id=snapshot_id, created=False, trade=None,
            reason_codes=["VALUATION_UNAVAILABLE"],
        )
    credit = short_price - long_price
    now = snapshot.timestamp_ist
    warnings = [] if snapshot.lot_size is not None else ["MISSING_LOT_SIZE"]
    trade = ShadowTrade(
        market_snapshot_id_entry=snapshot_id,
        risk_decision_id=risk_id,
        candidate_fingerprint=decision.candidate_fingerprint or candidate.candidate_id,
        strategy_type=candidate.strategy_type.value,
        expiry=candidate.expiry,
        entry_timestamp=now,
        entry_spot=snapshot.nifty_spot,
        short_leg=candidate.short_leg,
        long_leg=candidate.long_leg,
        entry_short_price=short_price,
        entry_long_price=long_price,
        entry_credit=credit,
        entry_pricing_basis=basis,
        spread_width=candidate.spread_width,
        lot_size=snapshot.lot_size,
        max_profit_per_unit=credit,
        max_loss_per_unit=candidate.spread_width - credit,
        max_profit_per_lot=None if snapshot.lot_size is None else credit * snapshot.lot_size,
        max_loss_per_lot=(None if snapshot.lot_size is None
                          else (candidate.spread_width - credit) * snapshot.lot_size),
        structural_reference=candidate.support_or_resistance_reference,
        entry_regime_confidence=regime.confidence,
        entry_evidence_quality=regime.evidence_quality.value,
        entry_vix_regime=feature.volatility_features.vix_regime,
        credit_to_width_ratio=credit / candidate.spread_width,
        source_candidate=candidate.model_dump(mode="json"),
        source_risk_decision=decision.model_dump(mode="json"),
        warnings=warnings,
        reason_codes=["SHADOW_ENTRY_CREATED", pricing_reason],
        created_at=now,
        updated_at=now,
    )
    return ShadowEntryResult(
        snapshot_id=snapshot_id, created=True, trade=trade,
        reason_codes=["SHADOW_ENTRY_CREATED", pricing_reason],
    )
