from datetime import date

from app.strategy.models import CreditSpreadCandidate, StrategyType


def strategy_fingerprint(candidate: CreditSpreadCandidate, trading_date: date) -> str:
    return (
        f"{trading_date.isoformat()}|{candidate.expiry.isoformat()}|"
        f"{candidate.strategy_type.value}|{candidate.short_leg.strike:g}|"
        f"{candidate.long_leg.strike:g}"
    )


def defined_risk_is_valid(candidate: CreditSpreadCandidate) -> bool:
    if candidate.short_leg.expiry != candidate.long_leg.expiry:
        return False
    if candidate.short_leg.option_type != candidate.long_leg.option_type:
        return False
    if candidate.short_leg.strike == candidate.long_leg.strike:
        return False
    if candidate.strategy_type == StrategyType.BULL_PUT_SPREAD:
        return (
            candidate.short_leg.option_type == "PE"
            and candidate.short_leg.action == "SELL"
            and candidate.long_leg.action == "BUY"
            and candidate.long_leg.strike < candidate.short_leg.strike
        )
    if candidate.strategy_type == StrategyType.BEAR_CALL_SPREAD:
        return (
            candidate.short_leg.option_type == "CE"
            and candidate.short_leg.action == "SELL"
            and candidate.long_leg.action == "BUY"
            and candidate.long_leg.strike > candidate.short_leg.strike
        )
    return False


def expected_strategy_for_regime(regime: str) -> StrategyType | None:
    if regime == "BULLISH":
        return StrategyType.BULL_PUT_SPREAD
    if regime == "BEARISH":
        return StrategyType.BEAR_CALL_SPREAD
    return None
