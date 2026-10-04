from dataclasses import dataclass

from app.data.models import OptionContractSnapshot


def bid_ask_spread_pct(contract: OptionContractSnapshot) -> float | None:
    if contract.bid is None or contract.ask is None or contract.bid <= 0 or contract.ask < contract.bid:
        return None
    midpoint = (contract.bid + contract.ask) / 2
    return None if midpoint <= 0 else (contract.ask - contract.bid) / midpoint * 100


def malformed_quotes(contract: OptionContractSnapshot) -> bool:
    if contract.bid is None or contract.ask is None:
        return False
    return contract.bid <= 0 or contract.ask <= 0 or contract.ask < contract.bid


@dataclass(frozen=True)
class LiquidityDecision:
    eligible: bool
    reason_codes: tuple[str, ...]
    warnings: tuple[str, ...]


def check_liquidity(short, long, config, volume_usable: bool) -> LiquidityDecision:
    reasons: list[str] = []
    warnings: list[str] = []
    if malformed_quotes(short) or malformed_quotes(long):
        reasons.append("MALFORMED_OPTION_RECORD")
    if short.open_interest is None or short.open_interest < config.min_short_open_interest:
        reasons.append("LIQUIDITY_TOO_LOW")
    if long.open_interest is None or long.open_interest < config.min_long_open_interest:
        reasons.append("LIQUIDITY_TOO_LOW")
    if volume_usable or config.require_usable_volume:
        if short.volume is None or short.volume < config.min_short_volume:
            reasons.append("LIQUIDITY_TOO_LOW")
        if long.volume is None or long.volume < config.min_long_volume:
            reasons.append("LIQUIDITY_TOO_LOW")
    else:
        warnings.append("VOLUME_UNAVAILABLE")
    for contract in (short, long):
        spread = bid_ask_spread_pct(contract)
        if spread is not None and spread > config.max_bid_ask_spread_pct:
            reasons.append("BID_ASK_TOO_WIDE")
    return LiquidityDecision(not reasons, tuple(sorted(set(reasons))), tuple(warnings))
