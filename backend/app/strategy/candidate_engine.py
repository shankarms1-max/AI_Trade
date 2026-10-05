from dataclasses import dataclass
from app.strategy.policy import CreditSpreadPolicy
from datetime import datetime
from math import log1p
from zoneinfo import ZoneInfo

from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.features.models import MarketFeatureSnapshot
from app.regime.models import RegimeResult
from app.strategy.liquidity import bid_ask_spread_pct, check_liquidity
from app.strategy.models import (
    CandidateLeg,
    CreditSpreadCandidate,
    LiquidityMetrics,
    PricingBasis,
    StrategyCandidateSet,
    StrategyType,
)
from app.strategy.payoff import calculate_payoff
from app.strategy.pricing import price_spread
from app.strategy.strike_selection import structural_reference

IST = ZoneInfo("Asia/Kolkata")
QUALITY_RANK = {"INSUFFICIENT": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


@dataclass(frozen=True)
class StrategyConfig:
    credit_spread_policy: CreditSpreadPolicy = CreditSpreadPolicy()
    min_regime_confidence: float = 60
    min_evidence_quality: str = "MEDIUM"
    require_structure_reference: bool = True
    min_short_distance_points: float = 100
    min_short_distance_pct: float = 0.25
    allowed_spread_widths: tuple[float, ...] = (50, 100, 150, 200)
    min_short_premium: float = 1
    min_net_credit: float = 1
    min_credit_to_width_ratio: float = 0.03
    min_short_open_interest: int = 100
    min_long_open_interest: int = 100
    min_short_volume: int = 1
    min_long_volume: int = 1
    require_usable_volume: bool = False
    max_bid_ask_spread_pct: float = 30
    short_delta_min_abs: float | None = None
    short_delta_max_abs: float | None = None
    max_candidates: int = 5
    max_snapshot_age_seconds: int = 600
    volatility_buffer_enabled: bool = False
    vix_low_distance_multiplier: float = 1.0
    vix_normal_distance_multiplier: float = 1.0
    vix_elevated_distance_multiplier: float = 1.25
    vix_high_distance_multiplier: float = 1.5
    no_candidate_on_high_vix: bool = False


def _none_result(
    snapshot_id: int,
    regime_id: int,
    regime: str,
    reasons: list[str],
    warnings: list[str] | None = None,
) -> StrategyCandidateSet:
    return StrategyCandidateSet(
        snapshot_id=snapshot_id,
        regime_snapshot_id=regime_id,
        regime=regime,
        strategy_type=StrategyType.NONE,
        eligible=False,
        candidates=[],
        candidate_count=0,
        reason_codes=list(dict.fromkeys(reasons)),
        warnings=list(dict.fromkeys(warnings or [])),
        created_at=datetime.now(IST),
    )


def _leg(contract: OptionContractSnapshot, action: str) -> CandidateLeg:
    return CandidateLeg(
        action=action,
        option_type=contract.option_type.value,
        strike=contract.strike,
        trading_symbol=contract.trading_symbol,
        instrument_token=contract.instrument_token,
        ltp=contract.ltp,
        open_interest=contract.open_interest,
        volume=contract.volume,
        bid=contract.bid,
        ask=contract.ask,
        delta=contract.delta,
        expiry=contract.expiry,
    )


def _delta_allowed(contract: OptionContractSnapshot, config: StrategyConfig) -> bool:
    if contract.delta is None:
        return True
    value = abs(contract.delta)
    if config.short_delta_min_abs is not None and value < config.short_delta_min_abs:
        return False
    if config.short_delta_max_abs is not None and value > config.short_delta_max_abs:
        return False
    return True


def _score(
    reference_gap: float,
    distance: float,
    width: float,
    credit_ratio: float,
    short_oi: int,
    long_oi: int,
    pricing_basis: PricingBasis,
    regime_confidence: float,
    config: StrategyConfig,
    minimum_distance: float | None = None,
) -> float:
    structure = min(1.0, reference_gap / max(width, 1))
    credit = min(1.0, credit_ratio / max(config.min_credit_to_width_ratio * 3, 0.01))
    oi_target = max(config.min_short_open_interest, config.min_long_open_interest, 1)
    liquidity = min(1.0, log1p(min(short_oi, long_oi)) / log1p(oi_target * 10))
    if pricing_basis == PricingBasis.LTP_ESTIMATE:
        liquidity *= 0.75
    buffer = min(1.0, distance / max((minimum_distance or config.min_short_distance_points) * 3, width * 3, 1))
    regime = min(1.0, regime_confidence / 100)
    return round(100 * (
        0.30 * structure
        + 0.25 * credit
        + 0.20 * liquidity
        + 0.15 * buffer
        + 0.10 * regime
    ), 4)


def generate_candidates(
    snapshot: MarketSnapshot,
    feature: MarketFeatureSnapshot,
    regime: RegimeResult,
    regime_snapshot_id: int,
    config: StrategyConfig = StrategyConfig(),
    *,
    enforce_freshness: bool = False,
    now: datetime | None = None,
) -> StrategyCandidateSet:
    if config.credit_spread_policy.enabled:
        from app.strategy.credit_spread_engine import generate_credit_spread_candidates
        return generate_credit_spread_candidates(snapshot, feature, regime, regime_snapshot_id, config, enforce_freshness=enforce_freshness, now=now)
    regime_name = regime.regime.value
    if regime_name not in {"BULLISH", "BEARISH"}:
        return _none_result(snapshot_id=feature.snapshot_id, regime_id=regime_snapshot_id,
                            regime=regime_name, reasons=["REGIME_NOT_DIRECTIONAL"])
    if regime.confidence < config.min_regime_confidence:
        return _none_result(feature.snapshot_id, regime_snapshot_id, regime_name,
                            ["REGIME_CONFIDENCE_TOO_LOW"])
    if QUALITY_RANK[regime.evidence_quality.value] < QUALITY_RANK[config.min_evidence_quality]:
        return _none_result(feature.snapshot_id, regime_snapshot_id, regime_name,
                            ["EVIDENCE_QUALITY_TOO_LOW"])
    if enforce_freshness:
        current = now or datetime.now(IST)
        observed = snapshot.timestamp_ist
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=IST)
        age = (current - observed).total_seconds()
        if age < 0 or age > config.max_snapshot_age_seconds:
            return _none_result(feature.snapshot_id, regime_snapshot_id, regime_name,
                                ["SNAPSHOT_STALE"])
    if snapshot.expiry != feature.expiry or not snapshot.options:
        return _none_result(feature.snapshot_id, regime_snapshot_id, regime_name,
                            ["OPTION_CHAIN_UNAVAILABLE"])

    vix_regime = feature.volatility_features.vix_regime
    multipliers = {
        "LOW": config.vix_low_distance_multiplier,
        "NORMAL": config.vix_normal_distance_multiplier,
        "ELEVATED": config.vix_elevated_distance_multiplier,
        "HIGH": config.vix_high_distance_multiplier,
    }
    distance_multiplier = multipliers.get(vix_regime or "", 1.0) if config.volatility_buffer_enabled else 1.0
    if config.volatility_buffer_enabled and config.no_candidate_on_high_vix and vix_regime == "HIGH":
        return _none_result(feature.snapshot_id, regime_snapshot_id, regime_name,
                            ["VOLATILITY_POLICY_NO_CANDIDATE"])
    required_distance_points = config.min_short_distance_points * distance_multiplier
    required_distance_pct = config.min_short_distance_pct * distance_multiplier

    strategy = (StrategyType.BULL_PUT_SPREAD if regime_name == "BULLISH"
                else StrategyType.BEAR_CALL_SPREAD)
    option_type = "PE" if regime_name == "BULLISH" else "CE"
    reference = structural_reference(feature, regime_name)
    missing_structure = "NO_SUPPORT_REFERENCE" if regime_name == "BULLISH" else "NO_RESISTANCE_REFERENCE"
    if reference is None and config.require_structure_reference:
        return _none_result(feature.snapshot_id, regime_snapshot_id, regime_name,
                            [missing_structure])

    contracts = [
        item for item in snapshot.options
        if item.option_type.value == option_type
        and item.expiry == snapshot.expiry
        and item.strike > 0
        and bool(item.trading_symbol.strip())
    ]
    by_strike = {item.strike: item for item in contracts}
    candidates: list[CreditSpreadCandidate] = []
    rejection_codes: set[str] = set()
    for short in contracts:
        distance = abs(snapshot.nifty_spot - short.strike)
        distance_pct = distance / snapshot.nifty_spot * 100
        if regime_name == "BULLISH":
            otm = short.strike < snapshot.nifty_spot
            beyond = reference is None or short.strike <= reference
        else:
            otm = short.strike > snapshot.nifty_spot
            beyond = reference is None or short.strike >= reference
        if not otm or distance < required_distance_points or distance_pct < required_distance_pct:
            rejection_codes.add("SHORT_STRIKE_NOT_OTM")
            continue
        if not beyond:
            rejection_codes.add("SHORT_STRIKE_NOT_BEYOND_STRUCTURE")
            continue
        if not _delta_allowed(short, config):
            rejection_codes.add("DELTA_OUT_OF_RANGE")
            continue
        for allowed_width in config.allowed_spread_widths:
            long_strike = short.strike - allowed_width if regime_name == "BULLISH" else short.strike + allowed_width
            long = by_strike.get(long_strike)
            if long is None:
                rejection_codes.add("NO_VALID_LONG_HEDGE")
                continue
            liquidity = check_liquidity(short, long, config, feature.data_quality.intraday_volume_usable)
            if not liquidity.eligible:
                rejection_codes.update(liquidity.reason_codes)
                continue
            price = price_spread(short, long)
            if price is None or price.short_price < config.min_short_premium:
                rejection_codes.add("INSUFFICIENT_CREDIT")
                continue
            payoff = calculate_payoff(strategy, short.strike, long.strike, price.net_credit)
            if payoff is None:
                rejection_codes.add("INVALID_DEFINED_RISK_PAYOFF")
                continue
            credit_ratio = price.net_credit / payoff.width
            if price.net_credit < config.min_net_credit or credit_ratio < config.min_credit_to_width_ratio:
                rejection_codes.add("INSUFFICIENT_CREDIT")
                continue
            warnings = list(liquidity.warnings)
            if short.delta is None:
                warnings.append("DELTA_UNAVAILABLE")
            if price.basis == PricingBasis.LTP_ESTIMATE:
                warnings.append("PRICING_USING_LTP_ESTIMATE")
            reference_value = reference
            reference_gap = 0 if reference is None else abs(reference - short.strike)
            score = _score(
                reference_gap, distance, payoff.width, credit_ratio,
                short.open_interest or 0, long.open_interest or 0,
                price.basis, regime.confidence, config, required_distance_points,
            )
            lot = snapshot.lot_size
            candidates.append(CreditSpreadCandidate(
                candidate_id=(f"phase6_v1:{feature.snapshot_id}:{strategy.value}:"
                              f"{short.strike:g}:{long.strike:g}"),
                market_snapshot_id=feature.snapshot_id,
                regime_snapshot_id=regime_snapshot_id,
                strategy_type=strategy,
                expiry=snapshot.expiry,
                spot=snapshot.nifty_spot,
                atm_strike=snapshot.atm_strike,
                short_leg=_leg(short, "SELL"),
                long_leg=_leg(long, "BUY"),
                spread_width=payoff.width,
                net_credit=price.net_credit,
                credit_to_width_ratio=credit_ratio,
                max_profit=payoff.max_profit,
                max_loss=payoff.max_loss,
                breakeven=payoff.breakeven,
                max_profit_per_lot=None if lot is None else payoff.max_profit * lot,
                max_loss_per_lot=None if lot is None else payoff.max_loss * lot,
                lot_size=lot,
                short_leg_distance_from_spot=distance,
                short_leg_distance_pct=distance_pct,
                support_or_resistance_reference=reference_value,
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
                selection_score=score,
                reason_codes=["CANDIDATE_ELIGIBLE"],
                warnings=list(dict.fromkeys(warnings)),
                created_at=datetime.now(IST),
            ))
    candidates.sort(key=lambda item: (-item.selection_score, item.short_leg.strike, item.spread_width))
    candidates = candidates[:config.max_candidates]
    if not candidates:
        reasons = sorted(rejection_codes) + ["NO_ELIGIBLE_CANDIDATES"]
        return _none_result(feature.snapshot_id, regime_snapshot_id, regime_name, reasons)
    warnings = sorted({warning for item in candidates for warning in item.warnings})
    return StrategyCandidateSet(
        snapshot_id=feature.snapshot_id,
        regime_snapshot_id=regime_snapshot_id,
        regime=regime_name,
        strategy_type=strategy,
        eligible=True,
        candidates=candidates,
        candidate_count=len(candidates),
        reason_codes=["CANDIDATE_ELIGIBLE"],
        warnings=warnings,
        created_at=datetime.now(IST),
    )
